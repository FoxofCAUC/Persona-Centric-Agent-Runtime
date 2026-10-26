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


def create_app(paths, client=None):
    config = ConfigStore(paths)
    config.initialize()
    store = Store(paths)
    store.initialize()
    engine = ChatEngine(store, config, client)
    engine.recover()
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
            request.path == "/v1/health" or request.path == "/v1/chat" or request.path.startswith("/v1/requests/")):
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
    app["stop"] = asyncio.Event()

    async def body(request):
        if request.content_type != "application/json":
            raise RuntimeErrorBase("JSON request required")
        value = await request.json()
        if not isinstance(value, dict):
            raise RuntimeErrorBase("JSON object required")
        return value

    def bridge_turn(request, turn):
        if request["role"] == "bridge" and turn["channel"] != "qq":
            raise web.HTTPForbidden()

    async def health(request):
        return web.json_response({"service": "persona-runtime", "instance_id": config.load()["instance_id"],
                                  "schema_version": SCHEMA_VERSION, "draining": engine.draining,
                                  "pending": len(engine.tasks)})

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
        result = await engine.submit(value.get("channel", "desktop"), value["text"], value["request_id"], value.get("session_id"))
        return web.json_response(result)

    async def get_request(request):
        turn = engine.get(request.match_info["id"])
        bridge_turn(request, turn)
        return web.json_response(turn)

    async def retry(request):
        turn = engine.get(request.match_info["id"])
        bridge_turn(request, turn)
        # A QQ duplicate must never trigger an automatic model retry.
        if request["role"] == "bridge":
            raise web.HTTPForbidden()
        return web.json_response(await engine.retry(turn["id"]))

    async def delivery(request):
        bridge_turn(request, engine.get(request.match_info["id"]))
        value = await body(request)
        return web.json_response(engine.delivery(request.match_info["id"], value["action"]))

    async def configuration(request):
        if request.method == "GET":
            return web.json_response(config.public())
        value = await body(request)
        old = config.load()
        if any(k in value and value[k] != old[k] for k in ("service_port", "astrbot_port", "onebot_port", "napcat_port", "owner_qq", "bot_qq")):
            raise RuntimeErrorBase("Stop services before changing QQ or port configuration")
        return web.json_response(config.update(value))

    async def model_test(request):
        # Only saved configuration is tested. All calls are durable and quota counted.
        return web.json_response(await engine.submit("test", "Reply with OK.", identifier(), test=True))

    async def manage(request):
        value = await body(request)
        action, args = value["action"], value.get("args", {})
        service = engine.service
        handlers = {
            "db.status": store.status,
            "persona.create": lambda spec=None: service.create_persona(PersonaSpec.from_dict(spec) if spec is not None else generic_persona()),
            "persona.list": service.list_personas, "persona.show": service.show_persona,
            "persona.use": service.use_persona, "relationship.set": service.set_relationship,
            "relationship.show": service.show_relationship, "session.list": service.list_sessions,
            "session.show": service.show_session, "session.select": engine.session,
            "event.append": service.append_event,
            "memory.add": service.add_memory, "memory.search": service.search_memories,
            "memory.show": service.show_memory, "memory.revise": service.revise_memory,
            "memory.retract": service.retract_memory, "data.export": service.export_persona,
            "data.backup": service.backup, "chat.history": engine.history,
            "delivery.pending": lambda: pending_deliveries(store),
        }
        if action not in handlers or not isinstance(args, dict):
            raise RuntimeErrorBase("Unknown management action")
        return web.json_response(handlers[action](**args))

    async def control(request):
        operation = request.match_info["operation"]
        if operation not in ("drain", "stop", "resume"):
            raise web.HTTPNotFound()
        engine.draining = operation != "resume"
        if operation == "stop":
            await engine.wait()
            app["stop"].set()
        return web.json_response({"draining": engine.draining, "pending": len(engine.tasks)})

    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/chat", chat)
    app.router.add_get("/v1/requests/{id}", get_request)
    app.router.add_post("/v1/requests/{id}/retry", retry)
    app.router.add_post("/v1/requests/{id}/delivery", delivery)
    app.router.add_get("/v1/config", configuration)
    app.router.add_post("/v1/config", configuration)
    app.router.add_post("/v1/model-test", model_test)
    app.router.add_post("/v1/manage", manage)
    app.router.add_post("/v1/control/{operation}", control)

    async def cleanup(_):
        engine.draining = True
        await engine.wait()
    app.on_cleanup.append(cleanup)
    return app


def pending_deliveries(store):
    with store.connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT d.turn_id,d.state,t.persona_id,t.session_id,t.status FROM deliveries d "
            "JOIN turns t ON t.id=d.turn_id WHERE d.state!='sent' ORDER BY d.updated_at DESC")]


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
