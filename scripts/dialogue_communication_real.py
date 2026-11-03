"""CR3-A.1 shared real auxiliary / three consumption views. Only execute --real sends HTTP."""
import argparse
import asyncio
from copy import deepcopy
import itertools
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.chat import ChatEngine,ChatCompletionsClient,ModelCompletion
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.dialogue_pipeline import DialoguePipeline,collect,current,source_guard,FROZEN
from persona_runtime.dialogue_context import require,route
from persona_runtime.communication_consumption import joint_messages_v2,parse_joint_v2,projection_v2,RENDERER_VERSION
from persona_runtime.character_selector_protocol import apply_selection
from persona_runtime.character_context import render_parts
from persona_runtime.context_m4 import build_context
from persona_runtime.prompt_assembly import PromptBlock
from persona_runtime.generation_retry import audited_complete,safe_diagnostics
from persona_runtime.provider_compat import build_request
from persona_runtime.private_personas import safe,guard_cloud_dispatch
from persona_runtime.character_embedding import private_directory
from scripts import dialogue_communication as base
from scripts.dialogue_context_real import Ledger,public
from scripts.dialogue_refactor import offline,close,NoDispatch
from scripts.example_calibration_r2 import offline as secondary
from tests.dialogue_communication_responses import joint,reply

HOME=base.HOME
OUT=HOME/'exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation'
PARENT=base.OUT/'offline-v4'
VERSION='cr3-real-v1'
read,sha,write_once=base.read,base.sha,base.write_once


def confined(root):return safe(Path(root).absolute(),OUT)


def sources():
    value=base.sources();value['scripts/dialogue_communication_real.py']=sha(Path(__file__))
    value['scripts/dialogue_context_real.py']=sha(ROOT/'scripts/dialogue_context_real.py')
    return value


def order():
    smoke=[(c,r,1) for c in ('G08','G11') for r in ('process','candid')]
    rest=[(c,r,n) for c in (f'G{i:02d}' for i in range(7,13)) for n in (1,2) for r in ('process','candid') if (c,r,n) not in smoke]
    perms=list(itertools.permutations(('raw','essential','full')))
    return [{'id':f'{c}-{r}-r{n}','case_id':c,'role':r,'replicate':n,'conditions':list(perms[i%6])}
            for i,(c,r,n) in enumerate(smoke+rest)]


