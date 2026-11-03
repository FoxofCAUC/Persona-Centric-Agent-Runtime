"""Separate, explicitly authorized Responses recovery. Only execute --real dispatches."""
import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import dialogue_communication_real as old
from scripts.dialogue_messages_audit import verify as audit_previous
from scripts.dialogue_communication_real_audit import verify as audit_original
from scripts.dialogue_responses_transport import build_responses_request,ResponsesClient,transport_contract,responses_endpoint
from scripts.dialogue_messages_retry import audited_messages_complete
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.config import atomic_json,ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.character_embedding import private_directory
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.generation_retry import safe_diagnostics

HOME=old.HOME
OUT=HOME/'exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-responses-3'
PREVIOUS=OUT.with_name('CR3-A.1-real-consumption-ablation-recovery-2')
CONTINUATION=OUT.with_name('CR3-A.1-real-consumption-ablation-continuation')
PARENT=old.OUT/'real-v1'
VERSION='cr3-responses-recovery-v1'
read,sha,write_once,require=old.read,old.sha,old.write_once,old.require


def sources():
    result=old.sources()
    for name in ('scripts/dialogue_responses_recovery.py','tests/test_dialogue_responses_recovery.py',
                 'scripts/dialogue_responses_transport.py','tests/test_dialogue_responses_transport.py',
                 'scripts/dialogue_anthropic_transport.py',
                 'scripts/dialogue_messages_retry.py','tests/test_dialogue_messages_retry.py',
                 'scripts/dialogue_communication_continuation.py','tests/test_dialogue_communication_continuation.py'):
        result[name]=sha(ROOT/name)
    return result


def confined(root):
    root=Path(root).resolve()
    require(root.is_relative_to(OUT.resolve()) and root!=OUT.resolve(),'recovery_root_outside')
    return root


def references(root):
    value=read(root/'imports.json')
    for item in value['prior_packages']:
        require(sha(Path(item['closure_path']))==item['closure_sha256'],'recovery_prior_seal_changed')
    for kind in ('results','auxiliary'):
        for item in value[kind].values():require(sha(Path(item['path']))==item['sha256'],'recovery_import_changed')
    for path,h in value['template_files'].items():require(sha(Path(path))==h,'recovery_template_changed')
    actual={str(f) for folder in value['template_roots'] for f in Path(folder).rglob('*') if f.is_file()}
    require(actual==set(value['template_files']),'recovery_template_inventory_changed')
    for path,h in value['prior_receipt_files'].items():require(sha(Path(path))==h,'recovery_old_receipt_changed')
    return value


def remaining(parent,values):
    done,pending=[],[]
    for position in parent['positions']:
        found=[position['id']+'-'+c in values for c in position['conditions']]
        require(not any(found) or all(found),'recovery_partial_group_requires_plan')
        (done if all(found) else pending).append(position)
    return done,pending


