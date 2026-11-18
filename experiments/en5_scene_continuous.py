"""Run the approved, web-disabled Jev/Pro continuous candidate on its own workbench."""
import argparse
import asyncio
import json
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_store import outside_repository
from persona_runtime.expression_runtime import enabled as expression_enabled
from persona_runtime.knowledge_routing import binding as provider_binding, configured_model
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.profile import snapshot
from persona_runtime.provider_compat import build_request
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .en5_d3_live import Admission, MainTransport, replay_workbench, setup_branch, source_inputs
from .en5_decision_packet import file_hash
from .en5_jev_transport import JevTransport, TIMING_PROFILE
from .en5_scene_desktop import SceneDisplayLedger, SceneEngine, SceneWorkbench
from .en5_scene_route import SceneRouter, SceneSet
from .en5_scene_style import VERSION as STYLE_VERSION, short_frames, speech
from .en5_scene_voice import context, jev_request, require

STUDY = 'en5-jev-continuous-v1'
REPAIR = 'en5-jev-continuous-style-repair-v1'
REPAIR_MESSAGE = '根据审阅结论进行针对性修复，然后使用修复后的一版进行原样重跑，随后再交由我审阅。'


def reviewed(root):
    state, material, original = source_inputs(root)
    rev = outside_repository(state['en5_scene_author_revision'])
    fixed = rev/'jev-pro-1.14-wire-timing'
    result = fixed/'results.private.json'
    require(file_hash(result)==state['en5_scene_jev_results_sha256'],'fixed_result_changed')
    matches = list((fixed/'author-feedback').glob('*/correction-record.private.json'))
    require(len(matches)==1,'corrected_feedback_missing')
    archive = matches[0].parent
    correction = read_private(matches[0])
    form = read_private(archive/'corrected.private.json')
    submitted = read_private(archive/'submitted.private.json')
    for pos in ('2-09','2-14'):
        submitted['positions'][pos]['ranks']={'甲':'1','乙':'2'}
    require(submitted==form,'unrelated_feedback_changes')
    require(file_hash(archive/'corrected.private.json')==correction['corrected_sha256']
            and form['results_sha256']==file_hash(result)
            and form['blind_key_sha256']==file_hash(fixed/'blind-key.private.json')
            and form['input_contract_sha256']==file_hash(rev/'requests.private.json'),'corrected_opinion_changed')
    key=read_private(fixed/'blind-key.private.json')['labels']
    results=read_private(result)['cases']
    winners={}
    for pos,row in form['positions'].items():
        if row['ranks']:
            require(set(row['ranks'].values())=={'1','2'} and set(row['ranks'])==set(key[pos]),'ranking')
            winners[pos]=key[pos][min(row['ranks'],key=row['ranks'].__getitem__)]
    require(winners=={'1-06':'C','1-12':'C','2-09':'C','2-14':'B'}
            and all(results[p]['status']=='selected' for p in winners if winners[p]=='C')
            and results['2-14']['status']=='http_error','corrected_preferences')
    revision=fingerprint(read_private(rev/'catalog.json'))
    fixed_scope=read_private(fixed/'scope.private.json')
    require(file_hash(fixed/'scope.private.json')==read_private(result)['binding']
            and fixed_scope['card_revision']==revision and fixed_scope['selected_model']=='jev-1.14'
            and fixed_scope['timing_profile']==TIMING_PROFILE,'scene_source_binding')
    baseline=root/'d3/live/results.private.json'
    require(file_hash(baseline)==state['en5_decision_d3_live_results_sha256'],'baseline_changed')
    usage={'version':STUDY,'approved':True,'roles':['main','scene_decision'],'sessions_per_arm':2,
           'turns_per_session':15,'web_enabled':False,'material_sha256':original['material_sha256'],
           'material_review_sha256':original['material_review_sha256'],'corrected_feedback_sha256':correction['corrected_sha256'],
           'fixed_results_sha256':file_hash(result),'scene_revision':revision,'baseline_sha256':file_hash(baseline),
           'timing_profile':TIMING_PROFILE,'jev_model':'jev-1.14','pro_model':'deepseek-v4-pro'}
    return state,material,rev,usage,archive


class CopyCards(SceneSet):
    """Verify canon at setup, then bind the same approved text to the local copy IDs."""
    def __init__(self, revision_dir, binding, prior):
        source=read_private(revision_dir/'catalog.json')
        require(fingerprint(source)==binding['scene_revision'],'unreviewed_catalog')
        self.authority=SceneSet(revision_dir,binding['scene_revision'],source['persona'])
        with Store(prior).connection() as db:
            pid=source['persona']
            head=db.execute('SELECT version_id FROM character_heads WHERE persona_id=?',(pid,)).fetchone()
            require(head and head[0]==source['character_version'] and
                    fingerprint(snapshot(db,pid))==source['profile_sha256'],'original_persona_changed')
        self.persona=binding['persona_id']
        self.revision=binding['scene_revision']
        self.binding=binding

    def catalog(self):
        return self.authority.catalog() | {'persona':self.persona,'character_version':self.binding['character_version'],
                                          'profile_sha256':self.binding['profile_hash']}

    def card(self,key):
        return self.authority.card(key)


