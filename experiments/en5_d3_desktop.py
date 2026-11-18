"""Opt-in D3 workbench engineering route. Its local entry point supplies both transports explicitly."""
import asyncio
from copy import deepcopy
from contextvars import ContextVar
import json
import time

from persona_runtime.archive import fingerprint, read_exact, turn_scope
from persona_runtime.chat import ChatEngine
from persona_runtime.deepseek_reply_decision import parse_receipt as parse_tool_receipt
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.expression_runtime import enabled as expression_enabled
from persona_runtime.generation_retry import audited_complete
from persona_runtime.instance import InstanceLock
from persona_runtime.profile import snapshot
from persona_runtime.prompt_assembly import fit_manifest
from persona_runtime.references import prepare as references
from persona_runtime.server import create_app
from persona_runtime.turn_input import load_turn_input, render_input
from .en5_decision_contract import DecisionWindow, flash_base, flash_wire, persona_projection, pro_wire, require, target_list
from .en5_decision_tool import MODE, decision_pair
from .en5_d3_presentation import DesktopLedger


class DesktopDecision:
    def __init__(self, store, ledger, transport, config):
        self.store, self.ledger, self.transport, self.config = store, ledger, transport, config
        self.jobs = set()
        with ledger.db() as db:
            db.execute("""UPDATE decisions SET reason=CASE WHEN started=0 THEN 'restart_before_dispatch'
                ELSE 'restart_unknown' END WHERE reason IS NULL""")

    def check(self, db, turn):
        binding = self.ledger.binding
        sid=turn['session_id']
        require(self.store.current(db)==binding['persona_id']==turn['persona_id'] and sid in binding['sessions']
                and turn['channel']=='desktop' and turn['reply_mode_snapshot']=='context_full'
                and json.loads(turn['model_snapshot']).get('_companion_profile',{}).get('version')=='companion-runtime-v8'
                and not turn['cancelled_at'] and turn['status'] in ('queued','running')
                and not turn.get('new_input_event_id'), 'desktop_turn_inactive')
        spec=binding['sessions'][sid]
        n=db.execute("""SELECT count(*) FROM turns WHERE session_id=? AND persona_id=? AND rowid<
            (SELECT rowid FROM turns WHERE id=?)""",(sid,turn['persona_id'],turn['id'])).fetchone()[0]
        members=load_turn_input(db,turn)
        require(n<len(spec['inputs']) and [fingerprint(m['text']) for m in members]==spec['inputs'][n],
                'desktop_unreviewed_input')
        require(all(not db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(m['id'],)).fetchone()
                    for m in members),'desktop_source_withdrawn')
        head=db.execute('SELECT version_id FROM character_heads WHERE persona_id=?',(turn['persona_id'],)).fetchone()
        require(head and json.loads(turn['character_snapshot'])['id']==head[0]==binding['character_version']
                and fingerprint(json.loads(turn['profile_snapshot']))==binding['profile_hash']==fingerprint(snapshot(db,turn['persona_id'])),
                'desktop_persona_changed')
        return spec,members

    def visible_material(self, db, turn, prepared):
        spec,current=self.check(db,turn)
        messages=prepared['messages']
        details=prepared['details']
        groups=details['history_groups']
        require(len(messages)==2+2*len(groups) and messages[0]['role']=='system'
                and messages[-1]=={'role':'user','content':render_input(current,bool(turn['input_batch_id']))},
                'desktop_v8_layout')
        visible=[messages[0]]
        entries=[]
        def add(event, origin, address):
            row=db.execute('SELECT seq,role,session_id,persona_id FROM events WHERE id=?',(event['id'],)).fetchone()
            require(row and {'persona':'assistant','user':'user'}.get(row['role'])==event['role'],
                    'desktop_message_identity')
            entries.append({'event_id':event['id'],'text':event['text'],'sequence':row['seq'],'role':event['role'],
                            'session_id':row['session_id'],'persona_id':row['persona_id'],'source':origin,
                            'pro_target':address,'displayed':True})
        ref_block,manifest=references(db,turn)
        require(manifest==details['references'],'desktop_reference_changed')
        if ref_block:
            require(ref_block in messages[0]['content'],'desktop_reference_missing')
            for group in json.loads(ref_block.split('\n',2)[2])['turns']:
                row=db.execute('SELECT * FROM turns WHERE id=?',(group['turn_id'],)).fetchone()
                require(row and row['session_id']==turn['session_id'] and row['persona_id']==turn['persona_id'],
                        'desktop_reference_scope')
                for e in group['replies']:
                    require(e['id']==row['reply_event_id'] and self.ledger.prefix(dict(row), e['text'])==e['text'],
                            'desktop_reference_not_visible')
                for e in [*group['members'],*group['replies']]:
                    if any(t['kind']=='turn' or t['id']==e['id'] for t in group['targets']):
                        role=e.get('role','assistant')
                        add(e|{'role':role},'reference',{'message_index':0,'reference_event':e['id'],'role':role})
        for i,g in enumerate(groups):
            source=read_exact(db,turn_scope(db,turn),{'id':g['turn_id'],'kind':'turn'})
            require(source.get('usable') and source['session_id']==turn['session_id'] and len(source['replies'])==1,
                    'desktop_history_scope')
            row=dict(db.execute('SELECT * FROM turns WHERE id=?',(g['turn_id'],)).fetchone())
            reply=source['replies'][0]
            user,spoken=messages[1+2*i:3+2*i]
            require(user=={'role':'user','content':render_input(source['members'],bool(row['input_batch_id']))}
                    and spoken=={'role':'assistant','content':reply['text']},'desktop_history_changed')
            loc=len(visible)
            visible.append(user)
            if g['kind']=='recent':
                for n,e in enumerate(source['members'],1):
                    add(e,'recent',{'message_index':loc,'role':'user','member':n})
            prefix=self.ledger.prefix(row,reply['text'])
            if prefix:
                loc=len(visible)
                visible.append({'role':'assistant','content':prefix})
                if g['kind']=='recent':
                    add({'id':reply['id'],'role':'assistant','text':prefix},'recent',
                        {'message_index':loc,'role':'assistant'})
        for n,e in enumerate(current,1):
            add(e,'current',{'message_index':len(visible),'role':'user','member':n})
        visible.append(messages[-1])
        return spec,visible,target_list(turn['persona_id'],turn['session_id'],entries)

    async def decide(self, turn, scope, targets, wire, recheck, *, model=None):
        tid=turn['id']; ledger=self.ledger
        with ledger.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM decisions WHERE turn=?',(tid,)).fetchone()
            if old:
                require(old['scope']==scope and json.loads(old['wire'])==wire,'desktop_replay_changed')
                require(old['reason'] is not None,'desktop_already_in_flight')
                return old['reason'],json.loads(old['choice']) if old['choice'] else None
            db.execute('INSERT INTO decisions(turn,scope,wire,started) VALUES(?,?,?,?)',
                       (tid,scope,json.dumps(wire,ensure_ascii=False),0))
        window=DecisionWindow(scope,targets)
        started=asyncio.Event()
        def save_raw(raw, *, http_status, diagnostics):
            parsed=parse_tool_receipt(bytes(raw),wire) if wire.get('tools') else parse_receipt(bytes(raw),False)
            body=parsed['body'] if parsed['valid'] and parsed['model']==wire['model'] and http_status==200 else ''
            window.received(scope,body)
            settled=window.result.result()
            with ledger.db() as db:
                db.execute("""UPDATE decisions SET raw=?,http=?,elapsed_ms=?,usage=?,
                    choice=CASE WHEN reason IS NULL THEN ? ELSE choice END, reason=coalesce(reason,?)
                    WHERE turn=? AND raw IS NULL""",
                           (bytes(raw),http_status,1000*(window.clock()-window.started),json.dumps(parsed['usage']),
                            json.dumps(settled.decision,ensure_ascii=False) if settled.decision else None,settled.reason,tid))
        async def send():
            try:recheck()
            except Exception:
                window.cancel();started.set()
                return
            with ledger.db() as db:
                db.execute('UPDATE decisions SET started=? WHERE turn=?',(time.time(),tid))
            window.dispatched()
            started.set()
            try:
                await self.transport.complete(model or self.config,wire,save_raw=save_raw)
            except Exception:
                if not window.result.done():window.received(scope,'')
        job=asyncio.create_task(send())
        self.jobs.add(job)
        def done(task):
            self.jobs.discard(task)
            if not task.cancelled():task.exception()
        job.add_done_callback(done)
        try:
            await started.wait()
            result=await window.resolve()
        except asyncio.CancelledError:
            window.cancel()
            if not started.is_set():job.cancel()
            with ledger.db() as db:
                db.execute("UPDATE decisions SET reason='cancelled' WHERE turn=? AND reason IS NULL",(tid,))
            raise
        with ledger.db() as db:
            db.execute('UPDATE decisions SET reason=?,choice=? WHERE turn=? AND reason IS NULL',
                       (result.reason,json.dumps(result.decision,ensure_ascii=False) if result.decision else None,tid))
        require(result.reason!='cancelled','desktop_decision_cancelled')
        return result.reason,result.decision


