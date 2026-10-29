"""Authenticated loopback API shared by the desktop and the QQ bridge."""
import asyncio
import hmac
import json
import logging
import sqlite3

from aiohttp import web

from .chat import ChatEngine
from .config import ConfigStore
from .instance import InstanceLock
from .logging_safe import setup
from .models import PersonaSpec, RuntimeErrorBase, generic_persona, identifier
from .paths import RuntimePaths
from .storage import SCHEMA_VERSION, Store
from .submissions import SubmissionJournal
from .organization import OrganizationEngine
from .profile import ProfileService
from .character import CharacterService
from .ingress import IngressService
from .delivery import DeliveryService
from .reply_service import ReplyService
from .quotas import QuotaService
from .persona_management import PersonaManagement


def create_app(paths, client=None):
    config = ConfigStore(paths)
    config.initialize()
    store = Store(paths)
    store.initialize()
    engine = ChatEngine(store, config, client)
    engine.recover()
    ingress=IngressService(engine)
    delivery_service=DeliveryService(store)
    ingress.recover()
    organization = OrganizationEngine(store, config, engine.client, engine)
    organization.recover()
    from .sessions import SessionService
    from .topics import TopicService
    from .session_organization import SessionOrganization
    sessions=SessionService(store);sessions.restart_observation()
    topics=TopicService(store);session_organization=SessionOrganization(organization)
    profiles=ProfileService(store)
    characters=CharacterService(store)
    from .archive import ArchiveService
    archive=ArchiveService(store)
    from .archive_search import ArchiveSearchService
    archive_search=ArchiveSearchService(store)
    admin, bridge = config.token(), config.token("bridge")

    @web.middleware
    async def guard(request, handler):
        peer = request.remote
        if peer not in ("127.0.0.1", "::1") or request.headers.get("Origin"):
            return web.json_response({"error": "local_clients_only"}, status=403)
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if hmac.compare_digest(supplied.encode("utf-8"), admin.encode("utf-8")):
            request["role"] = "admin"
        elif hmac.compare_digest(supplied.encode("utf-8"), bridge.encode("utf-8")):
            request["role"] = "bridge"
        else:
            return web.json_response({"error": "unauthorized"}, status=401)
        if request["role"] == "bridge" and not (
            request.path in ("/v1/health", "/v1/chat", "/v1/qq-pending", "/v2/delivery/streams")
            or request.path.startswith(("/v1/requests/", "/v1/submissions/"))
            or (request.method=="GET" and request.path.startswith(("/v2/submissions/","/v2/batches/","/v2/delivery/streams/","/v2/replies/")))
            or (request.method=="POST" and request.path.startswith(("/v2/replies/")))
            or request.path=="/v2/ingress"):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (RuntimeErrorBase, ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
            logging.getLogger("api").warning("operation_failed", exc_info=True)
            message = str(error) if isinstance(error, RuntimeErrorBase) else "invalid_input_or_storage_error"
            return web.json_response({"error": message}, status=400)

    app = web.Application(middlewares=[guard], client_max_size=256*1024)
    app["engine"], app["config"], app["store"] = engine, config, store
    app["organization"] = organization
    app["sessions"] = sessions
    app["ingress"] = ingress
    app["stop"] = asyncio.Event()

    async def body(request):
        if request.content_type != "application/json":
            raise RuntimeErrorBase("JSON request required")
        value = await request.json()
        if not isinstance(value, dict):
            raise RuntimeErrorBase("JSON object required")
        return value

    def bridge_turn(request, turn):
        if request["role"] == "bridge":
            cfg = config.load()
            if turn["channel"] != "qq" or turn["origin"] != {"sender": cfg["owner_qq"], "bot": cfg["bot_qq"]}:
                raise web.HTTPForbidden()

    async def health(request):
        return web.json_response({"service": "persona-runtime", "instance_id": config.load()["instance_id"],
                                  "schema_version": SCHEMA_VERSION, "draining": engine.draining,
                                  "pending": len(engine.tasks), "background_running": organization.running,
                                  "version": "0.7.5.dev4", "selector_configured":bool(config.load()["character_selector"]["enabled"] and config.load()["character_selector"]["name"] and config.load()["character_selector"]["base_url"]), "capabilities":["character_selection_v1","character_retrieval_v1","reply_batch_v2","reply_rhythm_v1","session_lifecycle_v1","session_package_v1","thread_manual_v1","cognitive_context_v1","archive_search_v1","working_set_preview_v1","archive_exact_v1","input_reference_v1","durable_ingress_v2","input_batch_v1","reply_batch_v1","behavior_resolver_v1","reply_delivery_parts_v1"], "buffer_pending":ingress.unresolved_count()})

    async def chat(request):
        value = await body(request)
        if request["role"] == "bridge":
            current = config.load()
            if not current["owner_qq"] or value.get("sender") != current["owner_qq"] or value.get("bot") != current["bot_qq"]:
                raise web.HTTPForbidden()
            if value.get("channel") != "qq" or not value.get("request_id", "").startswith("qq-"):
                raise web.HTTPForbidden()
        elif value.get("channel", "desktop") != "desktop":
            raise RuntimeErrorBase("Desktop clients must use desktop channel")
        origin = {"sender": value["sender"], "bot": value["bot"]} if request["role"] == "bridge" else None
        result = await engine.submit(value.get("channel", "desktop"), value["text"], value["request_id"], value.get("session_id"), origin=origin, persona_id=value.get("persona_id"))
        return web.json_response(result)

    def bridge_batch(request,value):
        if request['role']=='bridge':
            cfg=config.load()
            if value['channel']!='qq' or value['origin']!={'sender':cfg['owner_qq'],'bot':cfg['bot_qq']}:raise web.HTTPForbidden()

    async def ingress_accept(request):
        value=await body(request)
        if request['role']=='bridge':
            cfg=config.load()
            if value.get('channel')!='qq' or not cfg['owner_qq'] or value.get('sender')!=cfg['owner_qq'] or value.get('bot')!=cfg['bot_qq']:raise web.HTTPForbidden()
        elif value.get('channel','desktop')!='desktop':raise web.HTTPForbidden()
        return web.json_response(ingress.accept(value))

    async def ingress_lookup(request):
        if request['role']=='bridge' and request.match_info['channel']!='qq':raise web.HTTPForbidden()
        value=ingress.lookup(request.match_info['channel'],request.match_info['request_id'])
        if value is None:return web.json_response({'error':'submission_not_found'},status=404)
        bridge_batch(request,value)
        return web.json_response(value)

    async def batch_read(request):
        value=ingress.get(request.match_info['id']);bridge_batch(request,value)
        return web.json_response(value)

    async def batch_action(request):
        if request['role']!='admin':raise web.HTTPForbidden()
        value=await body(request)
        return web.json_response(ingress.action(request.match_info['id'],request.match_info['action'],value['expected_revision'],value['operation_id'],value['persona_id']))

    async def get_request(request):
        turn = engine.get(request.match_info["id"])
        bridge_turn(request, turn)
        return web.json_response(turn)

    async def submission(request):
        if request["role"] == "bridge" and request.match_info["channel"] != "qq":
            raise web.HTTPForbidden()
        turn = engine.lookup(request.match_info["channel"], request.match_info["request_id"])
        if turn is None:
            return web.json_response({"error": "submission_not_found"}, status=404)
        bridge_turn(request, turn)
        return web.json_response(turn)

    async def cancel(request):
        if request["role"] != "admin":
            raise web.HTTPForbidden()
        if not config.load()["interaction"]["cancel_queued"]:
            raise RuntimeErrorBase("queue_cancellation_disabled")
        return web.json_response(engine.cancel_queued(request.match_info["id"]))

    async def retry(request):
        turn = engine.get(request.match_info["id"])
        bridge_turn(request, turn)
        # A QQ duplicate must never trigger an automatic model retry.
        if request["role"] == "bridge":
            raise web.HTTPForbidden()
        return web.json_response(await engine.retry(turn["id"]))

    async def delivery(request):
        current=engine.get(request.match_info["id"])
        bridge_turn(request, current)
        if current.get("delivery_stream"):
            raise RuntimeErrorBase("m63_delivery_protocol_required")
        value = await body(request)
        return web.json_response(engine.delivery(request.match_info["id"], value["action"]))

    async def notice(request):
        bridge_turn(request, engine.get(request.match_info["id"]))
        value = await body(request)
        return web.json_response(engine.claim_notice(request.match_info["id"], value["kind"]))

    async def qq_pending(request):
        cfg = config.load()
        rows = []
        with store.connection() as conn:
            for row in conn.execute("SELECT t.id FROM turns t JOIN deliveries d ON d.turn_id=t.id "
                                    "WHERE t.status='completed' AND d.state='pending' "
                                    "AND NOT EXISTS (SELECT 1 FROM reply_delivery_parts p WHERE p.turn_id=t.id) AND NOT EXISTS(SELECT 1 FROM legacy_delivery_dispositions x WHERE x.turn_id=t.id) ORDER BY t.rowid LIMIT 100"):
                turn = engine._view(conn, row[0])
                if turn["origin"] == {"sender": cfg["owner_qq"], "bot": cfg["bot_qq"]}:
                    rows.append(turn)
        return web.json_response(rows)

    def delivery_origin(request):
        if request["role"] != "bridge":
            return None
        cfg=config.load()
        return {"sender":cfg["owner_qq"],"bot":cfg["bot_qq"]}

    async def delivery_streams(request):
        return web.json_response(delivery_service.pending(origin=delivery_origin(request),persona_id=request.query.get("persona_id")))

    async def delivery_stream(request):
        return web.json_response(delivery_service.get(request.match_info["stream_id"],origin=delivery_origin(request)))

    async def reply_parts(request):
        value=delivery_service.by_batch(request.match_info["reply_batch_id"])
        if value is None:raise web.HTTPNotFound()
        if request["role"]=="bridge":
            cfg=config.load()
            if value["channel"]!="qq" or value["origin"]!={"sender":cfg["owner_qq"],"bot":cfg["bot_qq"]}:raise web.HTTPForbidden()
        value["parts"]=[part for part in value["parts"] if part["reply_batch_id"]==request.match_info["reply_batch_id"]]
        return web.json_response(value)

    async def delivery_claim(request):
        value=await body(request);batch=delivery_service.by_batch(request.match_info["reply_batch_id"])
        if batch is None:raise web.HTTPNotFound()
        stream_id=batch["id"]
        if request.match_info["part_id"] not in {part["id"] for part in batch["parts"] if part["reply_batch_id"]==request.match_info["reply_batch_id"]}:raise web.HTTPNotFound()
        origin=delivery_origin(request)
        return web.json_response(delivery_service.claim(stream_id,request.match_info["part_id"],value["expected_revision"],
            value["operation_id"],persona_id=value.get("persona_id"),origin=origin,actor=request["role"],capabilities=value.get("capabilities")))

    async def delivery_result(request):
        value=await body(request);origin=delivery_origin(request);batch=delivery_service.by_batch(request.match_info["reply_batch_id"])
        if batch is None:raise web.HTTPNotFound()
        if request.match_info["part_id"] not in {part["id"] for part in batch["parts"] if part["reply_batch_id"]==request.match_info["reply_batch_id"]}:raise web.HTTPNotFound()
        stream_id=batch["id"]
        return web.json_response(delivery_service.result(stream_id,request.match_info["part_id"],
            value["claim_token"],value["state"],value["expected_revision"],value["operation_id"],
            platform_message_id=value.get("platform_message_id"),error=value.get("error"),persona_id=value.get("persona_id"),
            origin=origin,actor=request["role"]))

    async def delivery_resolve(request):
        if request["role"]!="admin":raise web.HTTPForbidden()
        value=await body(request);batch=delivery_service.by_batch(request.match_info["reply_batch_id"])
        if batch is None:raise web.HTTPNotFound()
        if request.match_info["part_id"] not in {part["id"] for part in batch["parts"] if part["reply_batch_id"]==request.match_info["reply_batch_id"]}:raise web.HTTPNotFound()
        return web.json_response(delivery_service.resolve(batch["id"],request.match_info["part_id"],
            value["action"],value["expected_revision"],value["operation_id"],persona_id=value["persona_id"],
            reason=value.get("reason"),allow_continue=value.get("allow_continue",True)))

    async def configuration(request):
        if request.method == "GET":
            return web.json_response(config.public())
        value = await body(request)
        old = config.load()
        if any(k in value and value[k] != old[k] for k in ("service_port", "astrbot_port", "onebot_port", "napcat_port", "owner_qq", "bot_qq")):
            raise RuntimeErrorBase("Stop services before changing QQ or port configuration")
        result = config.update(value)
        organization.schedule.pause_changed_models()
        return web.json_response(result)

    async def model_test(request):
        # Only saved configuration is tested. All calls are durable and quota counted.
        return web.json_response(await engine.submit("test", "Reply with OK.", identifier(), test=True))

    async def manage(request):
        value = await body(request)
        action, args = value["action"], value.get("args", {})
        service = engine.service
        if not isinstance(args, dict):
            raise RuntimeErrorBase("JSON object required")
        if action in ('context.preview','context.inspect'):
            return web.json_response(await asyncio.to_thread(getattr(engine.cognitive,action.split('.')[1]),**args))
        search_handlers={"archive.search":archive_search.search,"archive.search-status":archive_search.status,
                         "archive.search-rebuild":archive_search.rebuild,"archive.working-set-preview":archive_search.preview}
        if action in search_handlers:
            # SQLite, tokenization and snapshot verification must not stall the
            # ingress/generation/delivery event loop. Each request is bounded.
            return web.json_response(await asyncio.to_thread(search_handlers[action],**args))
        organization_handlers = {
            "organization.preview": organization.preview,
            "organization.submit": lambda preview, request_id: organization.submit(preview, request_id),
            "organization.list": organization.list, "organization.show": organization.show,
            "organization.sessions": organization.sessions,
            "organization.retry": organization.retry, "organization.cancel": organization.cancel,
            "organization.skip": organization.skip, "organization.status": organization.schedule.status,
            "organization.automatic": organization.schedule.set_automatic,
            "organization.daily-resume": organization.schedule.resume_daily,
            "organization.daily-run-now": organization.schedule.run_daily_now,
            "organization.model-test": organization.test_model,
            "summary.list": organization.summaries.list, "summary.show": organization.summaries.show,
            "summary.disable": organization.summaries.disable,
            "candidate.list": organization.candidates.list, "candidate.show": organization.candidates.show,
            "candidate.accept": organization.candidates.accept, "candidate.reject": organization.candidates.reject,
            "recall.explain": organization.explain,
        }
        if action in organization_handlers:
            return web.json_response(organization_handlers[action](**args))
        if action in ("inbox.check", "inbox.submit"):
            journal = SubmissionJournal(paths, "qq")
            cfg = config.load()
            rows = journal.pending()
            if action == "inbox.submit":
                selected = next((row for row in rows if row["payload"]["request_id"] == args.get("request_id")), None)
                if not selected:
                    raise RuntimeErrorBase("Inbox record not found")
                payload = selected["payload"]
                origin = {"sender": payload["sender"], "bot": payload["bot"]}
                if origin != {"sender": cfg["owner_qq"], "bot": cfg["bot_qq"]}:
                    raise RuntimeErrorBase("Inbox target changed; automatic redirection is forbidden")
                turn = ingress.lookup("qq", payload["request_id"]) or engine.lookup("qq", payload["request_id"])
                if turn is None:
                    if payload.get("platform_message_id"):turn = ingress.accept(payload)
                    else:turn = await engine.submit("qq",payload["text"],payload["request_id"],origin=origin)
                journal.forget(payload["request_id"])
                return web.json_response(turn)
            checked = []
            for row in rows:
                payload = row["payload"]
                turn = ingress.lookup("qq", payload["request_id"]) or engine.lookup("qq", payload["request_id"])
                if turn:
                    journal.forget(payload["request_id"])
                checked.append(row | {"turn": turn})
            return web.json_response(checked)
        if action.startswith('session.') and action not in ('session.select',):
            names={'current':sessions.current,'list':sessions.list,'show':sessions.show,'seal':sessions.seal,
                   'reopen':sessions.reopen,'package':sessions.package,'replay':sessions.replay,'triggers':sessions.triggers,
                   'delivery.abandon':sessions.abandon_delivery,'organization.preview':session_organization.preview,
                   'organization.submit':session_organization.submit}
            name=action[len('session.'):]
            # Old M1/M4 reads keep their response; explicit scope selects lifecycle DTOs.
            if name in names and (name not in ('list','show') or 'persona_id' in args):
                if any(k.startswith('_') for k in args):raise RuntimeErrorBase('invalid_session_field')
                return web.json_response(await asyncio.to_thread(names[name],**args))
        if action.startswith(('topic.','thread.')):
            kind,name=action.split('.',1)
            if name=='link' and kind=='thread':return web.json_response(await asyncio.to_thread(topics.link,**args))
            if name in ('list','show','create','update'):
                return web.json_response(await asyncio.to_thread(getattr(topics,name),kind=kind,**args))
        if action=='character.selector-test':
            return web.json_response(await engine.selector.probe(**args))
        if action=='character.selector-records':
            return web.json_response(engine.selector.list(**args))
        if action=='character.selector-status':
            return web.json_response({'configuration':config.public()['character_selector'],'busy':engine.selector.busy})
        if action=='character.selector-copy-main':
            return web.json_response(config.copy_selector_model())
        handlers = {
            **{"archive."+name:getattr(archive,name) for name in ("list","get","resolve","preview")},
            'quota.current': lambda: QuotaService(store, config).current(),
            'quota.update': lambda limits: QuotaService(store, config).update(limits),
            'quota.acceptance.begin': lambda: QuotaService(store, config).begin_acceptance(),
            'quota.acceptance.end': lambda: QuotaService(store, config).end_acceptance(),
            "interaction.current":ingress.current_policy,"interaction.update":ingress.update_policy,"batch.list":ingress.list,
            "reply.current":lambda persona_id,channel:ingress.current_policy(persona_id,channel),
            "reply.update":lambda persona_id,channel,reply_mode,expected_version,operation_id,pending_action=None:ingress.update_policy(persona_id,channel,ingress.current_policy(persona_id,channel)['mode'],expected_version,operation_id,pending_action=pending_action,reply_mode=reply_mode),
            "reply.preview":ReplyService(store).preview,
            "reply.cancel":delivery_service.cancel_optional,
            **{"character."+name:getattr(characters,name) for name in ("current","update","history","show","compare","restore","fact","preview","export")},
            "character.import":characters.import_package,
            'profile.current':profiles.current,'profile.history':profiles.history,'profile.update':profiles.update,
            'profile.restore':profiles.restore,'profile.compare':profiles.compare,
            'relationship.propose':profiles.propose_relationship,'relationship.proposals':profiles.proposals,
            'relationship.proposal-show':profiles.proposal_show,'relationship.review':profiles.review_relationship,
            "db.status": store.status,
            "persona.create": lambda spec=None: service.create_persona(PersonaSpec.from_dict(spec) if spec is not None else generic_persona()),
            "persona.list": service.list_personas, "persona.show": service.show_persona,
            "persona.use": service.use_persona, "relationship.set": service.set_relationship,
            **{"persona."+name:getattr(PersonaManagement(store),name)
               for name in ('inspect','clone','trash','restore')},
            "relationship.show": service.show_relationship, "session.list": service.list_sessions,
            "session.show": service.show_session, "session.select": engine.session,
            "event.append": service.append_event,
            "memory.add": service.add_memory, "memory.search": service.search_memories,
            "memory.show": service.show_memory, "memory.revise": service.revise_memory,
            "memory.retract": service.retract_memory, "data.export": service.export_persona,
            "data.backup": service.backup, "chat.history": engine.history,
            "chat.sessions": engine.desktop_sessions,
            "chat.snapshot": engine.history_snapshot, "chat.requests": engine.requests,
            "delivery.pending": lambda: pending_deliveries(store, delivery_service),
            "delivery.list": lambda persona_id: delivery_service.list(persona_id),
            "delivery.show": lambda stream_id, persona_id: delivery_service.get(stream_id, persona_id=persona_id),
            "delivery.resolve": lambda stream_id, part_id, action, expected_revision, operation_id, persona_id, reason=None, allow_continue=True: delivery_service.resolve(
                stream_id, part_id, action, expected_revision, operation_id, persona_id=persona_id,
                reason=reason, allow_continue=allow_continue),
        }
        if (action not in handlers and action not in ('character.index-status','character.index-prepare')) or not isinstance(args, dict):
            raise RuntimeErrorBase("Unknown management action")
        if action=='character.preview':return web.json_response(await asyncio.to_thread(engine.cognitive.characters.preview,**args))
        if action in ('character.index-status','character.index-prepare'):
            handler=engine.cognitive.characters.status if action.endswith('status') else engine.cognitive.characters.request_prepare
            return web.json_response(await asyncio.to_thread(handler,**args))
        if action in ('character.update','character.restore','character.import'):
            return web.json_response(await asyncio.to_thread(handlers[action],**args))
        return web.json_response(handlers[action](**args))

    async def control(request):
        operation = request.match_info["operation"]
        if operation not in ("drain", "stop", "resume"):
            raise web.HTTPNotFound()
        if operation in ("drain","stop") and ingress.unresolved_count():
            raise RuntimeErrorBase("unresolved_input_batches")
        engine.draining = operation != "resume"
        if operation == "resume":
            organization.draining = False
        if operation == "stop":
            await organization.drain()
            await engine.wait()
            app["stop"].set()
        return web.json_response({"draining": engine.draining, "pending": len(engine.tasks)})

    app.router.add_post("/v2/ingress",ingress_accept)
    app.router.add_get("/v2/submissions/{channel}/{request_id:.+}",ingress_lookup)
    app.router.add_get("/v2/batches/{id}",batch_read)
    app.router.add_post("/v2/batches/{id}/{action:seal|resume|cancel}",batch_action)
    app.router.add_get("/v2/delivery/streams",delivery_streams)
    app.router.add_get("/v2/delivery/streams/{stream_id}",delivery_stream)
    app.router.add_get("/v2/replies/{reply_batch_id}/parts",reply_parts)
    app.router.add_post("/v2/replies/{reply_batch_id}/parts/{part_id}/claim",delivery_claim)
    app.router.add_post("/v2/replies/{reply_batch_id}/parts/{part_id}/result",delivery_result)
    app.router.add_post("/v2/replies/{reply_batch_id}/parts/{part_id}/resolve",delivery_resolve)
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/chat", chat)
    app.router.add_get("/v1/qq-pending", qq_pending)
    app.router.add_get("/v1/requests/{id}", get_request)
    app.router.add_get("/v1/submissions/{channel}/{request_id:.+}", submission)
    app.router.add_post("/v1/requests/{id}/cancel", cancel)
    app.router.add_post("/v1/requests/{id}/retry", retry)
    app.router.add_post("/v1/requests/{id}/delivery", delivery)
    app.router.add_post("/v1/requests/{id}/notice", notice)
    app.router.add_get("/v1/config", configuration)
    app.router.add_post("/v1/config", configuration)
    app.router.add_post("/v1/model-test", model_test)
    app.router.add_post("/v1/manage", manage)
    app.router.add_post("/v1/control/{operation}", control)

    async def startup(_):
        await asyncio.to_thread(engine.cognitive.characters.notify,None)
        app["session_worker"] = asyncio.create_task(sessions.worker())
        app["organization_worker"] = asyncio.create_task(organization.worker())
        app["ingress_worker"] = asyncio.create_task(ingress.worker())
    app.on_startup.append(startup)

    async def cleanup(_):
        engine.draining = True
        session_worker=app.get("session_worker")
        if session_worker:
            session_worker.cancel()
            await asyncio.gather(session_worker,return_exceptions=True)
        ingress_worker=app.get("ingress_worker")
        if ingress_worker:
            ingress_worker.cancel()
            await asyncio.gather(ingress_worker,return_exceptions=True)
        await organization.drain()
        worker = app.get("organization_worker")
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await engine.wait()
        await asyncio.to_thread(engine.cognitive.characters.close)
    app.on_cleanup.append(cleanup)
    return app


def pending_deliveries(store, delivery=None):
    with store.connection() as conn:
        rows=[dict(row) for row in conn.execute(
            "SELECT d.turn_id,d.state,t.persona_id,t.session_id,t.status FROM deliveries d "
            "JOIN turns t ON t.id=d.turn_id WHERE d.state!='sent' "
            "AND NOT EXISTS (SELECT 1 FROM legacy_delivery_dispositions x WHERE x.turn_id=t.id) "
            "AND NOT EXISTS (SELECT 1 FROM reply_delivery_parts p WHERE p.turn_id=t.id) ORDER BY d.updated_at DESC")]
        if delivery:
            for stream in delivery.pending():
                pending=[part for part in stream['parts'] if part['state'] in ('pending','sending')]
                for part in pending:
                    rows.append({'turn_id':part['turn_id'],'state':part['state'],'persona_id':stream['persona_id'],
                                 'session_id':None,'status':'completed','part_id':part['id']})
        return rows


async def serve(paths):
    lock = InstanceLock(paths.home / "runtime" / "service.lock")
    try:
        setup(paths, "service")
        app = create_app(paths)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=65)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", app["config"].load()["service_port"]).start()
            await app["stop"].wait()
        finally:
            await runner.cleanup()
    finally:
        lock.close()
