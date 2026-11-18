"""Isolated workbench entry for the offline scene trial. Both providers are injected fixtures."""
import asyncio
from contextvars import ContextVar
import json
import threading
import time

from persona_runtime.archive import fingerprint, read_exact, turn_scope
from persona_runtime.chat import ChatEngine
from persona_runtime.context_builders import DialogueBuilder
from persona_runtime.generation_retry import audited_complete
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.profile import snapshot
from persona_runtime.references import prepare as references
from persona_runtime.turn_input import load_turn_input

from .en5_decision_contract import target_list
from .en5_d3_presentation import BubbleCapture, D3Workbench, DesktopLedger
from .en5_scene_voice import VERSION, context, jev_request, project, require


class SceneCapture(BubbleCapture):
    def notice(self,turn,source_id,body,span):
        require(self.showing() and len(span)==2 and self.widget.bbox(span[0])
                and self.widget.get(*span)=='人格：'+body+'\n\n','notice_not_visible')
        self.ledger.engine.check_current(turn['id'])
        with self.ledger.db() as db:
            db.execute('INSERT OR IGNORE INTO scene_notices VALUES(?,?,?,?)',
                       (turn['id'],source_id,fingerprint(body),time.time()))
            saved=db.execute('SELECT source,body FROM scene_notices WHERE turn=?',(turn['id'],)).fetchone()
            require(tuple(saved)==(source_id,fingerprint(body)),'notice_receipt_conflict')


class SceneWorkbench(D3Workbench):
    capture_type = SceneCapture


class SceneDisplayLedger(DesktopLedger):
    def __init__(self,paths,binding):
        super().__init__(paths,binding)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scene_notices(turn TEXT PRIMARY KEY,source TEXT,body TEXT,at REAL)')

    def can_present(self, tid):
        try:
            with self.engine.store.connection() as db:
                row=db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone()
                require(row and read_exact(db,turn_scope(db,dict(row)),{'id':tid,'kind':'turn'}).get('usable'),
                        'display_source_changed')
                self.engine.scene_for(db,dict(row),presented=True)
            return True
        except (RuntimeErrorBase, OSError, ValueError):
            return False

    def plan(self,turn,body):
        parts,visible,stopped=super().plan(turn,body)
        if not stopped and len(visible)<len(parts) and not self.can_present(turn['id']):
            self.stop(turn['session_id'])
            stopped=True
        return parts,visible,stopped

    def acknowledge(self,tid,index,now):
        return self.can_present(tid) and super().acknowledge(tid,index,now)


class SceneDialogue(DialogueBuilder):
    def render(self, conn, turn, evidence, character=None, capsule=None):
        key, body = self.engine.scene_for(conn,turn)
        messages, details = super().render(conn,turn,evidence,character,capsule)
        projected, assembly = project(messages,details['prompt_assembly'],body,notice=bool(turn.get('_search_notice')))
        # Apply desktop visibility to all history pairs, including older retrieval hits.
        for i, group in reversed(list(enumerate(details['history_groups']))):
            row = dict(conn.execute('SELECT * FROM turns WHERE id=?',(group['turn_id'],)).fetchone())
            archive = read_exact(conn,turn_scope(conn,turn),{'kind':'turn','id':row['id']})
            require(archive.get('usable') and row['session_id']==turn['session_id'],'history_changed')
            full = '\n'.join(item['text'] for item in archive['replies'])
            index = 2+2*i
            require(projected[index]=={'role':'assistant','content':full},'history_layout')
            visible = [self.engine.shown(conn,row,item) for item in archive['replies']]
            spoken = '\n'.join(part for part in visible if part)
            if spoken:
                projected[index] = {'role':'assistant','content':spoken}
            else:
                del projected[index]
        from persona_runtime.prompt_assembly import PromptBlock, assemble
        blocks = [PromptBlock(b['name'],projected[0]['content'][b['start_character']:b['start_character']+b['characters']],
                              b['owner'],b['version']) for b in assembly['blocks'] if b['role']=='system']
        projected, assembly = assemble(blocks,projected[1:-1],projected[-1])
        assembly.update(expression_renderer=VERSION,format_renderer=details['prompt_assembly']['format_renderer'])
        require(assembly['input_characters']<=json.loads(turn['model_snapshot'])['max_input_chars'],'pro_budget')
        details = dict(details,prompt_assembly=assembly,input_characters=assembly['input_characters'],
                       scene_voice={'version':VERSION,'scene':key,'revision':self.engine.router.cards.revision,
                                    'card_sha256':fingerprint(body)})
        return projected, details