class DecisionContext:
    def __init__(self, original, owner):
        self.original=original
        self.owner=owner

    def __getattr__(self,name):
        return getattr(self.original,name)

    def finalize(self, conn, turn, prepared):
        memo=prepared.get('_d3')
        if memo is None:return self.original.finalize(conn,turn,prepared)
        self.owner.check(conn,turn)
        base=memo['base']
        require(self.original._guard(conn,turn,base['details'])==base['guard'],'desktop_sources_changed')
        if prepared.get('_d3_sealed'):return prepared
        final=self.original.finalize(conn,turn,{**base,'manifest':deepcopy(base['manifest'])})
        require(final['messages']==base['messages'],'desktop_base_changed')
        messages=memo['messages']; details=dict(final['details'])
        info={'reason':memo['reason'],'scope':memo['scope'],'base_messages_sha256':fingerprint(base['messages']),
              'visible_messages_sha256':memo['visible_hash'],'final_messages_sha256':fingerprint(messages)}
        details['en5_d3']=info
        if messages!=base['messages']:
            raw=deepcopy(details['prompt_assembly'])
            blocks=[b for b in raw['blocks'] if b['role']=='system']
            suffix=messages[0]['content'][len(base['messages'][0]['content']):]
            if suffix:
                blocks.append({'name':'reply_decision','owner':'en5_d3','version':'d3-v1','message_index':0,
                               'role':'system','start_character':len(base['messages'][0]['content']),
                               'characters':len(suffix),'present':True,'content_hash':fingerprint(suffix)})
            for i,m in enumerate(messages[1:],1):
                blocks.append({'name':'current_input' if i==len(messages)-1 else 'history','owner':'en5_d3',
                               'version':'presented-v1','message_index':i,'role':m['role'],'start_character':0,
                               'characters':len(m['content']),'present':True,'content_hash':fingerprint(m['content'])})
            raw.update(blocks=blocks,input_snapshot_hash=fingerprint(messages),message_count=len(messages),
                       input_characters=sum(len(m['content']) for m in messages))
            if 'character_consumption' in raw:raw['character_consumption']['final_input_hash']=fingerprint(messages)
            details['prompt_assembly']=raw
        manifest=self.original._manifest(conn,turn,messages,details)
        manifest.update(context_id=final['manifest']['context_id'],en5_d3=info)
        return {**final,'_d3':memo,'_d3_sealed':True,'messages':messages,'details':details,'manifest':fit_manifest(manifest)}


