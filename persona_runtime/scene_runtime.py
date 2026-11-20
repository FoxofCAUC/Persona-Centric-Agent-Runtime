"""Select one approved scene before the main reply or search preface is assembled."""
from .call_limits import resume_on_limit, gate, turn_scope as quota_turn
import asyncio
import json
import threading
import time

from . import scene_policy
from .archive import fingerprint, read_exact, turn_scope
from .context_builders import DialogueBuilder
from .scene_jev import JevTransport
from .scene_voice import VERSION, WAIT_SECONDS, context, decision, jev_request, project, require
from .turn_input import load_turn_input

ROUTE = 'scene.route.'


class SceneDialogue(DialogueBuilder):
    def render(self, db, turn, evidence, character=None, capsule=None):
        messages, details = super().render(db,turn,evidence,character,capsule)
        selected = self.engine.scene.selected(db,turn)
        if selected is None:
            from .import_style import project as imported_projection
            return imported_projection(self.engine,db,turn,messages,details)
        scene, body = selected
        result, assembly = project(messages,details['prompt_assembly'],body,notice=bool(turn.get('_search_notice')))
        assembly.update(expression_renderer=VERSION,format_renderer=details['prompt_assembly']['format_renderer'])
        require(assembly['input_characters']<=json.loads(turn['model_snapshot'])['max_input_chars'],'pro_budget')
        return result, dict(details,prompt_assembly=assembly,input_characters=assembly['input_characters'],
                            scene_voice={'version':VERSION,'scene':scene,'card_sha256':fingerprint(body)})