async def prepare(root,real=False):
    root=confined(root);require(not root.exists(),'cr31_prepare_exists');root.mkdir(parents=True)
    initial=sources();parent=read(PARENT/'freeze.json')
    for name,h in parent['files'].items():require(sha(PARENT/name)==h,'cr31_parent_changed')
    data=read(PARENT/'materials.json');batch=uuid.uuid4().hex;runtime=HOME/'tmp/cr3-real'/batch
    private_directory(HOME,runtime)
    write_once(runtime/'owner.json',{'version':VERSION,'batch':batch,'artifact_root':str(root),'private':False})
    cfg=ConfigStore(RuntimePaths(HOME)).load()
    if real:
        require(all(cfg[k].get('encrypted_key') for k in ('model','character_selector')),'cr31_credentials_missing')
        write_once(runtime/'credentials.dpapi.json',{k:cfg[k] for k in ('model','character_selector')})
    write_once(root/'materials.json',data);write_once(root/'evaluation-only.json',read(PARENT/'evaluation-only.json'))
    groups=order();controls=[{'id':c+'-'+r+'-direct','case_id':c,'role':r,'replicate':1,'conditions':['raw']}
                           for c in ('G06','G05') for r in ('process','candid')]
    sequence=controls+groups;items={}
    seed=Path(read(PARENT/'runtime-owner.json')['home'])/'seed';require(Store(RuntimePaths(seed)).path.exists(),'cr31_seed_missing')
    for position in sequence:
        case=next(c for c in data['cases'] if c['id']==position['case_id']);key=position['case_id']+'-'+position['role']
        old=read(PARENT/'requests'/(key+'.json'));store=base.base.clone(Store(RuntimePaths(seed)),runtime/'admitted'/position['id'])
        local=base.base.configure(store.paths,base.base.public_models(),case)
        local_cfg=local.load()
        for section in ('model','character_selector'):
            a=public(local_cfg[section]);b=public(cfg[section]);a.pop('_dialogue_profile',None);b.pop('_dialogue_profile',None)
            require(a==b,'cr31_public_configuration_mismatch')
        recipe=base.execution_contract()|{'ablation_version':VERSION,'position':position,'batch':batch}
        write_once(store.paths.home/'execution-contract.json',recipe)
        attachment=await base.base.admit(store,local,old['attachment']['persona_id'],case,position['role'])
        attachment['pool_hash']=fingerprint(old['pool']);attachment['communication_contract_hash']=sha(store.paths.home/'execution-contract.json')
        write_once(store.paths.home/'artificial-cr2.json',attachment)
        engine=ChatEngine(store,local,NoDispatch())
        try:shared=collect(engine,attachment['turn_id'],attachment)
        finally:close(engine)
        require(route(shared['shared'])['path']==('direct' if len(position['conditions'])==1 else 'joint'),'cr31_frozen_route_mismatch')
        with store.connection() as db:turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(attachment['turn_id'],)).fetchone())
        item={'position':position,'admitted':str(store.paths.home),'attachment':attachment,'pool':old['pool'],
              'source_guard':shared['source_guard'],'evidence_hash':shared['shared'].digest,
              'public_snapshots':{k:turn[k] for k in FROZEN}}
        write_once(root/'requests'/(position['id']+'.json'),item);items[position['id']]=item
    protocol={'version':VERSION,'primary':['full','raw'],'secondary':['full','essential'],
              'exploratory':['essential','raw'],'planned_auxiliary':24,'planned_main_conditions':76,
              'normal_before_dedup':100,'total_call_cap':None,'native_retries':3,'paid_judge':False,
              'review':[{'number':2*k+j+1,'case_id':f'G{k+7:02d}','role':r,'replicate':1 if (k+j)%2==0 else 2,
                         'full_side':'A' if (2*k+j+1)%4 in (1,2) else 'B'}
                        for k in range(6) for j,r in enumerate(('process','candid'))]}
    write_once(root/'protocol.json',protocol)
    write_once(root/'prepared.json',{'version':VERSION,'real':real,'batch':batch,'runtime_root':str(runtime),
                                   'positions':sequence,'items':items})
    require(initial==sources(),'cr31_sources_changed_during_prepare')
    write_once(root/'freeze.json',{'sources':initial,'files':{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*.json')}})
    return {'groups':24,'direct_controls':4,'real_calls':0,'real_prepared':real}


def frozen(root):
    f=read(root/'freeze.json');require(f['sources']==sources(),'cr31_execution_source_changed')
    for n,h in f['files'].items():require(sha(root/n)==h,'cr31_frozen_file_changed')
    p=read(root/'prepared.json');runtime=Path(p['runtime_root']).resolve()
    require(runtime.is_relative_to((HOME/'tmp/cr3-real').resolve()),'cr31_runtime_boundary')
    require(read(runtime/'owner.json')=={'version':VERSION,'batch':p['batch'],'artifact_root':str(root),'private':False},'cr31_owner')
    return p


class Transport:
    def __init__(self,root,p,item,engine,ledger,slot,kind):
        self.root,self.p,self.item,self.engine,self.ledger,self.slot,self.kind=root,p,item,engine,ledger,slot,kind
    async def complete(self,model,messages):
        require(sources()==read(self.root/'freeze.json')['sources'],'cr31_execution_source_changed')
        require(self.kind in ('main','joint'),'cr31_unexpected_generation')
        require(read(Path(self.p['runtime_root'])/'owner.json')=={'version':VERSION,'batch':self.p['batch'],'artifact_root':str(self.root),'private':False},'cr31_owner_changed')
        item=self.item
        with self.engine.store.connection() as db:
            turn=current(self.engine,item['attachment']['turn_id'],item['attachment'],db)
            c=collect(self.engine,turn['id'],item['attachment'])
            require(c['source_guard']==item['source_guard'] and c['shared'].digest==item['evidence_hash'],'cr31_sources_changed')
        require(sha(self.engine.store.paths.home/'execution-contract.json')==item['attachment']['communication_contract_hash'],'cr31_recipe_changed')
        expected=json.loads(turn['model_snapshot'] if self.kind=='main' else turn['selector_model_snapshot'])
        allowed=deepcopy(expected)
        if self.kind=='main':allowed.update(_reply_mode=turn['reply_mode_snapshot'],_expression_snapshot=turn['expression_snapshot'])
        else:allowed.update(_json_output=True,_disposition_compile=True,_situation_observation=True,timeout=240,max_tokens=2048)
        request=fingerprint(build_request(model,messages)[0]);require(request==fingerprint(build_request(allowed,messages)[0]),'cr31_public_wire_changed')
        if self.p['real']:
            require((self.root/'run.started.json').exists(),'cr31_not_started')
            section='model' if self.kind=='main' else 'character_selector'
            approved=read(Path(self.p['runtime_root'])/'credentials.dpapi.json')[section]
            require(ConfigStore(RuntimePaths(HOME)).load()[section]==approved,'cr31_config_changed')
            for name in ('base_url','name','max_input_chars'):require(model[name]==expected[name]==approved[name],'cr31_model_permit')
            require(model['timeout']==(180 if self.kind=='main' else 240) and model['max_tokens']==(8192 if self.kind=='main' else 2048),'cr31_budget_changed')
            actual=deepcopy(model)|{'encrypted_key':approved['encrypted_key']}
            require(fingerprint(build_request(actual,messages)[0])==request,'cr31_auth_changed_body')
            guard_cloud_dispatch(self.engine.store,turn['persona_id'],'main' if self.kind=='main' else 'selector',actual)
            client=ChatCompletionsClient()
        else:actual=model;client=None;require(not model.get('encrypted_key'),'cr31_mock_key')
        row=self.ledger.reserve(self.slot,self.kind,model,messages);started=time.monotonic()
        try:
            if client:value=await client.complete(actual,messages)
            elif self.kind=='joint':
                payload=json.loads(messages[1]['content']);e=payload['evidence']|{'digest':payload['evidence_hash']}
                value=ModelCompletion(encoded(joint(item['position']['case_id'],e,item['position']['role'],2)),{'total_tokens':20},{'response_model':'mock','system_fingerprint':'mock'})
            else:value=ModelCompletion(reply(item['position']['case_id'],item['position']['role']),{'total_tokens':20},{'response_model':'mock','system_fingerprint':'mock'})
        except BaseException as error:
            code=str(error);code=code if re.fullmatch('[a-z][a-z0-9_]{0,100}',code) else type(error).__name__
            self.ledger.transport(row,{'status':'error','error_code':code,'usage':getattr(error,'usage',None),'raw':None,'real':self.p['real'],
                'diagnostics':safe_diagnostics(getattr(error,'diagnostics',None)),'elapsed_seconds':time.monotonic()-started});raise
        self.ledger.transport(row,{'status':'returned','raw':value[0],'usage':value[1],'real':self.p['real'],
            'diagnostics':safe_diagnostics(getattr(value,'diagnostics',None)),'elapsed_seconds':time.monotonic()-started})
        return value


class ViewPipeline(DialoguePipeline):
    def __init__(self,item,shared,condition):
        require(condition in ('raw','essential','full'),'cr31_condition_unknown')
        super().__init__(item['attachment'],item['pool'],expected_guard=item['source_guard'])
        self.item,self.shared,self.condition=item,shared,condition
    def recheck(self,engine,conn,turn,collected):
        require(sha(engine.store.paths.home/'execution-contract.json')==self.attachment['communication_contract_hash'],'cr31_recipe_changed')
        super().recheck(engine,conn,turn,collected)
    async def prepare(self,engine,tid):
        c=collect(engine,tid,self.attachment);require(c['shared'].digest==self.item['evidence_hash'],'cr31_evidence_stale')
        context='';result=None;removed=[]
        if self.shared is not None:
            result=parse_joint_v2(encoded(self.shared),c['shared'],json.loads(self.pool_json))
            view=deepcopy(result)
            if self.condition=='essential':view['communication_spec']['optional_reactions']=[]
            if self.condition!='raw':context,removed=projection_v2(view,c['shared'])
            character=apply_selection(c['character_version'],c['members'],json.loads(self.pool_json),result['selection'],4)
            character.update(requested_mode='joint',effective_mode='joint',degrade_reason=None)
        else:character=c['local']|{'requested_mode':'direct_local','effective_mode':'lexical','degrade_reason':None}
        turn=current(engine,tid,self.attachment);character=engine.cognitive.characters.prepare(turn,character)
        blocks=(PromptBlock('communication_spec',context,'communication_consumption',RENDERER_VERSION),) if context else ()
        with engine.store.connection() as db:
            self.recheck(engine,db,turn,c)
            messages,details=build_context(engine,db,turn,c['archive_evidence'],character,shared_materials=c['materials_json'],experimental_blocks=blocks)
            require(details['history_rows']==json.loads(c['materials_json'])['history_rows'],'cr31_second_pruning')
            require(len(render_parts(character['parts']))<=3000,'cr31_character_budget')
            manifest=engine.cognitive._manifest(db,turn,messages,details)
        manifest['_shadow_prepared']=None
        self.record={'condition':self.condition,'shared_evidence_hash':c['shared'].digest,'mother_spec_hash':fingerprint(result['communication_spec']) if result else None,
                     'view_spec_hash':fingerprint(view['communication_spec']) if result else None,'projection':context,'projection_removed':removed,'character':details['character']}
        return {'messages':messages,'details':details,'manifest':manifest,'character_prepared':character,'guard':c['source_guard'],
                'accepted_policy_hash':fingerprint(turn['context_policy_snapshot']),'_dialogue_pipeline':True,'_shared_collected':c}


def engine_for(p,item,tail,ledger):
    source=Store(RuntimePaths(Path(item['admitted'])));store=base.base.clone(source,Path(p['runtime_root'])/tail)
    cfg=ConfigStore(store.paths);cfg.file.parent.mkdir(parents=True,exist_ok=True);cfg.file.write_bytes(ConfigStore(source.paths).file.read_bytes())
    write_once(store.paths.home/'artificial-cr2.json',item['attachment']);write_once(store.paths.home/'execution-contract.json',read(source.paths.home/'execution-contract.json'))
    return ChatEngine(store,cfg,NoDispatch(),generation_audit_sink=ledger.audit)


async def group(root,p,pos,ledger):
    item=p['items'][pos['id']];shared=None
    if len(pos['conditions'])>1:
        engine=engine_for(p,item,'auxiliary/'+pos['id'],ledger)
        try:
            c=collect(engine,item['attachment']['turn_id'],item['attachment']);turn=current(engine,item['attachment']['turn_id'],item['attachment'])
            model=json.loads(turn['selector_model_snapshot'])|{'_json_output':True,'_disposition_compile':True,'_situation_observation':True,'timeout':240,'max_tokens':2048}
            messages=joint_messages_v2(c['shared'],item['pool'],4,model['max_input_chars'])
            def before(state):
                with engine.store.connection() as db:
                    fresh=current(engine,turn['id'],item['attachment'],db)
                    require(source_guard(engine,db,fresh,c['archive_evidence'],c['guard_details'])==item['source_guard'],'cr31_sources_changed')
            value=await audited_complete(Transport(root,p,item,engine,ledger,pos['id'],'joint'),model,messages,
                validate=lambda x:parse_joint_v2(x,c['shared'],item['pool']),save=ledger.audit,before_attempt=before)
            shared=parse_joint_v2(value[0],c['shared'],item['pool'])
            write_once(root/'auxiliary'/(pos['id']+'.json'),{'real':p['real'],'simulation':not p['real'],'raw':value[0],'parsed':shared,'evidence_hash':c['shared'].digest,
                'calls':[x['reserve']['id'] for x in ledger.rows if x['reserve']['slot']==pos['id']+'/joint']})
        finally:close(engine)
    seen={}
    for condition in pos['conditions']:
        ident=pos['id']+'-'+condition;engine=engine_for(p,item,'main/'+ident,ledger)
        pipe=ViewPipeline(item,shared,condition);engine.dialogue_pipeline=pipe
        try:
            preview=await pipe.prepare(engine,item['attachment']['turn_id']);turn=current(engine,item['attachment']['turn_id'],item['attachment'])
            model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],'_expression_snapshot':turn['expression_snapshot']}
            body=build_request(model,preview['messages'])[0];wire=fingerprint(body)
            if wire in seen:
                canonical=seen[wire];require(canonical['body']==body,'cr31_hash_collision')
                record={'id':ident,'group':pos['id'],'condition':condition,'real':p['real'],'simulation':not p['real'],
                    'status':'aliased','canonical_id':canonical['record']['id'],'request_hash':wire,'view':pipe.record,
                    'reply':canonical['record']['reply'],'messages':preview['messages'],'actual_manifest_from':canonical['record']['id']}
            else:
                engine.client=Transport(root,p,item,engine,ledger,ident,'main');engine.selector.client=engine.client
                first=len(ledger.rows);await engine._run(item['attachment']['turn_id']);turn=engine.get(item['attachment']['turn_id'])
                rows=ledger.rows[first:];good=[x for x in rows if x.get('terminal',{}).get('status')=='completed']
                with engine.store.connection() as db:
                    active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
                    manifests=[json.loads(x[0]) for x in db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(turn['id'],))]
                require(turn['status']=='completed' and len(good)==1 and not active,'cr31_main_failed_'+str(turn.get('error_code')))
                main=good[0];require(main['reserve']['request_hash']==wire,'cr31_preview_actual_changed')
                require(fingerprint(main['reserve']['messages'])==manifests[-1]['input_snapshot_hash'],'cr31_manifest_mismatch')
                record={'id':ident,'group':pos['id'],'condition':condition,'real':p['real'],'simulation':not p['real'],
                    'status':'completed','request_hash':wire,'messages':main['reserve']['messages'],'reply':main['transport']['raw'],
                    'manifest':manifests[-1],'view':pipe.record,'calls':[x['reserve']['id'] for x in rows],'runtime':str(engine.store.paths.home)}
                seen[wire]={'body':body,'record':record}
            write_once(root/'results'/(ident+'.json'),record)
        finally:close(engine)