class SceneEngine(ChatEngine):
    wire_timing = False

    @staticmethod
    def check_transports(ledger,client,scene_transport):
        require(ledger.binding['mode']=='scene_fixture' and getattr(client,'d3_offline_fixture',False)
                and getattr(scene_transport,'offline_fixture',False),'offline_instance')

    def __init__(self,store,config,client,*,ledger,router,scene_transport,**kwargs):
        self.check_transports(ledger,client,scene_transport)
        require(isinstance(ledger,SceneDisplayLedger) and not config.load()['model'].get('encrypted_key')
                and ledger.binding['home']==str(store.paths.home.resolve())
                and ledger.binding['instance_id']==config.load()['instance_id']
                and ledger.binding['persona_id']==router.cards.persona
                and ledger.binding['scene_revision']==router.cards.revision,'offline_instance')
        super().__init__(store,config,client,**kwargs)
        self.ledger, self.router, self.scene_transport = ledger, router, scene_transport
        ledger.engine = self
        self.active_turn = ContextVar('scene_root_turn')
        self.cognitive.dialogue = SceneDialogue(store,self)
        self.gates = {}
        original_prepare = self.knowledge.prepare_turn
        def prepare(turn):
            result = original_prepare(turn)
            self.wait_scene(turn)
            return result
        self.knowledge.prepare_turn = prepare
        self.generation_executor = self.first_generation

    async def first_generation(self,client,model,messages,*,before_attempt,**kwargs):
        turn_id = self.active_turn.get()
        def first(state):
            require(not state['attempts'] and not state['prior_failures'],'first_main_only')
            self.check_current(turn_id)
            before_attempt(state)
        result = await audited_complete(client,model,messages,before_attempt=first,paths=self.store.paths,**kwargs)
        self.check_current(turn_id)
        return result

    def shown(self, db, row, reply):
        if reply['kind']=='search_notice':
            saved=db.execute("SELECT json_extract(value,'$.data.fully_presented') FROM knowledge_control WHERE key=?",
                             ('knowledge.search_notice.turn.'+row['id'],)).fetchone()
            return reply['text'] if saved and saved[0] else ''
        with self.ledger.db() as display:
            plan=display.execute('SELECT session,frames,event FROM plans WHERE turn=?',(row['id'],)).fetchone()
            if plan is None:
                return ''
            require(plan['session']==row['session_id'] and plan['event']==reply['id'],'history_binding')
            parts=json.loads(plan['frames'])
            displayed=''.join(p['text'] for p in parts)
            if displayed!=reply['text']:
                from persona_runtime.presentation import batch_view
                require(displayed==batch_view(reply['text'])[0],'history_body_changed')
            visible=[r[0] for r in display.execute('SELECT idx FROM visible WHERE turn=? ORDER BY idx',(row['id'],))]
            require(visible==list(range(len(visible))) and len(visible)<=len(parts),'history_receipts')
            return ''.join(p['text'] for p in parts[:len(visible)])

    def sources(self, db, turn, *, presented=False):
        binding=self.ledger.binding
        pid, sid=turn['persona_id'],turn['session_id']
        require(pid==binding['persona_id']==self.store.current(db) and sid in binding['sessions']
                and turn['channel']=='desktop' and turn['reply_mode_snapshot']=='context_full'
                and not turn['cancelled_at'] and not turn.get('new_input_event_id')
                and turn['status'] in (('completed',) if presented else ('queued','running')),'turn_inactive')
        catalog=self.router.cards.catalog()
        model=json.loads(turn['model_snapshot'])
        require(model.get('name')=='deepseek-v4-pro' and model.get('_companion_profile',{}).get('version')=='companion-runtime-v8'
                and db.execute('SELECT version_id FROM character_heads WHERE persona_id=?',(pid,)).fetchone()[0]
                ==json.loads(turn['character_snapshot'])['id']==catalog['character_version']
                and fingerprint(snapshot(db,pid))==catalog['profile_sha256']==fingerprint(json.loads(turn['profile_snapshot'])),
                'persona_changed')
        members=load_turn_input(db,turn)
        ordinal=db.execute('SELECT count(*) FROM turns WHERE session_id=? AND rowid<(SELECT rowid FROM turns WHERE id=?)',
                           (sid,turn['id'])).fetchone()[0]
        slots=binding['sessions'][sid]['inputs']
        require(ordinal<len(slots) and [fingerprint(m['text']) for m in members]==slots[ordinal],'input_changed')
        entries=[]
        def add(e, origin, index, role):
            row=db.execute('SELECT seq,persona_id,session_id,role FROM events WHERE id=?',(e['id'],)).fetchone()
            require(row and row['persona_id']==pid and row['session_id']==sid
                    and row['role']==('persona' if role=='assistant' else 'user')
                    and not db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(e['id'],)).fetchone(),
                    'source_withdrawn')
            entries.append(dict(event_id=e['id'],sequence=row['seq'],role=role,session_id=sid,persona_id=pid,
                                text=e['text'],source=origin,displayed=True,pro_target={'message_index':index}))
        previous=db.execute("""SELECT * FROM turns WHERE persona_id=? AND session_id=? AND channel='desktop'
              AND status='completed' AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 2""",
                            (pid,sid,turn['id'])).fetchall()
        for i,row in enumerate(reversed(previous)):
            group=read_exact(db,turn_scope(db,turn),{'id':row['id'],'kind':'turn'})
            if not group.get('usable'):
                continue
            for e in group['members']:
                add(e,'recent',2*i+1,'user')
            for e in group['replies']:
                shown=self.shown(db,row,e)
                if shown:
                    add(e|{'text':shown},'recent',2*i+2,'assistant')
        block,_=references(db,turn)
        if block:
            for group in json.loads(block.split('\n',2)[2])['turns']:
                row=db.execute('SELECT * FROM turns WHERE id=? AND session_id=?',(group['turn_id'],sid)).fetchone()
                require(row,'reference_scope')
                for e in group['replies']:
                    require(self.shown(db,row,e)==e['text'],'reference_not_visible')
                for e in [*group['members'],*group['replies']]:
                    if any(t['kind']=='turn' or t['id']==e['id'] for t in group['targets']):
                        add(e,'reference',0,e.get('role','assistant'))
        for e in members:
            add(e,'current',5,'user')
        targets=target_list(pid,sid,entries)
        return fingerprint([pid,sid,turn['id'],self.router.cards.revision,targets]),targets

    def scene_for(self, db, turn, *, presented=False):
        scope,_=self.sources(db,turn,presented=presented)
        return self.router.locked(turn['id'],scope)

    def check_current(self, tid):
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            return self.scene_for(db,turn)

    def wait_scene(self,turn):
        event=self.gates.get(turn['id'])
        if event:
            event.wait()
        self.check_current(turn['id'])

    def await_notice_scene(self,turn):
        from persona_runtime.tk_notice import required
        with self.store.connection() as db:
            require(required(db,turn),'visible_notice_required')
        self.wait_scene(turn)

    async def _prepare_context(self, tid, *args):
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            scope,targets=self.sources(db,turn)
        def check():
            with self.store.connection() as db:
                row=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                require(self.sources(db,row)[0]==scope,'sources_changed')
        state=context(targets)
        catalog=self.router.cards.catalog()
        wire=jev_request(self.ledger.binding['jev_model'],state,catalog['instruction'],catalog['criteria']) if state is not None else None
        if wire and len(json.dumps(wire,ensure_ascii=False))>json.loads(turn['model_snapshot'])['max_input_chars']:
            wire=None
        gate=threading.Event()
        self.gates[tid]=gate
        async def resolve():
            try:
                await self.router.resolve(tid,scope,wire,self.scene_transport,check,wire_timing=self.wire_timing)
            finally:
                gate.set()
        self.active_turn.set(tid)
        route=asyncio.create_task(resolve())
        try:
            return await super()._prepare_context(tid,*args)
        finally:
            if not route.done():
                route.cancel()
            gate.set()
            try:
                await route
            finally:
                self.gates.pop(tid,None)
