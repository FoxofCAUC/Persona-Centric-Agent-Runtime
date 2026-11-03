"""Separate, explicitly authorized Anthropic Messages *streaming* recovery for CR3-A.1.

Why this package exists: four earlier recovery packages (original real-v1,
continuation, Messages recovery-2, Responses recovery-3) all sent
``stream: false``. Long joint-auxiliary reasoning produced no byte within the
gateway's ~120 s window and returned HTTP 500/524. This runner keeps the same
frozen materials, templates, routing, retry rule (first + at most 3) and
success-replay prohibition, and only changes the transport to
``anthropic-messages-stream-v1`` (``stream: true``, idle-read timeout only).

Only ``execute --real`` dispatches. ``--max-groups N`` stops cleanly after N
groups for a smoke run; a later root in this same package imports those
results instead of replaying them.
"""
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
from scripts.dialogue_responses_audit import verify as audit_previous
from scripts.dialogue_messages_audit import verify as audit_messages
from scripts.dialogue_communication_real_audit import verify as audit_original
from scripts.dialogue_anthropic_transport import (build_messages_request, AnthropicMessagesClient,
    stream_transport_contract, messages_endpoint, STREAM_VERSION)
from scripts.dialogue_messages_retry import audited_messages_complete
from persona_runtime.archive import encoded,fingerprint
from persona_runtime.config import atomic_json,ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.character_embedding import private_directory
from persona_runtime.private_personas import guard_cloud_dispatch
from persona_runtime.generation_retry import safe_diagnostics

HOME=old.HOME
OUT=HOME/'exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-stream-4'
PREVIOUS=OUT.with_name('CR3-A.1-real-consumption-ablation-responses-3')
MESSAGES_PREVIOUS=OUT.with_name('CR3-A.1-real-consumption-ablation-recovery-2')
CONTINUATION=OUT.with_name('CR3-A.1-real-consumption-ablation-continuation')
PARENT=old.OUT/'real-v1'
VERSION='cr3-stream-recovery-v1'
PROTOCOL='anthropic-messages-v1'
IDLE_TIMEOUT_SECONDS=90
# Conservative ledger of the four predecessor packages (27 reservations: 10 ok, 16 failed, 1 unknown).
PRIOR_PHYSICAL_CALLS,PRIOR_FAILED_CALLS,PRIOR_UNKNOWN_CALLS=27,16,1
read,sha,write_once,require=old.read,old.sha,old.write_once,old.require


