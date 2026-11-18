"""D4 with the accepted scene voice and the two previously approved public questions."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelCompletion, ModelError
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_store import outside_repository
from persona_runtime.deepseek_native_transport import DeepSeekNativeClient
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.knowledge_native import public_task
from persona_runtime.knowledge_native_route import flash_model
from persona_runtime.knowledge_routing import binding as provider_binding, configured_model
from persona_runtime.knowledge_web_decision import request as web_request
from persona_runtime.knowledge_web_transport import request as web_tool
from persona_runtime.models import identifier
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.provider_compat import build_request
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .en5_d3_live import Admission, MainTransport, replay_workbench, setup_branch
from .en5_decision_packet import file_hash
from .en5_experience_material import verify_sources
from .en5_experience_setup import binding_at, validate_use
from .en5_jev_transport import JevTransport
from .en5_scene_continuous import (ContinuousEngine,CopyCards,LiveChoice,REPAIR,
                                   ShortChatLedger,reviewed,style_review)
from .en5_scene_desktop import SceneWorkbench
from .en5_scene_route import SceneRouter
from .en5_scene_style import VERSION as STYLE_VERSION,speech,without_directions
from .en5_scene_voice import require

STUDY='en5-jev-d4-web-v1'
TARGETED_STUDY='en5-jev-d4-targeted-reuse-v1'
AUTHOR_QUERY_STUDY='en5-jev-d4-author-query-v1'


def materials(root,feedback,*,targeted=False,author_input=None):
    state,old,rev,use,_=reviewed(root)
    src=rev/'continuous-style-repair'
    result=src/'results.private.json'
    key=read_private(src/'blind-key.private.json')
    opinion=read_private(feedback)
    require(opinion['review_version']==REPAIR and opinion['version']=='en5-scene-continuous-author-feedback-v1'
            and opinion['results_sha256']==key['results']==file_hash(result)==state['en5_scene_style_repair_results_sha256']
            and opinion['materials_sha256']==use['material_sha256']
            and opinion['key_sha256']==file_hash(src/'blind-key.private.json'),'d4_style_review')
    require([g['session'] for g in opinion['groups']]==[1,2] and all(
        key['groups'][i].get(g['preference'])=='candidate' for i,g in enumerate(opinion['groups'])),'d4_author_preference')
    style_review(rev,use,src/'style-review.private.json')
    stamp=save_once(src/'author-feedback/submitted.private.json',opinion)
    stage=save_once(src/'author-feedback/stage-review.private.json',{'version':STUDY,'author':'审阅通过，继续下一步的开发。',
        'feedback_sha256':stamp,'stage':'style_passed','preferences':['candidate','candidate']})
    original=outside_repository(state['en5_root'])
    raw=verify_sources(original)
    old_grant=validate_use(original,require_live=True)
    selection=read_private(original/'selection.private.json')
    material=dict(raw,view_id=raw['source_view_id'],view_sha256=raw['source_view_sha256'],
                  source_proof_sha256=raw['source_proof_hash'],corpus_root=selection['corpus_root'])
    public=[[i,t['n'],t['public_task']] for i,s in enumerate(raw['sessions'],1) for t in s['turns'] if t.get('public_task')]
    require(len(public)==2 and {v[0] for v in public}=={1,2} and all(n<15 for _,n,_ in public),'previous_public_slots')
    d4=rev/'d4-approved-material'
    approval={'version':STUDY,'approved':True,'web_enabled':True,'roles':['main','scene_decision','knowledge_router','knowledge_native'],
              'style_profile':STYLE_VERSION,'scene_revision':use['scene_revision'],'jev_model':'jev-1.14',
              'stage_review_sha256':stage,'old_use_sha256':old_grant,'material_sha256':file_hash(original/'materials.private.json'),
              'source_contract_sha256':file_hash(original/'input-contract.private.json'),
              'fixed_results_sha256':use['fixed_results_sha256'],'sessions_per_arm':2,'turns_per_session':15,
              'approved_public_slots':public,'author_message':'沿用已授权的旧材料，按 Jev 的实际判断验收'}
    if targeted or author_input is not None:
        old_result=d4/'verified/results.private.json'
        repair=outside_repository(state['en5_scene_d4_reuse_repair_verification'])
        require(file_hash(old_result)==state['en5_scene_d4_results_sha256']
                and file_hash(repair)==state['en5_scene_d4_reuse_repair_verification_sha256'],'d4_targeted_source')
        earlier=read_private(old_result)
        selected=[slot for slot in public if slot[0]==2]
        require(len(selected)==1 and earlier['summary']['search_positions']==['1-11']
                and earlier['summary']['reuse_positions']==['1-12'],'d4_targeted_missing_search')
        part,at,_=selected[0]
        require(earlier['groups'][part-1][at-1]['designated_search']
                and earlier['groups'][part-1][at-1]['web_choice']=='none','d4_targeted_prior_decision')
        d4=rev/'d4-targeted-reuse'
        approval.update(version=TARGETED_STUDY,approved_public_slots=selected,sessions_per_arm=1,
                        turns_per_session=at+1,replay_slice={'part':part,'through':at+1},
                        original_d4_results_sha256=file_hash(old_result),reuse_repair_sha256=file_hash(repair),
                        author_message='按照计划，继续进行定向补验。')
        if author_input is not None:
            request=read_private(author_input)
            targeted_result=d4/'verified/results.private.json'
            require(file_hash(targeted_result)==state['en5_scene_d4_targeted_results_sha256']
                    and read_private(targeted_result)['groups'][0][at-1]['web_choice']=='none',
                    'd4_explicit_followup_source')
            text=request['author_reply']
            require(request['version']=='en5-d4-author-query-v1' and isinstance(text,str) and text.strip()==text
                    and 0<len(text)<=240 and request['public_slot_sha256']==fingerprint(selected[0]),
                    'd4_explicit_author_input')
            material=deepcopy(material)
            turn=material['sessions'][part-1]['turns'][at-1]
            turn['bubbles'].append({'text':text})
            turn['text']='\n'.join(b['text'] for b in turn['bubbles'])
            d4=rev/'d4-author-explicit-query'
            receipt=save_once(d4/'author-input.private.json',request)
            approval.update(version=AUTHOR_QUERY_STUDY,turns_per_session=2,
                            replay_slice={'part':part,'from':at,'through':at+1},author_message=text,
                            author_input_sha256=receipt,targeted_results_sha256=file_hash(targeted_result))
    return state,rev,original,material,d4,approval


class D4Speech(MainTransport):
    async def complete(self,model,messages):
        if model.get('_search_notice_profile'):
            return await self.managed.notice(model,messages)
        return await super().complete(model,messages)


class D4Engine(ContinuousEngine):
    web_enabled=True
    main_client_type=D4Speech


class D4Managed:
    def __init__(self,auth,scene,main,plans):
        self.auth,self.scene,self.main=auth,scene,main
        self.plans=plans
        self.org=None
        self.web=JevTransport(scene.transport.model)
        self.native=DeepSeekNativeClient()
        with auth.ledger.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS d4_calls(
                attempt TEXT PRIMARY KEY, task TEXT, turn TEXT, stage TEXT, wire TEXT, started REAL,
                raw BLOB, http INTEGER, diagnostics TEXT, ended REAL)""")

    def current(self,stage,model):
        self.auth.check()
        with self.auth.store.connection() as db:
            rows=db.execute("""SELECT t.*,a.id AS aid FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id
                WHERE t.status='running' AND a.status='running' AND json_extract(t.payload_json,'$.knowledge_processing.stage')=?""",
                            (stage,)).fetchall()
            require(len(rows)==1,'d4_active_task')
            task=dict(rows[0]);payload=json.loads(task['payload_json']);spec=payload['knowledge_processing']
            root=dict(db.execute('SELECT * FROM turns WHERE id=?',(spec['root_turn_id'],)).fetchone())
            require(root['status']=='queued' and not root['cancelled_at']
                    and root['persona_id']==task['persona_id']==self.auth.binding['persona_id']
                    and root['session_id']==task['session_id'] and root['session_id'] in self.plans
                    and self.auth.store.current(db)==root['persona_id']
                    and json.loads(task['model_snapshot'])=={k:v for k,v in model.items() if k!='_request_deadline'},'d4_scope')
            self.org.managed.check(db,task,self.org.managed.contract(db,task['id']),payload)
            self.scene.engine.sources(db,root)
            if stage!='conversation_web_decision':
                n=db.execute('SELECT count(*) FROM turns WHERE session_id=? AND rowid<(SELECT rowid FROM turns WHERE id=?)',
                             (root['session_id'],root['id'])).fetchone()[0]
                permitted=self.plans[root['session_id']][n][-1]['public_task']
                require(permitted is not None,'d4_no_public_slot')
                if stage in ('conversation_native','conversation_search_notice'):
                    require(public_task(spec['input']['public_task'])==permitted,'d4_public_projection')
                if stage!='conversation_search_notice':
                    with self.auth.ledger.db() as ledger:
                        require(ledger.execute('SELECT 1 FROM scene_notices WHERE turn=?',(root['id'],)).fetchone(),
                                'd4_notice_not_visible')
        return task,payload,root

    def begin(self,task,root,stage,wire):
        with self.auth.ledger.db() as db:
            db.execute('INSERT INTO d4_calls(attempt,task,turn,stage,wire,started) VALUES(?,?,?,?,?,?)',
                       (task['aid'],task['id'],root['id'],stage,json.dumps(wire,ensure_ascii=False),time.time()))

    def end(self,aid,raw,http,diagnostics):
        with self.auth.ledger.db() as db:
            db.execute('UPDATE d4_calls SET raw=?,http=?,diagnostics=?,ended=? WHERE attempt=? AND ended IS NULL',
                       (raw,http,json.dumps(diagnostics),time.time(),aid))

    async def routing(self,model,wire):
        stage='conversation_web_decision'
        task,payload,root=self.current(stage,model)
        require(wire==web_request(model,payload['knowledge_processing']['input'])
                and provider_identity(model)==provider_identity(self.web.model)
                and provider_binding(configured_model(self.auth.prior_paths))==provider_binding(self.scene.source),
                'd4_web_decision_wire')
        guard_cloud_dispatch(Store(self.auth.prior_paths),self.scene.registry.authority.persona,
                             'knowledge_router',self.scene.source)
        self.begin(task,root,stage,wire)
        diagnostic={}
        status,raw=await self.web.complete(wire,on_dispatch=lambda _:None,on_receipt=lambda _:None,diagnostics=diagnostic.update)
        self.end(task['aid'],raw.encode(),status,diagnostic)
        if status!=200:raise ModelError('jev_http_'+str(status))
        from persona_runtime.knowledge_web_decision import usage
        value=json.loads(raw)
        require(value.get('model')=='jev-1.14.0','d4_web_decision_model')
        return ModelCompletion(raw,usage(value),{'network_route':'system_proxy','http_status':status,
                               'elapsed_ms':diagnostic['wire_ms'],'request_headers_sent':True,'response_headers_received':True})

    def credentials(self,model):
        original=ConfigStore(self.auth.prior_paths).load()['model']
        require(original['base_url']==model['base_url'] and original.get('encrypted_key'),'d4_credentials')
        kind='knowledge_native' if model['name']=='deepseek-flash' else 'main'
        guard_cloud_dispatch(Store(self.auth.prior_paths),self.scene.registry.authority.persona,kind,model)
        return model|{'encrypted_key':original['encrypted_key']}

    async def notice(self,model,messages):
        stage='conversation_search_notice'
        task,payload,root=self.current(stage,model)
        with self.auth.store.connection() as db:
            expected=self.org.processing.search_notice.context(db,task,payload)['messages']
        require(expected==messages,'d4_notice_wire')
        wire=build_request(model,messages)[0];live=self.credentials(model)
        self.main.http.preflight(live,wire)
        self.begin(task,root,stage,wire)
        reply={}
        def keep(raw,*,http_status,diagnostics):
            self.end(task['aid'],bytes(raw),http_status,diagnostics)
            reply.update(parse_receipt(raw,wire['stream']),http=http_status,diagnostics=diagnostics)
        await self.main.http.complete(live,wire,save_raw=keep)
        self.current(stage,model)
        require(reply.get('http')==200 and reply.get('model')==wire['model'] and reply.get('valid'),'d4_notice_response')
        return ModelCompletion(without_directions(reply['body']),reply['usage'],reply['diagnostics']|{
            'finish_reason':reply['finish_reason'],'response_model':reply['model']})

    async def complete(self,model,messages):
        stage='conversation_native'
        task,payload,root=self.current(stage,model)
        with self.auth.store.connection() as db:
            expected,_=self.org.processing.native.messages_for_task(db,task,payload)
        require(expected==messages and model['name']=='deepseek-flash','d4_native_projection')
        wire=build_request(model,messages)[0]
        require(wire.get('thinking',{}).get('type')=='enabled' and wire.get('reasoning_effort')=='high'
                and 'deepseek_web_search' in json.dumps(wire.get('tools')),'d4_native_deep_search')
        self.begin(task,root,stage,wire)
        result=await self.native.complete(self.credentials(model),messages)
        self.end(task['aid'],result[0].encode(),result.diagnostics.get('http_status'),result.diagnostics)
        return result

    async def supplement(self,stage,values,model):
        prefix='conversation_'+stage
        task,payload,root=self.current(prefix,model)
        args=payload['knowledge_processing']['input']
        from persona_runtime.knowledge_general import public_url
        expected={'query':args['query'][:128],'count':8,'need_content':True} if stage=='search' else {'url':public_url(args['url'])}
        require(stage in ('search','fetch') and values==expected,'d4_supplement')
        self.begin(task,root,prefix,values)
        result=await web_tool(stage,values,self.credentials(model))
        self.end(task['aid'],result[0].encode(),result.diagnostics.get('http_status'),result.diagnostics)
        return result

    def configure(self,app):
        self.org=app['organization']
        self.org.routing.transport=self.routing
        self.org.client=self
        self.org.processing.tools.transport=self.supplement
        pid=self.auth.binding['persona_id'];service=self.org.processing
        reader=service.service.current_policy(pid);processing=service.policy(pid);web=self.org.web.policy(pid)
        activation=self.auth.folder/'web-activation.private.json'
        with self.auth.store.connection() as db:
            started=db.execute('SELECT 1 FROM ingress_receipts LIMIT 1').fetchone()
        if activation.exists() or started:
            require(reader['data']['enabled'] and processing['data']['enabled'] and web['data']['enabled'],
                    'd4_web_permission_withdrawn')
        if not (reader['data']['enabled'] and processing['data']['enabled'] and web['data']['enabled']):
            service.enable_for_persona(pid,True,reader['revision'],processing['revision'],web['revision'],identifier())
        save_once(activation,{'grant':self.auth.binding['author_use_sha256'],'instance':self.auth.binding['instance_id']})