class LiveChoice:
    def __init__(self,auth,registry,fixed_scope):
        self.auth=auth
        self.registry=registry
        self.source=configured_model(auth.prior_paths)
        self.transport=JevTransport(self.source|{'name':'jev-1.14'})
        require(provider_binding(self.source)==fixed_scope['authorized_credential_source']
                and provider_binding(self.transport.model)==fixed_scope['jev_provider'],'jev_connection_changed')
        self.engine=None

    async def __call__(self,wire,*,on_dispatch,on_receipt):
        self.auth.check()
        tid=self.engine.active_turn.get()
        with self.auth.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            _,targets=self.engine.sources(db,turn)
        catalog=self.registry.catalog()
        expected=jev_request(self.auth.binding['jev_model'],context(targets),catalog['instruction'],catalog['criteria'])
        require(wire==expected and self.transport.model['name']==wire['model']
                and provider_binding(configured_model(self.auth.prior_paths))==provider_binding(self.source),'jev_wire_scope')
        guard_cloud_dispatch(Store(self.auth.prior_paths),self.registry.authority.persona,'knowledge_router',self.source)
        def diagnostics(value):
            save_once(self.auth.folder/'timings'/(tid+'.private.json'),value)
        return await self.transport.complete(wire,on_dispatch=on_dispatch,on_receipt=on_receipt,diagnostics=diagnostics)


class ContinuousEngine(SceneEngine):
    wire_timing=True
    web_enabled=False
    main_client_type=MainTransport

    @classmethod
    def check_transports(cls,ledger,client,scene_transport):
        require(ledger.binding['mode']=='scene_reviewed_live' and type(client) is cls.main_client_type
                and type(scene_transport) is LiveChoice and client.auth is scene_transport.auth
                and client.auth.ledger is ledger,'live_scene_scope')

    async def _prepare_context(self,tid,*args):
        self.client.auth.check()
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            require(bool(self.knowledge.processing and self.knowledge.processing.enabled(turn))==self.web_enabled
                    and not expression_enabled(db,tid) and not self.direct.generation.enabled(db,tid)
                    and not db.execute('SELECT 1 FROM attempts WHERE turn_id=?',(tid,)).fetchone(),'unexpected_stage')
        prepared=await super()._prepare_context(tid,*args)
        model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],
                                                  '_expression_snapshot':turn['expression_snapshot']}
        wire=build_request(model,prepared['messages'])[0]
        def check():
            self.client.auth.check()
            self.check_current(tid)
            with self.store.connection() as db:
                row=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                require(self.cognitive._guard(db,row,prepared['details'])==prepared['guard'],'live_context_changed')
        self.client.auth.bind(turn,'main',model,wire,check)
        return prepared


def style_review(rev,use,feedback):
    folder=rev/'continuous'
    submitted=read_private(feedback)
    archived=read_private(folder/'results.private.json')
    key=read_private(folder/'blind-key.private.json')
    require(submitted['version']=='en5-scene-continuous-author-feedback-v1'
            and submitted['review_version']==STUDY and submitted['materials_sha256']==use['material_sha256']
            and submitted['results_sha256']==key['results']==file_hash(folder/'results.private.json')
            and submitted['key_sha256']==file_hash(folder/'blind-key.private.json'),'style_feedback_binding')
    require(archived['version']==STUDY and archived['baseline_sha256']==use['baseline_sha256']
            and len(submitted['groups'])==len(key['groups'])==2,'style_feedback_scope')
    first,second=submitted['groups']
    require((first['session'],second['session'])==(1,2)
            and key['groups'][0].get(first['preference'])=='candidate'
            and second['preference']=='两者都不可接受' and key['groups'][1]['乙']=='candidate'
            and '字数' in first['note'] and '括号' in second['note'],'style_feedback_review')
    return submitted


class ShortChatLedger(SceneDisplayLedger):
    frames_for=staticmethod(short_frames)


async def run(root,*,style_feedback=None):
    root=outside_repository(root)
    state,material,rev,use,archive=reviewed(root)
    folder=rev/'continuous'
    message='以这一份评判为准，继续下一步的开发。'
    stage=save_once(archive/'stage-review.private.json',{'version':STUDY,'feedback_sha256':use['corrected_feedback_sha256'],
        'author_message':message,'decision':'advance_to_continuous'})
    if style_feedback:
        review=style_review(rev,use,style_feedback)
        folder=rev/'continuous-style-repair'
        review_hash=save_once(folder/'style-review.private.json',review)
        use=use|{'version':REPAIR,'style_profile':STYLE_VERSION,'style_feedback_sha256':review_hash,
                 'prior_continuous_results_sha256':review['results_sha256']}
        message=REPAIR_MESSAGE
    grant=folder/'author-use.private.json'
    require(grant.exists() or not (folder/'candidate/home').exists(),'continuous_permission_withdrawn')
    save_once(grant,use|{'author_message':message,'stage_review_sha256':stage})
    fields={'mode':'scene_reviewed_live','version':use['version'],'scene_revision':use['scene_revision'],'jev_model':use['jev_model']}
    paths,binding,plans,prior=setup_branch(root,folder,'candidate',material,use,fields)
    ledger=(ShortChatLedger if style_feedback else SceneDisplayLedger)(paths,binding)
    cards=CopyCards(rev,binding,prior)
    auth=Admission(root,paths,binding,prior,material,ledger)
    scene=LiveChoice(auth,cards,read_private(rev/'jev-pro-1.14-wire-timing/scope.private.json'))
    router=SceneRouter(folder/'candidate/scene-ledger',cards)
    def present(tid,body):
        key,_=scene.engine.check_current(tid)
        return speech(body,key)
    client=MainTransport(auth,present=present if style_feedback else None)
    def factory(store,config,main,**kwargs):
        engine=ContinuousEngine(store,config,main,ledger=ledger,router=router,scene_transport=scene,**kwargs)
        scene.engine=engine
        return engine
    try:
        await replay_workbench(paths,binding,plans,auth,ledger,factory,client,SceneWorkbench,'scene-candidate')
    finally:
        try:
            await router.close()
        finally:
            await scene.transport.close()


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    cli.add_argument('--style-feedback',type=Path)
    args=cli.parse_args()
    asyncio.run(run(args.root,style_feedback=args.style_feedback))