async def prepare(root,real=False):
    root=confined(root);require(not root.exists(),'recovery_existing_package')
    audit_original();audit_previous()
    require(read(PREVIOUS/'real-v1/state.json')['status']=='stopped','recovery_previous_not_stopped')
    parent=read(PARENT/'prepared.json')
    values={p.stem:read(p) for p in (PARENT/'results').glob('*.json')}
    done,pending=remaining(parent,values)
    require(len(done)==6 and len(pending)==22 and pending[0]['id']=='G11-process-r1','recovery_boundary_changed')
    require(not list((PREVIOUS/'real-v1/results').glob('*.json')),'recovery_unimported_previous_success')
    cfg=ConfigStore(RuntimePaths(HOME)).load()
    if real:require(all(cfg[k].get('encrypted_key') for k in ('model','character_selector')),'recovery_credentials_missing')
    items={position['id']:deepcopy(parent['items'][position['id']]) for position in pending}
    for item in items.values():
        original=ConfigStore(RuntimePaths(Path(item['admitted']))).load()
        for section in ('model','character_selector'):
            a,b=old.public(original[section]),old.public(cfg[section])
            a.pop('_dialogue_profile',None);b.pop('_dialogue_profile',None)
            require(a==b,'recovery_public_route_changed')
            require(not original[section].get('encrypted_key'),'recovery_key_in_template')
            responses_endpoint(original[section])
    root.mkdir(parents=True)
    batch=uuid.uuid4().hex;runtime=HOME/'tmp/cr3-real'/batch;private_directory(HOME,runtime)
    write_once(runtime/'owner.json',{'version':VERSION,'batch':batch,'artifact_root':str(root),'private':False})
    if real:write_once(runtime/'credentials.dpapi.json',{k:cfg[k] for k in ('model','character_selector')})
    for name in ('materials.json','evaluation-only.json','protocol.json'):write_once(root/name,read(PARENT/name))
    template_roots=[items[position['id']]['admitted'] for position in pending]
    histories=[PARENT,CONTINUATION/'real-v1',PREVIOUS/'real-v1']
    write_once(root/'imports.json',{'parent_root':str(PARENT),'history_roots':[str(p) for p in histories],
        'parent_closure':{'path':str(old.OUT/'closure.json'),'sha256':sha(old.OUT/'closure.json')},
        'prior_packages':[{'root':str(p),'closure_path':str(p.parent/'closure.json'),'closure_sha256':sha(p.parent/'closure.json')} for p in histories],
        'results':{key:{'path':str(PARENT/'results'/(key+'.json')),'sha256':sha(PARENT/'results'/(key+'.json'))} for key in values},
        'auxiliary':{p.stem:{'path':str(p),'sha256':sha(p)} for p in (PARENT/'auxiliary').glob('*.json')},
        'imported_completed_positions':[x['id'] for x in done],
        'template_roots':template_roots,'template_files':{str(p):sha(p) for folder in template_roots for p in Path(folder).rglob('*') if p.is_file()},
        'prior_receipt_files':{str(p):sha(p) for history in histories for p in (history/'receipts').glob('*.json')},
        'prior_physical_calls':23,'prior_failed_calls':13,'prior_receipts_origin':str(PARENT)})
    contract={'version':VERSION,'transport':transport_contract(),'retry':{'max_retries':3,'delays_seconds':[1,2,4],
        'local_attempt_timeout':None,'slow_failure_stop_seconds':None},
        'requested_snapshots':'unchanged accepted legacy snapshots','effective_output_max_tokens':None,
        'effective_timeout_seconds':None,'output_field_policy':'omit max_output_tokens and max_tokens; rejection stops; no fallback',
        'cross_protocol_comparison':'old successes retained as historical cohort; do not attribute protocol changes to consumption',
        'authorization':'user requested OpenAI-compatible Responses, retained no generation cap/local timeout, and kept the existing bubble contract',
        'no_paid_probe':True,'no_total_call_cap':True}
    write_once(root/'transport-contract.json',transport_contract())
    write_once(root/'execution-responses.json',contract)
    write_once(root/'prepared.json',{'version':VERSION,'real':real,'batch':batch,'runtime_root':str(runtime),
        'positions':pending,'original_positions':parent['positions'],'items':items,'parent_batch':parent['batch'],
        'execution_contract_hash':sha(root/'execution-responses.json'),
        'authorization':{'manual_new_window':True,'success_replay_prohibited':True,'old_failures_retained':True}})
    write_once(root/'freeze.json',{'generation_sources':sources(),'files':{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*.json')}})
    return {'real':real,'new_positions':22,'imported_positions':6,'real_calls':0,'protocol':contract['transport']}


def frozen(root):
    root=confined(root);f=read(root/'freeze.json');p=read(root/'prepared.json')
    require(f['generation_sources']==sources(),'recovery_generation_source_changed')
    for name,h in f['files'].items():require(sha(root/name)==h,'recovery_frozen_file_changed')
    runtime=Path(p['runtime_root']).resolve()
    require(runtime.is_relative_to((HOME/'tmp/cr3-real').resolve()),'recovery_runtime_outside')
    require(read(runtime/'owner.json')=={'version':VERSION,'batch':p['batch'],'artifact_root':str(root),'private':False},'recovery_owner_changed')
    require(sha(root/'execution-responses.json')==p['execution_contract_hash'],'recovery_contract_changed')
    references(root);return p


class Ledger:
    def __init__(self,root,p):
        self.package=root;self.root=root/'receipts';self.p=p;self.real=p['real'];self.rows=[];self.seen=set();self.broken=False
        self.allowed={x['id']+'/joint' for x in p['positions']}|{x['id']+'-'+c+'/main' for x in p['positions'] for c in x['conditions']}
    def reserve(self,pos,kind,model,messages,payload):
        frozen(self.package);slot=pos+'/'+kind
        require(slot in self.allowed,'recovery_success_replay_forbidden')
        require(not self.broken and not any('terminal' not in r for r in self.rows),'recovery_unsettled')
        previous=[x for x in self.rows if x['reserve']['slot']==slot];request=fingerprint(payload)
        require(len(previous)<4 and not any(x['terminal']['status']=='completed' for x in previous),'recovery_retry_or_success_replay')
        require(all(x['reserve']['request_hash']==request for x in previous),'recovery_retry_changed')
        value={'id':f'{len(self.rows)+1:04d}','ordinal':len(self.rows)+1,'slot':slot,'kind':kind,'real':self.real,
            'simulation':not self.real,'protocol':'openai-responses-v1','model':old.public(model),'messages':deepcopy(messages),
            'wire_request':deepcopy(payload),'wire_protocol':'openai-responses-v1',
            'input_hash':fingerprint(messages),'request_hash':request,'started_epoch':time.time(),
            'effective_timeout_seconds':None,'effective_output_max_tokens':None}
        try:write_once(self.root/(value['id']+'.reserve.json'),value)
        except BaseException:self.broken=True;raise
        row={'reserve':value};self.rows.append(row);return row
    def transport(self,row,value):
        try:write_once(self.root/(row['reserve']['id']+'.transport.json'),value)
        except BaseException:self.broken=True;raise
        row['transport']=value
    def audit(self,state):
        for attempt in state.get('generation_retry',{}).get('attempts',[]):
            if attempt['status']=='running' or attempt['id'] in self.seen:continue
            row=next((r for r in self.rows if 'terminal' not in r and r['reserve']['request_hash']==attempt['request_hash']),None)
            if row is None:continue
            value={'status':attempt['status'],'runtime_attempt':attempt,'reservation_hash':fingerprint(row['reserve']),
                   'real':self.real,'simulation':not self.real}
            try:write_once(self.root/(row['reserve']['id']+'.terminal.json'),value)
            except BaseException:self.broken=True;raise
            row['terminal']=value;self.seen.add(attempt['id'])


def public_response_metadata(value):
    source=getattr(value,'provider_response_metadata',None)
    if not isinstance(source,dict):return {}
    return {name:text for name,text in source.items()
            if name in ('model','system_fingerprint','status','response_id')
            and isinstance(text,str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,128}',text)}


class Transport:
    def __init__(self,root,p,item,engine,ledger,slot,kind):
        self.root,self.p,self.item,self.engine,self.ledger,self.slot,self.kind=root,p,item,engine,ledger,slot,kind
        self.contract=read(root/'transport-contract.json')
    def request_payload(self,model,messages):return build_responses_request(model,messages,self.contract)[0]
    async def complete(self,model,messages):
        frozen(self.root);item=self.item
        with self.engine.store.connection() as db:
            turn=old.current(self.engine,item['attachment']['turn_id'],item['attachment'],db)
            c=old.collect(self.engine,turn['id'],item['attachment'])
            require(c['source_guard']==item['source_guard'] and c['shared'].digest==item['evidence_hash'],'recovery_sources_changed')
        require(sha(self.engine.store.paths.home/'execution-contract.json')==item['attachment']['communication_contract_hash'],'recovery_old_recipe_changed')
        expected=json.loads(turn['model_snapshot'] if self.kind=='main' else turn['selector_model_snapshot'])
        allowed=deepcopy(expected)
        if self.kind=='main':allowed.update(_reply_mode=turn['reply_mode_snapshot'],_expression_snapshot=turn['expression_snapshot'])
        else:allowed.update(_json_output=True,_disposition_compile=True,_situation_observation=True)
        payload=self.request_payload(model,messages)
        require(payload==self.request_payload(allowed,messages),'recovery_effective_wire_changed')
        actual=deepcopy(model)
        for key in ('timeout','max_tokens','_request_deadline'):actual.pop(key,None)
        if self.p['real']:
            require((self.root/'run.started.json').exists(),'recovery_not_started')
            section='model' if self.kind=='main' else 'character_selector'
            approved=read(Path(self.p['runtime_root'])/'credentials.dpapi.json')[section]
            require(ConfigStore(RuntimePaths(HOME)).load()[section]==approved,'recovery_current_config_changed')
            for key in ('base_url','name','max_input_chars'):require(model[key]==expected[key]==approved[key],'recovery_route_changed')
            actual['encrypted_key']=approved['encrypted_key']
            guard_cloud_dispatch(self.engine.store,turn['persona_id'],'main' if self.kind=='main' else 'selector',actual)
        else:require(not model.get('encrypted_key'),'recovery_simulation_key')
        require(payload==self.request_payload(actual,messages),'recovery_auth_changed_payload')
        row=self.ledger.reserve(self.slot,self.kind,model,messages,payload);started=time.monotonic()
        try:
            if self.p['real']:value=await ResponsesClient(self.contract).complete(actual,messages)
            elif self.kind=='joint':
                data=json.loads(messages[1]['content']);e=data['evidence']|{'digest':data['evidence_hash']}
                value=old.ModelCompletion(encoded(old.joint(item['position']['case_id'],e,item['position']['role'],2)),{'total_tokens':20},{'response_model':'mock-responses','system_fingerprint':'mock','transport_profile':'openai-responses-v1','finish_reason':'stop'})
            else:value=old.ModelCompletion(old.reply(item['position']['case_id'],item['position']['role']),{'total_tokens':20},{'response_model':'mock-responses','system_fingerprint':'mock','transport_profile':'openai-responses-v1','finish_reason':'stop'})
            if not self.p['real']:
                value.provider_response_metadata={'model':'mock-responses','system_fingerprint':'mock','status':'completed','response_id':'mock-response'}
        except BaseException as error:
            code=str(error);code=code if re.fullmatch('[a-z][a-z0-9_]{0,100}',code) else type(error).__name__
            self.ledger.transport(row,{'status':'error','error_code':code,'usage':getattr(error,'usage',None),'raw':None,'real':self.p['real'],
                'provider_error':getattr(error,'provider_error',None),
                'provider_response_metadata':public_response_metadata(error),
                'diagnostics':safe_diagnostics(getattr(error,'diagnostics',None)),'elapsed_seconds':time.monotonic()-started});raise
        self.ledger.transport(row,{'status':'returned','raw':value[0],'usage':value[1],'real':self.p['real'],
            'provider_response_metadata':public_response_metadata(value),
            'diagnostics':safe_diagnostics(getattr(value,'diagnostics',None)),'elapsed_seconds':time.monotonic()-started})
        return value


def engine_for(p,item,tail,ledger):
    engine=old.engine_for(p,item,tail,ledger)
    engine.generation_executor=audited_messages_complete
    return engine


class ResponsesViewPipeline(old.ViewPipeline):
    def __init__(self,item,shared,condition,contract):
        super().__init__(item,shared,condition);self.responses_contract=contract
    async def prepare(self,engine,tid):
        value=await super().prepare(engine,tid)
        turn=old.current(engine,tid,self.attachment)
        model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],'_expression_snapshot':turn['expression_snapshot']}
        payload,descriptor=build_responses_request(model,value['messages'],self.responses_contract)
        value['manifest']['provider_request_contract']=descriptor|{
            'effective_max_tokens':None,'effective_timeout_seconds':None,
            'requested_limits_from_accepted_snapshot':{'timeout':model.get('timeout'),'max_tokens':model.get('max_tokens')},
            'isolated_execution_override':VERSION}
        return value


async def group(root,p,pos,ledger):
    item=p['items'][pos['id']];shared=None
    engine=engine_for(p,item,'auxiliary/'+pos['id'],ledger)
    try:
        c=old.collect(engine,item['attachment']['turn_id'],item['attachment']);turn=old.current(engine,item['attachment']['turn_id'],item['attachment'])
        model=json.loads(turn['selector_model_snapshot'])|{'_json_output':True,'_disposition_compile':True,'_situation_observation':True}
        messages=old.joint_messages_v2(c['shared'],item['pool'],4,model['max_input_chars'])
        def before(state):
            with engine.store.connection() as db:
                fresh=old.current(engine,turn['id'],item['attachment'],db)
                require(old.source_guard(engine,db,fresh,c['archive_evidence'],c['guard_details'])==item['source_guard'],'recovery_aux_source_changed')
        value=await audited_messages_complete(Transport(root,p,item,engine,ledger,pos['id'],'joint'),model,messages,
            validate=lambda x:old.parse_joint_v2(x,c['shared'],item['pool']),save=ledger.audit,before_attempt=before)
        shared=old.parse_joint_v2(value[0],c['shared'],item['pool'])
        write_once(root/'auxiliary'/(pos['id']+'.json'),{'real':p['real'],'simulation':not p['real'],'protocol':'openai-responses-v1',
            'raw':value[0],'parsed':shared,'evidence_hash':c['shared'].digest,'calls':[r['reserve']['id'] for r in ledger.rows if r['reserve']['slot']==pos['id']+'/joint']})
    finally:old.close(engine)
    seen={}
    for condition in pos['conditions']:
        ident=pos['id']+'-'+condition;engine=engine_for(p,item,'main/'+ident,ledger)
        pipe=ResponsesViewPipeline(item,shared,condition,read(root/'transport-contract.json'));engine.dialogue_pipeline=pipe
        try:
            preview=await pipe.prepare(engine,item['attachment']['turn_id']);turn=old.current(engine,item['attachment']['turn_id'],item['attachment'])
            model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],'_expression_snapshot':turn['expression_snapshot']}
            transport=Transport(root,p,item,engine,ledger,ident,'main');body=transport.request_payload(model,preview['messages']);wire=fingerprint(body)
            if wire in seen:
                previous=seen[wire];require(previous['body']==body,'recovery_hash_collision')
                record={'id':ident,'group':pos['id'],'condition':condition,'real':p['real'],'simulation':not p['real'],
                    'protocol':'openai-responses-v1','status':'aliased','canonical_id':previous['record']['id'],'request_hash':wire,
                    'view':pipe.record,'reply':previous['record']['reply'],'messages':preview['messages'],'actual_manifest_from':previous['record']['id']}
            else:
                engine.client=transport;engine.selector.client=transport
                first=len(ledger.rows);await engine._run(item['attachment']['turn_id']);turn=engine.get(item['attachment']['turn_id'])
                rows=ledger.rows[first:];good=[r for r in rows if r.get('terminal',{}).get('status')=='completed']
                with engine.store.connection() as db:
                    active=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
                    manifests=[json.loads(x[0]) for x in db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(turn['id'],))]
                require(turn['status']=='completed' and len(good)==1 and not active,'recovery_main_failed_'+str(turn.get('error_code')))
                main=good[0];require(main['reserve']['request_hash']==wire,'recovery_preview_changed')
                require(fingerprint(main['reserve']['messages'])==manifests[-1]['input_snapshot_hash'],'recovery_manifest_changed')
                require(manifests[-1]['provider_request_contract']['wire_hash']==wire,'recovery_manifest_wire_changed')
                record={'id':ident,'group':pos['id'],'condition':condition,'real':p['real'],'simulation':not p['real'],'protocol':'openai-responses-v1',
                    'status':'completed','request_hash':wire,'messages':main['reserve']['messages'],'reply':main['transport']['raw'],
                    'manifest':manifests[-1],'view':pipe.record,'calls':[r['reserve']['id'] for r in rows],'runtime':str(engine.store.paths.home)}
                seen[wire]={'body':body,'record':record}
            write_once(root/'results'/(ident+'.json'),record)
        finally:old.close(engine)


