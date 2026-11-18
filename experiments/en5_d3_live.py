"""Isolated, author-bound D3 desktop replay. Neither instance stores a provider credential."""
import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import time

from aiohttp import web
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ModelCompletion, ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.continuous_decision_use import current
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.deepseek_reply_decision import BASE
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, guard_cloud_dispatch, provider_identity
from persona_runtime.profile import snapshot as profile_snapshot
from persona_runtime.provider_compat import build_request
from persona_runtime.server import create_app
from persona_runtime.sessions import SessionService
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .en5_decision_contract import require
from .en5_decision_d2 import register
from .en5_decision_packet import file_hash
from .en5_decision_tool import MODE, DecisionToolHTTPClient
from .en5_d3_desktop import D3Engine
from .en5_d3_material import prepare as verify_material
from .en5_d3_presentation import D3Workbench, DesktopLedger, close_local_window
from .en5_experience_replay import plan_for, snapshot
from .en5_experience_setup import MANIFEST, binding_at, prepare


def source_inputs(root):
    state=read_private(root.parent/'prompt-review-state.json')
    materials=root/'d3/materials/materials.proposed.private.json'
    _, checksum, page_hash=verify_material(root)
    summary=read_private(Path(state['en5_decision_d2_strict_outcome_summary']))
    engineering=read_private(Path(state['en5_decision_d3_engineering_evidence']))
    require(checksum==file_hash(materials)==engineering['d3_materials_sha256']
            and page_hash==file_hash(Path(state['en5_decision_d3_material_review']))
            and file_hash(Path(state['en5_decision_d2_strict_results']))==summary['result_sha256']==state['en5_decision_d2_strict_results_sha256']
            and summary['contract_sha256']==state['en5_decision_d2_strict_contract_sha256']
            and file_hash(Path(state['en5_decision_confirmed_requests']))==state['en5_decision_confirmed_requests_sha256'],
            'continuous_input_binding')
    packet=read_private(materials)
    authorization={'version':'d3-continuous-use-v1','approved':True,'material_sha256':checksum,
        'material_review_sha256':page_hash,'d2_results_sha256':summary['result_sha256'],
        'd2_contract_sha256':summary['contract_sha256'],'notes_source_sha256':state['en5_decision_confirmed_requests_sha256'],
        'roles':['main','dialogue_decision'],'sessions_per_arm':2,'turns_per_session':15,'web_enabled':False}
    return state,packet,authorization


def setup(root, branch):
    require(branch in ('baseline','candidate'),'branch')
    state,material,expected=source_inputs(root)
    paths,binding,plans,prior=setup_branch(root,root/'d3/live',branch,material,expected,{'decision_mode':MODE})
    notes=read_private(Path(state['en5_decision_confirmed_requests']))['author_text']
    return paths,binding,plans,prior,notes,material


def replay_plans(script,binding,checksum,selection=None):
    if selection is None:
        return {sid:plan_for(script,binding,i,checksum) for i,sid in enumerate(binding['sessions'],1)}
    part,start,upto=selection['part'],selection.get('from',1),selection['through']
    require(part in (1,2) and type(start) is int and type(upto) is int and 1<=start<=upto<=15,'targeted_replay_bounds')
    return {binding['sessions'][part-1]:plan_for(script,binding,part,checksum)[start-1:upto]}