async def run(root,real):
    p=frozen(root);require(p['real'] is real,'cr31_mode_mismatch');require(not (root/'run.started.json').exists(),'cr31_existing_batch_report_only')
    write_once(root/'run.started.json',{'real':real,'at':time.time(),'batch':p['batch']});ledger=Ledger(root/'receipts',real);completed=0
    try:
        for pos in p['positions']:
            require(sources()==read(root/'freeze.json')['sources'],'cr31_source_changed')
            await group(root,p,pos,ledger);completed+=1
            atomic_json(root/'state.json',{'status':'running','real':real,'completed_groups_and_controls':completed,'physical_calls':len(ledger.rows)})
            print(encoded({'completed':completed,'of':28,'physical_calls':len(ledger.rows),'real':real}),flush=True)
            if completed==8:write_once(root/'smoke.json',{'engineering_passed':True,'completed_groups_and_controls':8,'physical_calls':len(ledger.rows),'quality_not_rerun_gate':True})
    except BaseException as error:
        reason=str(error);reason=reason if re.fullmatch('[a-z][a-z0-9_]{0,100}',reason) else type(error).__name__
        atomic_json(root/'state.json',{'status':'stopped','real':real,'completed_groups_and_controls':completed,'physical_calls':len(ledger.rows),'reason':reason});raise
    atomic_json(root/'state.json',{'status':'completed','real':real,'completed_groups_and_controls':completed,'physical_calls':len(ledger.rows)})
    return report(root)