async def run(root,real):
    p=frozen(root);require(p['real'] is real,'recovery_mode_mismatch')
    require(not (root/'run.started.json').exists(),'recovery_existing_batch_report_only')
    write_once(root/'run.started.json',{'real':real,'at':time.time(),'batch':p['batch'],'manual_authorized_window':True})
    ledger=Ledger(root,p);completed=0
    try:
        for pos in p['positions']:
            frozen(root);await group(root,p,pos,ledger);completed+=1
            atomic_json(root/'state.json',{'status':'running','real':real,'completed_new_groups':completed,'new_groups':22,'physical_calls':len(ledger.rows)})
            print(encoded({'completed_new':completed,'of':22,'physical_calls':len(ledger.rows),'real':real}),flush=True)
            if completed==1:write_once(root/'connectivity.json',{'passed':True,'planned_position':pos['id'],'extra_probes':0,'protocol':'openai-responses-v1'})
    except BaseException as error:
        reason=str(error);reason=reason if re.fullmatch('[a-z][a-z0-9_]{0,100}',reason) else type(error).__name__
        atomic_json(root/'state.json',{'status':'stopped','real':real,'completed_new_groups':completed,'new_groups':22,'physical_calls':len(ledger.rows),'reason':reason});raise
    atomic_json(root/'state.json',{'status':'completed','real':real,'completed_new_groups':22,'new_groups':22,'physical_calls':len(ledger.rows)})
    return report(root)