def sources():
    result=old.sources()
    for name in ('scripts/dialogue_stream_recovery.py','tests/test_dialogue_stream_recovery.py',
                 'scripts/dialogue_anthropic_transport.py','tests/test_dialogue_anthropic_transport.py',
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


def receipt_counts(folder):
    receipts=folder/'receipts'
    ids={x.name.split('.')[0] for x in receipts.glob('*.reserve.json')}
    settled={x.name.split('.')[0] for x in receipts.glob('*.terminal.json')}&{x.name.split('.')[0] for x in receipts.glob('*.transport.json')}
    failed=sum(read(receipts/(i+'.terminal.json'))['status']!='completed' for i in ids&settled)
    return {'physical':len(ids),'settled':len(ids&settled),'failed':failed}


def package_history(exclude=None):
    """Earlier *real* roots of this package; each must be fully settled before it is imported."""
    found=[]
    if not OUT.exists():return found
    for candidate in sorted(p for p in OUT.iterdir() if p.is_dir()):
        if exclude is not None and candidate.resolve()==Path(exclude).resolve():continue
        if not (candidate/'prepared.json').is_file():continue
        prepared=read(candidate/'prepared.json')
        if prepared.get('version')!=VERSION or prepared.get('real') is not True:continue
        if not (candidate/'run.started.json').exists():continue
        state=read(candidate/'state.json') if (candidate/'state.json').exists() else {}
        require(state.get('status') in ('stopped','completed'),'recovery_in_package_unsettled')
        counts=receipt_counts(candidate)
        require(counts['physical']==counts['settled'],'recovery_in_package_unsettled')
        found.append(candidate)
    return found


async def prepare(root,real=False):
    root=confined(root);require(not root.exists(),'recovery_existing_package')
    audit_original();audit_messages();audit_previous()
    for prior in (CONTINUATION,MESSAGES_PREVIOUS,PREVIOUS):
        require(read(prior/'real-v1/state.json')['status']=='stopped','recovery_previous_not_stopped')
        require(not list((prior/'real-v1/results').glob('*.json')),'recovery_unimported_previous_success')
    parent=read(PARENT/'prepared.json')
    values={p.stem:read(p) for p in (PARENT/'results').glob('*.json')}
    original_done,original_pending=remaining(parent,values)
    require(len(original_done)==6 and len(original_pending)==22 and original_pending[0]['id']=='G11-process-r1','recovery_boundary_changed')
    own=package_history(exclude=root)
    result_paths={key:PARENT/'results'/(key+'.json') for key in values}
    aux_paths={p.stem:p for p in (PARENT/'auxiliary').glob('*.json')}
    for prior in own:
        for f in (prior/'results').glob('*.json'):
            require(f.stem not in result_paths,'recovery_duplicate_import');result_paths[f.stem]=f;values[f.stem]=read(f)
        for f in (prior/'auxiliary').glob('*.json'):
            require(f.stem not in aux_paths,'recovery_duplicate_import');aux_paths[f.stem]=f
    done,pending=remaining(parent,values)
    require(pending==[x for x in original_pending if x['id'] not in {d['id'] for d in done}],'recovery_boundary_changed')
    require(pending,'recovery_nothing_pending')
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
            messages_endpoint(original[section])
    root.mkdir(parents=True)
    batch=uuid.uuid4().hex;runtime=HOME/'tmp/cr3-real'/batch;private_directory(HOME,runtime)
    write_once(runtime/'owner.json',{'version':VERSION,'batch':batch,'artifact_root':str(root),'private':False})
    if real:write_once(runtime/'credentials.dpapi.json',{k:cfg[k] for k in ('model','character_selector')})
    for name in ('materials.json','evaluation-only.json','protocol.json'):write_once(root/name,read(PARENT/name))
    template_roots=[items[position['id']]['admitted'] for position in pending]
    histories=[PARENT,CONTINUATION/'real-v1',MESSAGES_PREVIOUS/'real-v1',PREVIOUS/'real-v1']
    own_counts=[receipt_counts(prior) for prior in own]
    write_once(root/'imports.json',{'parent_root':str(PARENT),'history_roots':[str(p) for p in histories],
        'in_package_roots':[str(p) for p in own],
        'parent_closure':{'path':str(old.OUT/'closure.json'),'sha256':sha(old.OUT/'closure.json')},
        'prior_packages':[{'root':str(p),'closure_path':str(p.parent/'closure.json'),'closure_sha256':sha(p.parent/'closure.json')} for p in histories],
        'results':{key:{'path':str(path),'sha256':sha(path)} for key,path in result_paths.items()},
        'auxiliary':{key:{'path':str(path),'sha256':sha(path)} for key,path in aux_paths.items()},
        'imported_completed_positions':[x['id'] for x in done],
        'template_roots':template_roots,'template_files':{str(p):sha(p) for folder in template_roots for p in Path(folder).rglob('*') if p.is_file()},
        'prior_receipt_files':{str(p):sha(p) for history in histories+own for p in (history/'receipts').glob('*.json')},
        'prior_physical_calls':PRIOR_PHYSICAL_CALLS+sum(c['physical'] for c in own_counts),
        'prior_failed_calls':PRIOR_FAILED_CALLS+sum(c['failed'] for c in own_counts),
        'prior_unknown_calls':PRIOR_UNKNOWN_CALLS,'prior_receipts_origin':str(PARENT)})
    transport=stream_transport_contract(IDLE_TIMEOUT_SECONDS)
    contract={'version':VERSION,'transport':transport,'retry':{'max_retries':3,'delays_seconds':[1,2,4],
        'local_attempt_timeout':None,'slow_failure_stop_seconds':None},
        'requested_snapshots':'unchanged accepted legacy snapshots','effective_output_max_tokens':None,
        'effective_timeout_seconds':None,'idle_timeout_seconds':IDLE_TIMEOUT_SECONDS,
        'output_field_policy':'omit max_tokens; rejection stops; no fallback',
        'stream_policy':'stream true; SSE folded to the non-streaming message object; idle-read timeout only, no total deadline',
        'reasoning_policy':'reasoning_effort not sent (supplier default), unchanged from predecessors',
        'cross_protocol_comparison':'old successes retained as historical cohort; do not attribute transport changes to consumption',
        'authorization':'2026-11-03 user approved the streaming transport fix (stream:true plus 90 s idle-read timeout) and runner wiring; every real dispatch still requires a separate explicit instruction',
        'no_paid_probe':True,'no_total_call_cap':True}
    write_once(root/'transport-contract.json',transport)
    write_once(root/'execution-stream.json',contract)
    write_once(root/'prepared.json',{'version':VERSION,'real':real,'batch':batch,'runtime_root':str(runtime),
        'positions':pending,'original_positions':parent['positions'],'items':items,'parent_batch':parent['batch'],
        'idle_timeout_seconds':IDLE_TIMEOUT_SECONDS,'execution_contract_hash':sha(root/'execution-stream.json'),
        'authorization':{'manual_new_window':True,'success_replay_prohibited':True,'old_failures_retained':True}})
    write_once(root/'freeze.json',{'generation_sources':sources(),'files':{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*.json')}})
    return {'real':real,'new_positions':len(pending),'imported_positions':len(done),'in_package_roots':len(own),
            'real_calls':0,'protocol':transport}


def frozen(root):
    root=confined(root);f=read(root/'freeze.json');p=read(root/'prepared.json')
    require(f['generation_sources']==sources(),'recovery_generation_source_changed')
    for name,h in f['files'].items():require(sha(root/name)==h,'recovery_frozen_file_changed')
    runtime=Path(p['runtime_root']).resolve()
    require(runtime.is_relative_to((HOME/'tmp/cr3-real').resolve()),'recovery_runtime_outside')
    require(read(runtime/'owner.json')=={'version':VERSION,'batch':p['batch'],'artifact_root':str(root),'private':False},'recovery_owner_changed')
    require(sha(root/'execution-stream.json')==p['execution_contract_hash'],'recovery_contract_changed')
    references(root);return p


class Ledger:
    def __init__(self,root,p):
        self.package=root;self.root=root/'receipts';self.p=p;self.real=p['real'];self.rows=[];self.seen=set();self.broken=False
        self.allowed={x['id']+'/joint' for x in p['positions']}|{x['id']+'-'+c+'/main' for x in p['positions'] for c in x['conditions']}
    def reserve(self,pos,kind,model,messages,payload):
        frozen(self.package);slot=pos+'/'+kind
        require(slot in self.allowed,'recovery_success_replay_forbidden')
        require(not self.broken and not any('terminal' not in r for r in self.rows),'recovery_unsettled')
        require(payload.get('stream') is True and 'max_tokens' not in payload,'recovery_not_streaming')
        previous=[x for x in self.rows if x['reserve']['slot']==slot];request=fingerprint(payload)
        require(len(previous)<4 and not any(x['terminal']['status']=='completed' for x in previous),'recovery_retry_or_success_replay')
        require(all(x['reserve']['request_hash']==request for x in previous),'recovery_retry_changed')
        value={'id':f'{len(self.rows)+1:04d}','ordinal':len(self.rows)+1,'slot':slot,'kind':kind,'real':self.real,
            'simulation':not self.real,'protocol':PROTOCOL,'model':old.public(model),'messages':deepcopy(messages),
            'wire_request':deepcopy(payload),'wire_protocol':PROTOCOL,'stream':True,
            'input_hash':fingerprint(messages),'request_hash':request,'started_epoch':time.time(),
            'effective_timeout_seconds':None,'idle_timeout_seconds':self.p.get('idle_timeout_seconds'),
            'effective_output_max_tokens':None}
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


def stream_public(value):
    """Timing facts the generic safe_diagnostics drops; numbers and flags only, never text."""
    if not isinstance(value,dict):return {}
    result={}
    for key in ('stream','stream_downgraded','local_timeout_enabled'):
        if type(value.get(key)) is bool:result[key]=value[key]
    for key in ('stream_events','first_event_ms','first_thinking_ms','first_text_ms','timeout_seconds','elapsed_ms'):
        item=value.get(key)
        if type(item) in (int,float) and 0<=item<=1e9:result[key]=item
    return result


def mock_completion(item,kind,messages):
    meta={'response_model':'mock-messages','system_fingerprint':'mock','transport_profile':PROTOCOL,
          'finish_reason':'end_turn','stream':True,'stream_events':5,'first_event_ms':1.0,'first_thinking_ms':2.0,'first_text_ms':3.0}
    if kind=='joint':
        data=json.loads(messages[1]['content']);e=data['evidence']|{'digest':data['evidence_hash']}
        return old.ModelCompletion(encoded(old.joint(item['position']['case_id'],e,item['position']['role'],2)),{'total_tokens':20},meta)
    return old.ModelCompletion(old.reply(item['position']['case_id'],item['position']['role']),{'total_tokens':20},meta)


class Transport:
    def __init__(self,root,p,item,engine,ledger,slot,kind):
        self.root,self.p,self.item,self.engine,self.ledger,self.slot,self.kind=root,p,item,engine,ledger,slot,kind
        self.contract=read(root/'transport-contract.json')
        require(self.contract.get('version')==STREAM_VERSION,'recovery_not_streaming')
    def request_payload(self,model,messages):return build_messages_request(model,messages,self.contract)[0]
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
            if self.p['real']:value=await AnthropicMessagesClient(self.contract).complete(actual,messages)
            else:value=mock_completion(item,self.kind,messages)
        except BaseException as error:
            code=str(error);code=code if re.fullmatch('[a-z][a-z0-9_]{0,100}',code) else type(error).__name__
            diagnostics=getattr(error,'diagnostics',None)
            self.ledger.transport(row,{'status':'error','error_code':code,'usage':getattr(error,'usage',None),'raw':None,'real':self.p['real'],
                'provider_error':getattr(error,'provider_error',None),
                'diagnostics':safe_diagnostics(diagnostics),'stream_diagnostics':stream_public(diagnostics),
                'elapsed_seconds':time.monotonic()-started});raise
        diagnostics=getattr(value,'diagnostics',None)
        self.ledger.transport(row,{'status':'returned','raw':value[0],'usage':value[1],'real':self.p['real'],
            'diagnostics':safe_diagnostics(diagnostics),'stream_diagnostics':stream_public(diagnostics),
            'elapsed_seconds':time.monotonic()-started})
        return value


def engine_for(p,item,tail,ledger):
    engine=old.engine_for(p,item,tail,ledger)
    engine.generation_executor=audited_messages_complete
    return engine


class StreamViewPipeline(old.ViewPipeline):
    def __init__(self,item,shared,condition,contract):
        super().__init__(item,shared,condition);self.messages_contract=contract
    async def prepare(self,engine,tid):
        value=await super().prepare(engine,tid)
        turn=old.current(engine,tid,self.attachment)
        model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],'_expression_snapshot':turn['expression_snapshot']}
        payload,descriptor=build_messages_request(model,value['messages'],self.messages_contract)
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
        write_once(root/'auxiliary'/(pos['id']+'.json'),{'real':p['real'],'simulation':not p['real'],'protocol':PROTOCOL,'stream':True,
            'raw':value[0],'parsed':shared,'evidence_hash':c['shared'].digest,'calls':[r['reserve']['id'] for r in ledger.rows if r['reserve']['slot']==pos['id']+'/joint']})
    finally:old.close(engine)
    seen={}
    for condition in pos['conditions']:
        ident=pos['id']+'-'+condition;engine=engine_for(p,item,'main/'+ident,ledger)
        pipe=StreamViewPipeline(item,shared,condition,read(root/'transport-contract.json'));engine.dialogue_pipeline=pipe
        try:
            preview=await pipe.prepare(engine,item['attachment']['turn_id']);turn=old.current(engine,item['attachment']['turn_id'],item['attachment'])
            model=json.loads(turn['model_snapshot'])|{'_reply_mode':turn['reply_mode_snapshot'],'_expression_snapshot':turn['expression_snapshot']}
            transport=Transport(root,p,item,engine,ledger,ident,'main');body=transport.request_payload(model,preview['messages']);wire=fingerprint(body)
            if wire in seen:
                previous=seen[wire];require(previous['body']==body,'recovery_hash_collision')
                record={'id':ident,'group':pos['id'],'condition':condition,'real':p['real'],'simulation':not p['real'],
                    'protocol':PROTOCOL,'status':'aliased','canonical_id':previous['record']['id'],'request_hash':wire,
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
                record={'id':ident,'group':pos['id'],'condition':condition,'real':p['real'],'simulation':not p['real'],'protocol':PROTOCOL,
                    'status':'completed','request_hash':wire,'messages':main['reserve']['messages'],'reply':main['transport']['raw'],
                    'manifest':manifests[-1],'view':pipe.record,'calls':[r['reserve']['id'] for r in rows],'runtime':str(engine.store.paths.home)}
                seen[wire]={'body':body,'record':record}
            write_once(root/'results'/(ident+'.json'),record)
        finally:old.close(engine)


async def run(root,real,max_groups=None,group_runner=None):
    group_runner=group_runner or group
    p=frozen(root);require(p['real'] is real,'recovery_mode_mismatch')
    require(not (root/'run.started.json').exists(),'recovery_existing_batch_report_only')
    planned=len(p['positions']);limit=planned if max_groups is None else max_groups
    require(type(limit) is int and 1<=limit<=planned,'recovery_group_limit_invalid')
    write_once(root/'run.started.json',{'real':real,'at':time.time(),'batch':p['batch'],'manual_authorized_window':True,
        'max_groups':limit,'planned_groups':planned,'transport':STREAM_VERSION})
    ledger=Ledger(root,p);completed=0
    try:
        for pos in p['positions'][:limit]:
            frozen(root);await group_runner(root,p,pos,ledger);completed+=1
            atomic_json(root/'state.json',{'status':'running','real':real,'completed_new_groups':completed,'new_groups':planned,'max_groups':limit,'physical_calls':len(ledger.rows)})
            print(encoded({'completed_new':completed,'of':limit,'planned':planned,'physical_calls':len(ledger.rows),'real':real}),flush=True)
            if completed==1:write_once(root/'connectivity.json',{'passed':True,'planned_position':pos['id'],'extra_probes':0,'protocol':PROTOCOL,'stream':True})
    except BaseException as error:
        reason=str(error);reason=reason if re.fullmatch('[a-z][a-z0-9_]{0,100}',reason) else type(error).__name__
        atomic_json(root/'state.json',{'status':'stopped','real':real,'completed_new_groups':completed,'new_groups':planned,'max_groups':limit,'physical_calls':len(ledger.rows),'reason':reason});raise
    if completed==planned:
        atomic_json(root/'state.json',{'status':'completed','real':real,'completed_new_groups':planned,'new_groups':planned,'max_groups':limit,'physical_calls':len(ledger.rows)})
    else:
        atomic_json(root/'state.json',{'status':'stopped','real':real,'completed_new_groups':completed,'new_groups':planned,'max_groups':limit,'physical_calls':len(ledger.rows),'reason':'group_limit_reached'})
    return report(root)


def report(root):
    p=frozen(root);v={f.stem:read(f) for f in (root/'results').glob('*.json')};imp=references(root)
    result={'version':VERSION,'real':p['real'],'protocol':PROTOCOL,'transport':STREAM_VERSION,
        'state':read(root/'state.json') if (root/'state.json').exists() else {'status':'not_started'},
        'new_logical_conditions':len(v),'imported_logical_conditions':len(imp['results']),'new_unique_main':sum(x['status']=='completed' for x in v.values()),
        'new_aliases':sum(x['status']=='aliased' for x in v.values()),'new_auxiliary':len(list((root/'auxiliary').glob('*.json'))),
        'new_physical_calls':len(list((root/'receipts').glob('*.reserve.json'))),'prior_real_physical_calls':imp['prior_physical_calls'],
        'prior_unknown_calls':imp['prior_unknown_calls'],'old_successes_replayed':0,
        'stage_complete':False,'machine_reading':'pending','user_review':'pending','cross_protocol_caveat':True}
    atomic_json(root/'report.json',result);return result


def verify(root):
    p=frozen(root);s=report(root)
    planned_conditions=sum(len(x['conditions']) for x in p['positions'])
    require(s['state']['status']=='completed' and s['new_logical_conditions']==planned_conditions and s['new_auxiliary']==len(p['positions']),'recovery_incomplete')
    ids={x.name.split('.')[0] for x in (root/'receipts').glob('*.reserve.json')}
    for ext in ('transport','terminal'):require({x.name.split('.')[0] for x in (root/'receipts').glob('*.'+ext+'.json')}==ids,'recovery_unsettled')
    for ident in ids:
        r=read(root/'receipts'/(ident+'.reserve.json'));t=read(root/'receipts'/(ident+'.terminal.json'))
        require(r['real']==t['real']==p['real'] and fingerprint(r)==t['reservation_hash'],'recovery_receipt_binding')
        require(fingerprint(r['wire_request'])==r['request_hash']==t['runtime_attempt']['request_hash'],'recovery_actual_wire_changed')
        require('max_tokens' not in r['wire_request'] and r['effective_timeout_seconds'] is None,'recovery_limit_returned')
        require(r['wire_request']['stream'] is True and r['idle_timeout_seconds']==p['idle_timeout_seconds'],'recovery_not_streaming')
    for value in (read(f) for f in (root/'results').glob('*.json')):
        require(value['protocol']==PROTOCOL,'recovery_wrong_protocol')
        if value['status']=='aliased':
            c=read(root/'results'/(value['canonical_id']+'.json'))
            require(c['status']=='completed' and c['group']==value['group'] and all(c[k]==value[k] for k in ('request_hash','messages','reply')) and 'manifest' not in value,'recovery_alias_invalid')
        else:require(fingerprint(value['messages'])==value['manifest']['input_snapshot_hash'],'recovery_logical_manifest_changed')
    imp=read(root/'imports.json')
    result=s|{'passed':True,'success_replays':0,'combined_logical_conditions':len(imp['results'])+s['new_logical_conditions'],
              'combined_auxiliary':len(imp['auxiliary'])+s['new_auxiliary']}
    require(result['combined_logical_conditions']==76 and result['combined_auxiliary']==24,'recovery_combined_count_changed')
    atomic_json(root/'verification.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','simulate','execute','report','verify'))
    parser.add_argument('--root',type=Path,default=OUT/'simulation-v1');parser.add_argument('--real',action='store_true')
    parser.add_argument('--max-groups',type=int,default=None,help='stop cleanly after N groups (smoke run)')
    args=parser.parse_args();root=confined(args.root);require(not (OUT/'closure.json').exists(),'recovery_stage_sealed')
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp');os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    if args.action=='execute':require(args.real,'recovery_explicit_real_required');result=asyncio.run(run(root,True,args.max_groups))
    else:
        with old.offline() as net,old.secondary() as secondary:
            try:result=asyncio.run(prepare(root,args.real)) if args.action=='prepare' else asyncio.run(run(root,False,args.max_groups)) if args.action=='simulate' else verify(root) if args.action=='verify' else report(root)
            finally:
                if root.exists():atomic_json(root/('network-'+args.action+'.json'),{'primary':net,'secondary':secondary,'real_calls':0})
    print(encoded(result))


if __name__=='__main__':main()