def setup_branch(root,parent,branch,material,expected,stage_fields,*,web_providers=None):
    grant_path=parent/'author-use.private.json'; grant=read_private(grant_path)
    require({k:grant.get(k) for k in expected}==expected and grant.get('author_message'),'continuous_use_not_approved')
    prior=outside_repository(read_private(Path(read_private(root/'material.private.json')['prior'])/'input-contract.private.json')['source'])
    prior_paths, prior_scope=binding_at(prior)
    branch_root=parent/branch
    prepare(branch_root,root.parent/'prompt-review-state.json',Path(prior_scope['source_home']),mode='author_material_replay')
    paths,b=binding_at(branch_root)
    store=Store(paths); cfg=ConfigStore(paths).load(); pid=b['persona_id']
    require(not cfg['model'].get('encrypted_key') and not cfg['organization']['daily_enabled'],'unisolated_live_home')
    with store.connection() as db, Store(prior_paths).connection() as old:
        profile=profile_snapshot(db,pid)
        reference=profile_snapshot(old,prior_scope['persona_id'])
        require({k:v['data'] for k,v in profile.items()}=={k:v['data'] for k,v in reference.items()}
                and b['character_hash']==prior_scope['character_hash'] and b['spec_hash']==prior_scope['spec_hash'],'persona_copy_drift')
    # The active desktop receives only these original user bubbles.
    script={'sessions':[{'part':n,'turns':[{'n':t['n'],'bubbles':[{'text':x['text']} for x in t['bubbles']],
             'text':'\n'.join(x['text'] for x in t['bubbles'])} for t in s['turns']]}
             for n,s in enumerate(material['sessions'],1)]}
    if web_providers:
        require(expected['web_enabled'] and set(web_providers)=={'knowledge_router','knowledge_native'},'web_use_scope')
        for part,original in zip(script['sessions'],material['sessions'],strict=True):
            for row,source in zip(part['turns'],original['turns'],strict=True):
                row['public_task']=source['public_task']
        # Only the scoped transport can borrow credentials at dispatch.
        save_once(paths.home/'private/knowledge-router/config.json',
                  web_providers['knowledge_router']|{'encrypted_key':'scoped-d4-credential-reference'})
    msha=save_once(branch_root/'materials.private.json',script)
    plans=replay_plans(script,b,msha,expected.get('replay_slice'))
    sessions={sid:{'branch':branch,'display':'bubbles','inputs':[[fingerprint(x['text']) for x in g] for g in groups]}
              for sid,groups in plans.items()}
    contract={'parent_use_sha256':file_hash(grant_path),'branch':branch,'material_sha256':msha,
              'sessions':sessions,'instance_id':b['instance_id'],'persona_id':pid}
    csha=save_once(branch_root/'input-contract.private.json',contract)
    use={**expected,**contract,'author_message':grant['author_message'],'sessions':b['sessions'],
         'scope_sha256':file_hash(paths.home/MANIFEST),'input_contract_sha256':csha,
         'providers':{'main':{k:cfg['model'][k] for k in ('base_url','name')},**(web_providers or {})}}
    usha=save_once(branch_root/'author-use.private.json',use)
    slots={sid:[{'request_id':x['request_id'],'n':x['n'],'i':x['i'],'text_sha256':digest(x['text'].encode()),
                 'public_task':x['public_task']} for group in groups for x in group] for sid,groups in plans.items()}
    ssha=save_once(branch_root/'approved-slots.private.json',{'approval_sha256':usha,'instance_id':b['instance_id'],'sessions':slots})
    binding={**contract,'version':'d3-reviewed-desktop-v1','mode':'reviewed_live','home':str(paths.home.resolve()),
             'character_version':CharacterService(store).current(pid)['id'],
             'profile_hash':fingerprint(profile),'author_use_sha256':usha,**stage_fields}
    marker='d4-binding.json' if web_providers else 'd3-binding.json'
    bh=save_once(paths.home/'runtime'/marker,binding)
    private=PrivatePersonas(paths)
    record=private.record_for_persona(pid)
    operation='d3-main-'+usha
    fields={'en5_slots_sha256':ssha,'en5_approval_sha256':usha,('d4_binding_sha256' if web_providers else 'd3_binding_sha256'):bh}
    seal=branch_root/'activation.private.json'
    with store.connection() as db:
        started=db.execute('SELECT 1 FROM ingress_receipts LIMIT 1').fetchone()
    if seal.exists() or started:
        require(all(record.get(k)==v for k,v in fields.items()) and private.main_authorized(pid,record,cfg['model']),
                'continuous_grant_withdrawn')
        require(all(private.model_authorized(pid,record,kind,provider) for kind,provider in
                    (web_providers or {}).items()),'web_grant_withdrawn')
    else:
        require(all(record.get(k) in (None,v) for k,v in fields.items()),'continuous_grant_changed')
        private.authorize_main(store,pid,provider_identity(cfg['model']),operation)
        for purpose,provider in (web_providers or {}).items():
            private.authorize_model(store,pid,purpose,provider_identity(provider),purpose+'-'+usha)
        require(private.main_authorized(pid,record,cfg['model']),'continuous_grant_withdrawn')
        private.save_record(record|fields)
    save_once(seal,{'operation':operation,'binding_sha256':bh})
    return paths,binding,plans,prior_paths