async def run(root,feedback,*,targeted=False,author_input=None):
    root=outside_repository(root)
    state,rev,original,material,folder,use=materials(root,feedback,targeted=targeted,author_input=author_input)
    grant=folder/'author-use.private.json'
    require(grant.exists() or not (folder/'candidate/home').exists(),'d4_permission_withdrawn')
    save_once(grant,use)
    prior,_=binding_at(original)
    source=configured_model(prior);base=ConfigStore(prior).load()['model']
    providers={'knowledge_router':{'base_url':source['base_url'],'name':use['jev_model']},
               'knowledge_native':{k:flash_model(base)[k] for k in ('base_url','name')}}
    fields={'mode':'scene_reviewed_live','version':use['version'],'scene_revision':use['scene_revision'],'jev_model':use['jev_model']}
    paths,binding,plans,prior=setup_branch(root,folder,'candidate',material,use,fields,web_providers=providers)
    ledger=ShortChatLedger(paths,binding)
    cards=CopyCards(rev,binding,prior)
    auth=Admission(root,paths,binding,prior,material,ledger,marker='d4-binding.json')
    scene=LiveChoice(auth,cards,read_private(rev/'jev-pro-1.14-wire-timing/scope.private.json'))
    router=SceneRouter(folder/'candidate/scene-ledger',cards)
    def present(tid,raw):
        key,_=scene.engine.check_current(tid)
        return speech(raw,key)
    client=D4Speech(auth,present=present)
    managed=D4Managed(auth,scene,client,plans);client.managed=managed
    def factory(store,config,main,**kwargs):
        engine=D4Engine(store,config,main,ledger=ledger,router=router,scene_transport=scene,**kwargs)
        scene.engine=engine
        return engine
    async def resume_local(app,turn):
        tid=turn['id']
        require(turn['error_code']=='scene_history_body_changed','d4_failure_requires_review')
        with auth.store.connection() as db, ledger.db() as side, router.db() as choices:
            require(not db.execute('SELECT 1 FROM attempts WHERE turn_id=?',(tid,)).fetchone()
                    and not side.execute('SELECT 1 FROM main_calls WHERE turn=?',(tid,)).fetchone()
                    and not side.execute('SELECT 1 FROM d4_calls WHERE turn=?',(tid,)).fetchone()
                    and not choices.execute('SELECT 1 FROM routes WHERE turn=? AND started IS NOT NULL',(tid,)).fetchone(),
                    'd4_prior_dispatch')
        await app['engine'].retry(tid)
    try:
        await replay_workbench(paths,binding,plans,auth,ledger,factory,client,SceneWorkbench,'D4',
                               configure=managed.configure,resume_local=resume_local)
    finally:
        try: await router.close()
        finally:
            await scene.transport.close()
            await managed.web.close()


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True);cli.add_argument('--feedback',type=Path,required=True)
    mode=cli.add_mutually_exclusive_group()
    mode.add_argument('--targeted',action='store_true');mode.add_argument('--author-input',type=Path)
    args=cli.parse_args()
    asyncio.run(run(args.root,args.feedback,targeted=args.targeted,author_input=args.author_input))