def report(root):
    p=frozen(root);v={f.stem:read(f) for f in (root/'results').glob('*.json')};imp=references(root)
    result={'version':VERSION,'real':p['real'],'protocol':'openai-responses-v1','state':read(root/'state.json') if (root/'state.json').exists() else {'status':'not_started'},
        'new_logical_conditions':len(v),'imported_logical_conditions':len(imp['results']),'new_unique_main':sum(x['status']=='completed' for x in v.values()),
        'new_aliases':sum(x['status']=='aliased' for x in v.values()),'new_auxiliary':len(list((root/'auxiliary').glob('*.json'))),
        'new_physical_calls':len(list((root/'receipts').glob('*.reserve.json'))),'prior_real_physical_calls':23,'old_successes_replayed':0,
        'stage_complete':False,'machine_reading':'pending','user_review':'pending','cross_protocol_caveat':True}
    atomic_json(root/'report.json',result);return result


def verify(root):
    p=frozen(root);s=report(root)
    require(s['state']['status']=='completed' and s['new_logical_conditions']==66 and s['new_auxiliary']==22,'recovery_incomplete')
    ids={x.name.split('.')[0] for x in (root/'receipts').glob('*.reserve.json')}
    for ext in ('transport','terminal'):require({x.name.split('.')[0] for x in (root/'receipts').glob('*.'+ext+'.json')}==ids,'recovery_unsettled')
    for ident in ids:
        r=read(root/'receipts'/(ident+'.reserve.json'));t=read(root/'receipts'/(ident+'.terminal.json'))
        require(r['real']==t['real']==p['real'] and fingerprint(r)==t['reservation_hash'],'recovery_receipt_binding')
        require(fingerprint(r['wire_request'])==r['request_hash']==t['runtime_attempt']['request_hash'],'recovery_actual_wire_changed')
        require('max_tokens' not in r['wire_request'] and 'max_output_tokens' not in r['wire_request'] and r['effective_timeout_seconds'] is None,'recovery_limit_returned')
        require(r['wire_request']['store'] is False and r['wire_request']['stream'] is False,'recovery_storage_or_stream_changed')
    for value in (read(f) for f in (root/'results').glob('*.json')):
        require(value['protocol']=='openai-responses-v1','recovery_wrong_protocol')
        if value['status']=='aliased':
            c=read(root/'results'/(value['canonical_id']+'.json'))
            require(c['status']=='completed' and c['group']==value['group'] and all(c[k]==value[k] for k in ('request_hash','messages','reply')) and 'manifest' not in value,'recovery_alias_invalid')
        else:require(fingerprint(value['messages'])==value['manifest']['input_snapshot_hash'],'recovery_logical_manifest_changed')
    result=s|{'passed':True,'success_replays':0,'combined_logical_conditions':76,'combined_auxiliary':24}
    atomic_json(root/'verification.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','simulate','execute','report','verify'))
    parser.add_argument('--root',type=Path,default=OUT/'simulation-v1');parser.add_argument('--real',action='store_true')
    args=parser.parse_args();root=confined(args.root);require(not (OUT/'closure.json').exists(),'recovery_stage_sealed')
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp');os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    if args.action=='execute':require(args.real,'recovery_explicit_real_required');result=asyncio.run(run(root,True))
    else:
        with old.offline() as net,old.secondary() as secondary:
            try:result=asyncio.run(prepare(root,args.real)) if args.action=='prepare' else asyncio.run(run(root,False)) if args.action=='simulate' else verify(root) if args.action=='verify' else report(root)
            finally:
                if root.exists():atomic_json(root/('network-'+args.action+'.json'),{'primary':net,'secondary':secondary,'real_calls':0})
    print(encoded(result))


if __name__=='__main__':main()