def report(root):
    p=frozen(root);results={x.stem:read(x) for x in (root/'results').glob('*.json')};actual=[v for v in results.values() if v['status']=='completed']
    state=read(root/'state.json') if (root/'state.json').exists() else {'status':'not_started'}
    summary={'version':VERSION,'real':p['real'],'state':state,'logical_conditions':len(results),'unique_main_calls':len(actual),
        'aliases':sum(v['status']=='aliased' for v in results.values()),'auxiliary_completed':len(list((root/'auxiliary').glob('*.json'))),
        'physical_calls':len(list((root/'receipts').glob('*.reserve.json'))),'machine_reading':'pending','user_review':'pending','release_approved':False}
    atomic_json(root/'report.json',summary);return summary


def verify(root):
    p=frozen(root);s=report(root);require(s['state']['status']=='completed' and s['logical_conditions']==76 and s['auxiliary_completed']==24,'cr31_incomplete')
    ids={x.name.split('.')[0] for x in (root/'receipts').glob('*.reserve.json')}
    for ext in ('transport','terminal'):require({x.name.split('.')[0] for x in (root/'receipts').glob('*.'+ext+'.json')}==ids,'cr31_unsettled')
    for ident in ids:
        row=read(root/'receipts'/(ident+'.reserve.json'));t=read(root/'receipts'/(ident+'.terminal.json'))
        require(row['real']==p['real']==t['real'],'cr31_mock_real_mixed')
        require(fingerprint(row)==t['reservation_hash'] and row['request_hash']==t['runtime_attempt']['request_hash'],'cr31_native_binding')
    values={x.stem:read(x) for x in (root/'results').glob('*.json')}
    for v in values.values():
        if v['status']=='aliased':
            c=values[v['canonical_id']];require(c['status']=='completed' and c['group']==v['group'] and c['request_hash']==v['request_hash'] and c['messages']==v['messages'] and c['reply']==v['reply'],'cr31_bad_alias')
        else:require(fingerprint(v['messages'])==v['manifest']['input_snapshot_hash'],'cr31_manifest_changed')
    result={'passed':True,'real':p['real'],'logical_conditions':76,'auxiliary':24,'unique_main_calls':s['unique_main_calls'],'aliases':s['aliases'],'physical_calls':s['physical_calls']}
    atomic_json(root/'verification.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','simulate','execute','report','verify'))
    parser.add_argument('--root',type=Path,default=OUT/'simulation-v1');parser.add_argument('--real',action='store_true');args=parser.parse_args();root=confined(args.root)
    require(not (OUT/'closure.json').exists(),'cr31_stage_sealed')
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp');os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    if args.action=='execute':require(args.real,'cr31_explicit_real_required');result=asyncio.run(run(root,True))
    else:
        with offline() as n,secondary() as n2:
            try:result=asyncio.run(prepare(root,args.real)) if args.action=='prepare' else asyncio.run(run(root,False)) if args.action=='simulate' else verify(root) if args.action=='verify' else report(root)
            finally:
                if root.exists():atomic_json(root/('network-'+args.action+'.json'),{'primary':n,'secondary':n2,'real_calls':0})
    print(encoded(result))


if __name__=='__main__':main()