class D3Engine(ChatEngine):
    @staticmethod
    def check_transports(config, client, ledger, flash_client):
        require(getattr(client,'d3_offline_fixture',False) is True and getattr(flash_client,'d3_offline_fixture',False) is True
                and ledger.binding['mode']=='local_fixture'
                and not config.load()['model'].get('encrypted_key'),'local_fixture_required')

    def bind_stage(self, turn, stage, model, messages_or_wire, recheck):
        pass

    def __init__(self, store, config, client, *, ledger, flash_client, notes, **kwargs):
        self.check_transports(config, client, ledger, flash_client)
        super().__init__(store,config,client,**kwargs)
        self.ledger=ledger; self.notes=dict(notes)
        self.decision_mode=ledger.binding.get('decision_mode','json-object-v1')
        require(self.decision_mode in ('json-object-v1',MODE),'desktop_decision_mode')
        self.turn_guard=ContextVar('d3_active_turn_guard')
        flash={'base_url':'https://api.deepseek.com','name':'deepseek-flash','max_tokens':128,'_json_output':True}
        self.decision=DesktopDecision(store,ledger,flash_client,flash)
        self.cognitive=DecisionContext(self.cognitive,self.decision)
        self.generation_executor=self.first_generation

    async def first_generation(self, client, model, messages, *, before_attempt, **kwargs):
        def first(state):
            require(not state['attempts'] and not state['prior_failures'],'desktop_first_main_only')
            before_attempt(state)
        completed=await audited_complete(client,model,messages,before_attempt=first,paths=self.store.paths,**kwargs)
        self.turn_guard.get()()
        return completed

    async def _prepare_context(self, tid, *args):
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            self.decision.check(db,turn)
            require(not db.execute('SELECT 1 FROM attempts WHERE turn_id=?',(tid,)).fetchone(),'desktop_main_already_attempted')
            require(not expression_enabled(db,tid) and not self.direct.generation.enabled(db,tid),
                    'desktop_other_generation_enabled')
            require(not (self.knowledge.processing and self.knowledge.processing.enabled(turn)),'desktop_web_enabled')
        base=await super()._prepare_context(tid,*args)
        model=json.loads(turn['model_snapshot'])
        require(model['name']=='deepseek-v4-pro' and not model.get('encrypted_key'),'local_fixture_required')
        with self.store.connection() as db:
            self.decision.check(db,dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone()))
            spec,visible,targets=self.decision.visible_material(db,turn,base)
        scope_parts=[self.ledger.binding['version'],tid,turn['persona_id'],turn['session_id'],
                     turn['execution_revision'],base['guard'],fingerprint(visible),targets,self.notes]
        if self.decision_mode==MODE:
            scope_parts.append(MODE)
        scope=fingerprint(scope_parts)
        def recheck():
            with self.store.connection() as db:
                current=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                self.decision.check(db,current)
                require(current['execution_revision']==turn['execution_revision']
                        and self.cognitive._guard(db,current,base['details'])==base['guard'],'desktop_sources_changed')
        self.turn_guard.set(recheck)
        reason='baseline'; messages=visible
        if spec['branch']=='candidate':
            profile=persona_projection(json.loads(turn['profile_snapshot']))
            flash_source=flash_base(model,profile,targets)
            if self.decision_mode==MODE:
                decision_model,wire=decision_pair(flash_source,self.notes['flash'],targets,model['timeout'])
            else:
                decision_model,wire=None,flash_wire(flash_source,self.notes['flash'])
            require(sum(len(m['content']) for m in wire['messages'])<=min(60000,model['max_input_chars']),'desktop_flash_capacity')
            self.bind_stage(turn,'decision',decision_model or self.decision.config,wire,recheck)
            reason,choice=await self.decision.decide(turn,scope,targets,wire,recheck,model=decision_model)
            if choice is not None:
                messages=pro_wire({'messages':visible},self.notes['pro'],choice,targets)['messages']
        require(sum(len(m['content']) for m in messages)<=model['max_input_chars'],'desktop_pro_capacity')
        self.bind_stage(turn,'main',model,messages,recheck)
        return {**base,'_d3':{'base':base,'scope':scope,'messages':messages,
                              'reason':reason,'visible_hash':fingerprint(visible)}}

    async def wait(self):
        await super().wait()
        if self.decision.jobs:
            await asyncio.gather(*list(self.decision.jobs),return_exceptions=True)


def local_app(paths, binding, main_client, flash_client, notes):
    """Create an isolated pair of local transports. Real continuous use has its own later grant."""
    ledger=DesktopLedger(paths,binding)
    def create(store,config,client,**kwargs):
        require(str(paths.home.resolve())==binding['home'] and config.load()['instance_id']==binding['instance_id'],
                'desktop_binding')
        return D3Engine(store,config,client,ledger=ledger,flash_client=flash_client,notes=notes,**kwargs)
    ownership=InstanceLock(ledger.path.parent/'engine.lock')
    try:
        app=create_app(paths,client=main_client,foreground_only=True,engine_factory=create)
    except BaseException:
        ownership.close()
        raise
    async def release(_):
        ownership.close()
    app.on_cleanup.append(release)
    return app,ledger