class Admission:
    def __init__(self,root,paths,binding,prior_paths,material,ledger,*,marker='d3-binding.json'):
        self.root=root; self.paths=paths; self.binding=binding; self.store=Store(paths)
        self.prior_paths=prior_paths; self.material=material; self.ledger=ledger
        self.folder=paths.home.parent
        self.marker=paths.home/'runtime'/marker
        self.bh=file_hash(self.marker)
        self.use=self.folder.parent/'author-use.private.json'
        self.corpus=DatasetCorpusStore(material['corpus_root'])
        with ledger.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS main_calls(turn TEXT PRIMARY KEY,wire TEXT NOT NULL,started REAL NOT NULL,
                raw BLOB,http INTEGER,diagnostics TEXT)""")

    def check(self):
        require(file_hash(self.use)==self.binding['parent_use_sha256']
                and file_hash(self.folder/'author-use.private.json')==self.binding['author_use_sha256']
                and file_hash(self.marker)==self.bh,'continuous_use_changed')
        with self.corpus.connection() as db:
            view=self.corpus._view_status(db,self.material['view_id'])
            require(view['source_proof_hash']==self.material['source_proof_sha256'] and
                    view['payload_hash']==self.material['view_sha256'],'continuous_sources_changed')

    def bind(self,turn,stage,model,wire,recheck):
        self.check()
        role='main' if stage=='main' else 'dialogue_decision'
        ctx={'home':self.binding['home'],'binding':self.bh,'session':turn['session_id'],'turn':turn['id'],
             'kind':role,'provider':provider_identity(model),'wire_hash':fingerprint(wire),'recheck':recheck}
        if stage=='decision':
            spec=self.binding['sessions'][turn['session_id']]
            require(spec['branch']=='candidate' and model['base_url']==BASE,'continuous_decision_route')
            slot=turn['id']+'.decision'
            permit={'purpose':role,'approved':True,'instance_id':self.binding['instance_id'],'persona_id':turn['persona_id'],
                    'slots':{slot:ctx['wire_hash']},'provider':ctx['provider'],'parent_use':self.binding['parent_use_sha256']}
            derived=self.folder/'decisions'/turn['id']
            save_once(derived/'author-use.private.json',permit|{'author_message':read_private(self.use)['author_message']})
            gid=register(derived,self.paths,permit)
            ctx['decision_use']=(gid,slot,wire)
        current.set(ctx)

    def credential(self,model,wire,stage):
        ctx=current.get()
        require(ctx and ctx['kind']==stage and ctx['wire_hash']==fingerprint(wire),'unbound_continuous_request')
        ctx['recheck']()
        guard_cloud_dispatch(self.store,self.binding['persona_id'],stage,model,decision_use=ctx.get('decision_use'))
        cfg=ConfigStore(self.prior_paths).load()['model']
        require(cfg.get('encrypted_key') and cfg['name']=='deepseek-v4-pro' and
                (provider_identity(cfg)==provider_identity(model) if stage=='main' else model['base_url']==BASE),'continuous_credential_route')
        return model|{'encrypted_key':cfg['encrypted_key']}


class MainTransport:
    def __init__(self,auth,*,present=None):
        self.auth=auth; self.http=GenerationHTTPClient(); self.present=present

    async def complete(self,model,messages):
        wire=build_request(model,messages)[0]
        live=self.auth.credential(model,wire,'main'); tid=current.get()['turn']
        self.http.preflight(live,wire)
        with self.auth.ledger.db() as db:
            db.execute('INSERT INTO main_calls(turn,wire,started) VALUES(?,?,?)',(tid,json.dumps(wire,ensure_ascii=False),time.time()))
        body={}
        def keep(raw,*,http_status,diagnostics):
            with self.auth.ledger.db() as db:
                db.execute('UPDATE main_calls SET raw=?,http=?,diagnostics=? WHERE turn=? AND raw IS NULL',
                           (bytes(raw),http_status,json.dumps(diagnostics),tid))
            body.update(parse_receipt(raw,wire['stream']))
            body.update(http=http_status,diagnostics=diagnostics)
        await self.http.complete(live,wire,save_raw=keep)
        if body.get('http')!=200 or not body.get('valid') or body.get('model')!=wire['model']:
            raise ModelError('d3_main_first_receipt_invalid',usage=body.get('usage'))
        current.get()['recheck']()
        guard_cloud_dispatch(self.auth.store,self.auth.binding['persona_id'],'main',model)
        spoken=self.present(tid,body['body']) if self.present else body['body']
        return ModelCompletion(spoken,body['usage'],body['diagnostics']|{'finish_reason':body['finish_reason'],
                               'response_model':body['model']})


class FlashTransport:
    def __init__(self,auth):
        self.auth=auth; self.http=DecisionToolHTTPClient()
    async def complete(self,model,wire,*,save_raw):
        live=self.auth.credential(model,wire,'dialogue_decision')
        return await self.http.complete(live,wire,save_raw=save_raw)


class LiveEngine(D3Engine):
    @staticmethod
    def check_transports(config,client,ledger,flash_client):
        require(type(client) is MainTransport and type(flash_client) is FlashTransport
                and client.auth is flash_client.auth and ledger is client.auth.ledger
                and ledger.binding['mode']=='reviewed_live' and not config.load()['model'].get('encrypted_key'),
                'continuous_transport_scope')
    def bind_stage(self,turn,stage,model,data,recheck):
        if stage=='main':
            model=dict(model,_reply_mode=turn['reply_mode_snapshot'],_expression_snapshot=turn.get('expression_snapshot'))
            wire=build_request(model,data)[0]
        else: wire=data
        def check():
            recheck()
            self.client.auth.check()
        self.client.auth.bind(turn,stage,model,wire,check)


async def drive(root,branch):
    paths,binding,plans,prior,notes,material=setup(root,branch)
    ledger=DesktopLedger(paths,binding)
    auth=Admission(root,paths,binding,prior,material,ledger)
    client,flash=MainTransport(auth),FlashTransport(auth)
    def factory(store,config,main,**kwargs):
        return LiveEngine(store,config,main,ledger=ledger,flash_client=flash,notes=notes,**kwargs)
    await replay_workbench(paths,binding,plans,auth,ledger,factory,client,D3Workbench,branch)


async def replay_workbench(paths,binding,plans,auth,ledger,factory,client,window,branch,*,configure=None,resume_local=None):
    owner=InstanceLock(paths.home/'runtime/service.lock')
    runner=None;w=None
    try:
        app=create_app(paths,client=client,foreground_only=True,engine_factory=factory)
        if configure is not None:
            configure(app)
        runner=web.AppRunner(app,access_log=None);await runner.setup()
        await web.TCPSite(runner,'127.0.0.1',0).start()
        ConfigStore(paths).update({'service_port':runner.addresses[0][1]})
        w=window(paths,ledger);w.title('D3 · '+branch);w.geometry('940x740+20000+20000')
        errors=[];w.report_callback_exception=lambda *exc:errors.append(str(exc[1]))
        w.show_page('chat')
        async def until(predicate,timeout=360):
            async with asyncio.timeout(timeout):
                while True:
                    w.update()
                    require(not errors and not w.closed,'desktop_ui_failed')
                    result=predicate()
                    if result:return result
                    await asyncio.sleep(.035)
        all_sessions=read_private(paths.home/MANIFEST)['sessions']
        for sid,plan in plans.items():
            part=all_sessions.index(sid)+1
            def progress():
                return snapshot(paths,binding,sid,plan)
            accepted,batches,turns=progress()
            def painted(tid):
                with ledger.db() as db:
                    row=db.execute('SELECT frames,stopped FROM plans WHERE turn=?',(tid,)).fetchone()
                    if not row:return False
                    require(not row['stopped'],'continuous_presentation_stopped')
                    seen=db.execute('SELECT count(*) FROM visible WHERE turn=?',(tid,)).fetchone()[0]
                    return seen==len(json.loads(row['frames']))
            if len(turns)==len(plan) and all(t['status']=='completed' and painted(t['id']) for t in turns.values()):
                continue
            if part==2:
                manager=SessionService(auth.store)
                head=manager.current(binding['persona_id'],'desktop')
                if head['session_id']==all_sessions[0]:
                    manager.seal(head['session_id'],binding['persona_id'],head['revision'],'d3-part-one-complete','desktop')
            w.load_chat(session_id=sid)
            await until(lambda:w.session_id==sid,30)
            await until(lambda:not any('persona-read' in op.resources for op in w.hub.active.values()),30)
            w.refresh_personas()
            await until(lambda:w.session_id==sid and w.chat_persona.current()>=0)
            for group in plan:
                n=group[0]['n']
                auth.check()
                accepted,batches,turns=progress()
                prior_turn=turns.get(batches.get(n))
                if prior_turn:
                    if resume_local and prior_turn['status']=='failed':
                        await resume_local(app,prior_turn)
                        _,_,refreshed=progress()
                        prior_turn=refreshed[batches[n]]
                    require(prior_turn['status'] not in ('interrupted','failed'),'continuous_prior_attempt_stopped')
                    if prior_turn['status']=='completed' and painted(prior_turn['id']):continue
                for x in group:
                    if x['request_id'] in accepted:continue
                    await until(lambda:not any(op.resources & {'send','chat','persona','chat-cancel','emergency'} for op in w.hub.active.values()))
                    require(w.session_id==sid and not w.chat_input.get('1.0','end-1c') and not w.chat_reference and not w.chat_public_task,
                            'continuous_draft_changed')
                    if x['public_task'] is not None:
                        w.chat_public_task={'persona_id':binding['persona_id'],'session_id':sid,'task':x['public_task']}
                    w.chat_input.insert('1.0',x['text']);op=w.send_chat(request_id=x['request_id'])
                    require(op is not None,'continuous_submit_rejected')
                    def arrived():
                        require(op.state!='failed','continuous_submit_failed')
                        return op.state=='completed' and not w.chat_input.get('1.0','end-1c')
                    await until(arrived)
                    accepted,batches,turns=progress()
                    require(x['request_id'] in accepted,'continuous_input_missing')
                def finished():
                    _,bs,ts=progress()
                    t=ts.get(bs.get(n))
                    if t and t['status'] in ('failed','interrupted'):raise RuntimeError('continuous_turn_failed '+str(t['error_code']))
                    if t and t['status']=='completed' and not painted(t['id']):
                        marks=w.en5_display.marks.get(t['id'])
                        if marks and marks[3]:
                            span=w.transcript.tag_ranges(marks[3])
                            if span and not w.transcript.bbox(span[0]):w.transcript.see(span[0])
                    return t and t['status']=='completed' and painted(t['id'])
                await until(finished)
                print(branch,part,n,'visible',flush=True)
        await app['engine'].wait()
    finally:
        try:
            if w is not None:await close_local_window(w)
        finally:
            try:
                if runner:await runner.cleanup()
            finally:owner.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--branch',choices=('baseline','candidate'),required=True)
    args=parser.parse_args()
    asyncio.run(drive(outside_repository(args.root),args.branch))