class SceneRuntime:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.gates = {}
        self.jobs = set()
        self.transport = None
        self.connections = []

    def current(self, db, turn):
        policy = scene_policy.frozen(turn)
        config = self.engine.config.load()
        persona = turn['persona_id']
        require(policy and scene_policy.active(db,persona,self.store)==policy
                and config['instance_id']==policy['instance'] and self.store.current(db,required=False)==persona
                and turn['channel']=='desktop' and turn['reply_mode_snapshot']=='context_full'
                and turn['status'] in ('queued','running') and not turn.get('cancelled_at')
                and not turn.get('new_input_event_id'),'turn_inactive')
        from .sessions import assert_writable
        assert_writable(db,turn['session_id'])
        from .companion import read
        require((read(turn) or {}).get('version')==scene_policy.BASELINE,'v8_policy_required')
        from .dialogue_generation import enabled as direct_enabled
        from .expression_runtime import enabled as expression_enabled
        require(not direct_enabled(db,turn['id']) and not expression_enabled(db,turn['id']),'competing_generation')
        catalog, _ = scene_policy.material(self.store.paths,policy)
        require(scene_policy.head(db,persona)==(catalog['character_version'],catalog['profile_sha256'])
                ==(json.loads(turn['character_snapshot'])['id'],fingerprint(json.loads(turn['profile_snapshot']))),
                'persona_changed')
        model = json.loads(turn['model_snapshot'])
        require(scene_policy.provider_identity(config['model'])==policy['main'],'provider_changed')
        return policy,catalog,scene_policy.cloud(self.store,db,policy,model)

    @staticmethod
    def targets(db, turn):
        from .references import prepare as references
        from .tk_notice import already_presented
        ordered = {}
        def add(event, origin, index, role):
            row = db.execute('SELECT seq,role FROM events WHERE id=? AND session_id=? AND persona_id=?',
                             (event['id'],turn['session_id'],turn['persona_id'])).fetchone()
            require(row and row['role']==('persona' if role=='assistant' else 'user')
                    and not db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(event['id'],)).fetchone(),
                    'source_withdrawn')
            old = ordered.get(row['seq'])
            if old:
                require((old['role'],old['text'])==(role,event['text']),'source_changed')
                if origin not in old['origins']:
                    old['origins'].append(origin)
            else:
                ordered[row['seq']] = {'role':role,'text':event['text'],'origins':[origin],
                                       'pro_target':{'message_index':index}}
                if event["id"].startswith("imported."):
                    ordered[row['seq']]["imported_history"] = True
                    ordered[row['seq']]["recorded_at"] = event.get("created_at")
        scope = turn_scope(db,turn)
        previous = db.execute("""SELECT * FROM turns WHERE session_id=? AND status='completed'
              AND (source_key NOT LIKE 'imported.%' OR user_event_id IS NOT NULL OR reply_event_id IS NOT NULL)
              AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 2""",
                              (turn['session_id'],turn['id'])).fetchall()
        for i, prior in enumerate(reversed(previous)):
            group = read_exact(db,scope,{'kind':'turn','id':prior['id']})
            if not group.get('usable'):
                continue
            if group.get("coverage") == "imported_transcript":
                from .import_history import history_allowed
                if not history_allowed(db,turn["session_id"],"jev"):
                    continue
            for event in group['members']:
                add(event,'recent',2*i+1,'user')
            for event in group['replies']:
                # Main scene events contain the confirmed prefix. Older v8 events
                # follow the visibility contract of their original desktop client.
                if event['kind']!='search_notice' or already_presented(db,prior['id']):
                    add(event,'recent',2*i+2,'assistant')
        block, _ = references(db,turn)
        if block:
            for group in json.loads(block.split('\n',2)[2])['turns']:
                require(db.execute('SELECT 1 FROM turns WHERE id=? AND session_id=?',
                                   (group['turn_id'],turn['session_id'])).fetchone(),'reference_scope')
                if any(e["id"].startswith("imported.") for e in group["members"] + group["replies"]):
                    from .import_history import history_allowed
                    if not history_allowed(db,turn["session_id"],"jev"):
                        continue
                for event in [*group['members'],*group['replies']]:
                    if any(t['kind']=='turn' or t['id']==event['id'] for t in group['targets']):
                        add(event,'reference',0,event.get('role','assistant'))
        for event in load_turn_input(db,turn):
            add(event,'current',5,'user')
        require(any('current' in row['origins'] for row in ordered.values()),'current_input_missing')
        return [dict(ordered[key],sequence=key) for key in sorted(ordered)]

    def snapshot(self, db, turn):
        policy, catalog, model = self.current(db,turn)
        sources = self.targets(db,turn)
        token = fingerprint([turn['id'],turn['session_id'],policy,sources])
        state = context(sources)
        wire = jev_request(model['name'],state,catalog['instruction'],catalog['criteria']) if state is not None else None
        if wire and len(json.dumps(wire,ensure_ascii=False))>model['max_input_chars']:
            wire = None
        return token, wire, model

    @staticmethod
    def root(db, tid):
        row = db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone()
        require(row,'turn_inactive')
        return dict(row)

    def selected(self, db, turn):
        policy = scene_policy.frozen(turn)
        if not policy:
            return None
        token, _, _ = self.snapshot(db,turn)
        stored = scene_policy.read(db,ROUTE+turn['id'])
        require(stored and stored['scope']==token and stored['scene'],'decision_unavailable')
        return stored['scene'],scene_policy.card(self.store.paths,policy,stored['scene'])

    def ensure(self, turn):
        if not scene_policy.frozen(turn):
            return
        gate = self.gates.get(turn['id'])
        if gate:
            gate.wait()
        with self.store.connection() as db:
            self.selected(db,self.root(db,turn['id']))

    def before_notice(self, turn):
        if scene_policy.frozen(turn):
            from .tk_notice import required
            with self.store.connection() as db:
                require(required(db,turn),'visible_notice_required')
            self.ensure(turn)

    async def prepare(self, prepare, tid, *args):
        with self.store.connection() as db:
            turn = self.root(db,tid)
            if not scene_policy.frozen(turn):
                token = None
            else:
                token, wire, model = self.snapshot(db,turn)
        if token is None:
            return await prepare(tid,*args)
        gate = threading.Event()
        self.gates[tid] = gate
        async def resolve():
            try:
                await self.resolve(tid,token,wire,model)
            finally:
                gate.set()
        task = asyncio.create_task(resolve())
        try:
            result = await prepare(tid,*args)
            await task
            return result
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            self.gates.pop(tid,None)

    def revise(self, tid, **changes):
        with self.store.connection(write=True) as db:
            value = scene_policy.read(db,ROUTE+tid)
            if 'reason' in changes and value.get('reason') is not None:
                changes = {key:val for key,val in changes.items() if key not in ('reason','scene')}
            value.update(changes)
            scene_policy.save(db,ROUTE+tid,value)

    @resume_on_limit
    async def resolve(self, tid, token, wire, model):
        with self.store.connection(write=True) as db:
            old = scene_policy.read(db,ROUTE+tid)
            if old:
                require(old['scope']==token and old['reason'] is not None,'decision_conflict')
                return
            if wire is not None:gate(db,self.store,'scene',quota_turn(self.root(db,tid)))
            scene_policy.save(db,ROUTE+tid,{'scope':token,'request_sha256':fingerprint(wire),
                'started':None,'scene':None,'reason':None,'usage':None})
        if wire is None:
            self.revise(tid,scene='fallback',reason='budget')
            return
        loop = asyncio.get_running_loop()
        sent = asyncio.Event()
        received = loop.create_future()
        wire_start = None
        def on_dispatch(at):
            nonlocal wire_start
            require(wire_start is None,'duplicate_dispatch')
            wire_start = at
            sent.set()
        def on_receipt(at):
            require(wire_start is not None and at>=wire_start and not received.done(),'receipt_clock')
            received.set_result(at)

        async def first():
            with self.store.connection() as db:
                scope, current_wire, current_model = self.snapshot(db,self.root(db,tid))
                require((scope,current_wire,current_model)==(token,wire,model),'sources_changed')
            self.revise(tid,started=time.time())
            diagnostics = {}
            try:
                if self.transport is None or self.transport.model!=model:
                    self.transport = JevTransport(model)
                    self.connections.append(self.transport)
                status, raw = await self.transport.complete(wire,on_dispatch=on_dispatch,on_receipt=on_receipt,
                                                           diagnostics=diagnostics.update)
                require(received.done(),'receipt_clock_missing')
            except Exception:
                self.revise(tid,scene='fallback',reason='transport_unconfirmed',diagnostics=diagnostics)
                return
            elapsed = received.result()-wire_start
            from .knowledge_web_decision import usage
            try:
                value = json.loads(raw)
                measured = usage(value) if isinstance(value,dict) else None
            except (ValueError,TypeError):
                measured = None
            chosen, reason = ('fallback','timeout') if elapsed>=WAIT_SECONDS else decision(raw) if status==200 else ('fallback','http_error')
            try:
                with self.store.connection() as db:
                    require(self.snapshot(db,self.root(db,tid))[0]==token,'sources_changed')
            except Exception:
                chosen, reason = None,'source_changed'
            self.revise(tid,scene=chosen,reason=reason,raw=raw,http=status,wire_ms=elapsed*1000,
                        usage=measured,diagnostics=diagnostics)

        job = asyncio.create_task(first())
        self.jobs.add(job)
        def settled(done):
            self.jobs.discard(done)
            sent.set()
            if not done.cancelled():
                done.exception()
        job.add_done_callback(settled)
        try:
            await sent.wait()
            if wire_start is not None and not received.done():
                await asyncio.wait({job,received},timeout=max(0,wire_start+WAIT_SECONDS-loop.time()))
            if not job.done() and not received.done():
                self.revise(tid,scene='fallback',reason='timeout')
            else:
                await asyncio.shield(job)
        except BaseException:
            if wire_start is None:
                job.cancel()
            self.revise(tid,scene=None,reason='cancelled_or_source_changed')
            raise

    async def close(self):
        if self.jobs:
            await asyncio.gather(*list(self.jobs),return_exceptions=True)
        for connection in self.connections:
            await connection.close()
