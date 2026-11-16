"""冻结并执行首批16个独立转译位置，源稿与旧结果只读。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import random
import secrets
import shutil
import sqlite3
import time
from contextlib import closing

from persona_runtime.config import ConfigStore
from persona_runtime.expression_transport import ExpressionHTTPClient
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase,identifier,timestamp
from persona_runtime.network_proxy import outbound_proxy
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request,usage_fields
from persona_runtime.security import unprotect

from .protocol import PROFILE,TASK,REQUEST_LIMIT,build_messages,canonical,decode_output,voice_card

REPO=Path(__file__).resolve().parents[2]
VERSION='expression-persona-draft-R1-plan-v1'
CONTINUATION_VERSION='expression-persona-draft-R1-continuation-v2'


def realizer(profile):
    if profile==PROFILE:
        from . import protocol
        return protocol
    from persona_runtime import expression_persona_draft
    if profile==expression_persona_draft.PROFILE:return expression_persona_draft
    raise RuntimeErrorBase('persona_study_profile_unsupported')


def sha(value):return hashlib.sha256(value).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))
def write_once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as file:json.dump(value,file,ensure_ascii=False,indent=2)


def model_for(case_id):
    return {'base_url':'https://api.deepseek.com','name':'deepseek-flash','encrypted_key':'',
        'timeout':120,'max_tokens':8192,'_json_output':True,
        '_dialogue_profile':{'version':'natural-dialogue-v8','seed':case_id,'temperature':1.0}}


def prepare(root,source_root,*,trial_kind='real'):
    root,source_root=root.resolve(),source_root.resolve()
    if root.is_relative_to(REPO) or root.exists():raise RuntimeErrorBase('persona_study_target_invalid')
    if trial_kind not in ('real','offline_fixture'):raise RuntimeErrorBase('persona_study_kind_invalid')
    source_plan=read(source_root/'plan.json')
    if sha((source_root/'plan.json').read_bytes())!=read(source_root/'plan-hash.json')['sha256']:
        raise RuntimeErrorBase('persona_source_plan_changed')
    if source_plan['fixtures'].get('source')!='authored_public_synthetic':raise RuntimeErrorBase('persona_public_source_required')
    if trial_kind=='real' and source_plan.get('trial_kind')!='real':raise RuntimeErrorBase('persona_real_source_required')
    fixture=read(Path(__file__).with_name('fixtures.json'));source=read(source_root/'review-data.json')
    cards={key:voice_card({'core_traits':source['personas'][key]['core_traits'],'style':source['personas'][key]['expression_style']}) for key in fixture['role_order']}
    cases=[]
    for draft in fixture['authored_drafts']:
        for role in fixture['role_order']:
            cases.append({'id':'R1-'+draft['id']+'-'+role,'kind':'authored_capability_draft','title':draft['title'],
                'role':role,'role_name':source['personas'][role]['name'],'voice':cards[role],'draft':draft['draft'],
                'source':{'kind':'author_approved_plan_fixture','fixture_sha256':sha(Path(__file__).with_name('fixtures.json').read_bytes())}})
    database=source_root/'instance/data/database/runtime.sqlite3'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        for spec in fixture['saved_cases']:
            old=next(c for c in source['cases'] if c['id']==spec['id']);turn=db.execute('SELECT * FROM turns WHERE id=?',(old['turn_id'],)).fetchone()
            receipt=db.execute('SELECT * FROM expression_main_receipts WHERE turn_id=?',(old['turn_id'],)).fetchone()
            if not turn or not receipt or not receipt['valid']:raise RuntimeErrorBase('persona_source_receipt_missing')
            if sha(receipt['body'].encode())!=receipt['body_hash'] or receipt['body']!=old['primary'] or old['primary_hash']!=receipt['body_hash']:
                raise RuntimeErrorBase('persona_source_body_changed')
            profile=json.loads(turn['profile_snapshot']);card=voice_card({'core_traits':profile['core']['data']['core_traits'],'style':profile['expression']['data']['style']})
            if card!=cards[spec['persona']] or old['persona']!=spec['persona']:raise RuntimeErrorBase('persona_voice_source_mismatch')
            cases.append({'id':'R1-'+spec['id'],'kind':'saved_real_primary','title':old['title'],'role':spec['persona'],
                'role_name':source['personas'][spec['persona']]['name'],'voice':card,'draft':receipt['body'],
                'source':{'kind':'durable_primary_receipt','old_case_id':spec['id'],'turn_id':old['turn_id'],
                    'receipt_id':receipt['id'],'physical_attempt_id':receipt['physical_attempt_id'],'body_hash':receipt['body_hash'],
                    'core_version':profile['core'].get('id'),'expression_version':profile['expression'].get('id')}})
    if len(cases)!=16 or len({c['id'] for c in cases})!=16:raise RuntimeErrorBase('persona_study_case_count_invalid')
    root.mkdir(parents=True)
    frozen={}
    for directory in ('persona_runtime','experiments/expression_persona_draft'):
        for path in sorted((REPO/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.json'):
                rel=path.relative_to(REPO);copy=root/'frozen-source'/rel;copy.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,copy)
                frozen[rel.as_posix()]=sha(path.read_bytes())
    positions=[]
    for ordinal,case in enumerate(cases,1):
        path=root/'inputs'/(case['id']+'.json');write_once(path,case)
        model=model_for(case['id']);messages=build_messages(case['voice'],case['draft']);request,_=build_request(model,messages)
        if len(canonical(request))>REQUEST_LIMIT:raise RuntimeErrorBase('persona_request_capacity')
        wire=root/'positions'/case['id']/'request.json';write_once(wire,request)
        positions.append({'id':case['id'],'ordinal':ordinal,'kind':case['kind'],'role':case['role'],
            'input_sha256':sha(path.read_bytes()),'request_sha256':sha(canonical(request).encode())})
    plan={'version':VERSION,'created_at':timestamp(),'profile':PROFILE,'trial_kind':trial_kind,
        'source_root':str(source_root),'source_files':{name:sha((source_root/name).read_bytes()) for name in ('plan.json','review-data.json','review-key.json')},
        'source_hashes':frozen,'task_sha256':sha(TASK.encode()),'positions':positions,'max_physical_calls':16,
        'primary_calls':0,'jev_calls':0,'native_calls':0,'paid_selector':0,'paid_splitter':0,'paid_learning':0,
        'per_position_actual_dispatch_max':1,'automatic_retries':0,'stop_on_failure_or_unknown':True,
        'business_inputs':['core_traits_and_expression_style','complete_primary_draft'],
        'author_value_gate_before_remaining_40':True,'runtime_or_formal_schema_modified':False}
    write_once(root/'plan.json',plan);write_once(root/'plan-hash.json',{'sha256':sha((root/'plan.json').read_bytes())})
    left=['primary']*8+['expression']*8;random.Random(secrets.token_hex(24)).shuffle(left)
    write_once(root/'review-key.json',{'plan_sha256':sha((root/'plan.json').read_bytes()),'left_source':{p['id']:v for p,v in zip(positions,left)}})
    return plan


def load(root,*,check_source=True):
    plan=read(root/'plan.json')
    if plan.get('version') not in (VERSION,CONTINUATION_VERSION) or sha((root/'plan.json').read_bytes())!=read(root/'plan-hash.json')['sha256']:
        raise RuntimeErrorBase('persona_study_plan_changed')
    expected=16 if plan['version']==VERSION else 40
    if len(plan['positions'])!=expected or plan['max_physical_calls']!=expected or len({p['id'] for p in plan['positions']})!=expected:
        raise RuntimeErrorBase('persona_study_case_count_invalid')
    codec=realizer(plan['profile'])
    if plan['version']==CONTINUATION_VERSION:
        parent=plan['parent_first_batch'];first=Path(parent['root'])
        if sha((first/'plan.json').read_bytes())!=parent['plan_sha256'] or len(list((first/'positions').glob('*/dispatch.json')))!=16:
            raise RuntimeErrorBase('persona_continuation_parent_changed')
        claim=read(first.parent/'remaining40-claim.json')
        if claim['root']!=str(root.resolve()) or claim['parent_plan_sha256']!=parent['plan_sha256']:
            raise RuntimeErrorBase('persona_continuation_claim_mismatch')
    if check_source:
        recovery=read(root/'local-recovery-sources.json') if (root/'local-recovery-sources.json').exists() else None
        suffix_path=root/'execution-suffix-sources.json'
        if suffix_path.exists():
            suffix=read(suffix_path)
            if not recovery or suffix['previous_recovery_sha256']!=sha((root/'local-recovery-sources.json').read_bytes()):
                raise RuntimeErrorBase('persona_local_recovery_binding_invalid')
            recovery=suffix
        allowed={'persona_runtime/expression_persona_draft.py','experiments/expression_persona_draft/study.py'}
        if recovery and (plan['version']!=CONTINUATION_VERSION or recovery['plan_sha256']!=sha((root/'plan.json').read_bytes()) or set(recovery['files'])-allowed):
            raise RuntimeErrorBase('persona_local_recovery_binding_invalid')
        for name,expected in plan['source_hashes'].items():
            path=(REPO/name).resolve()
            if not path.is_relative_to(REPO) or not path.is_file():raise RuntimeErrorBase('persona_study_source_changed')
            current=sha(path.read_bytes())
            repaired=recovery['files'].get(name) if recovery else None
            if current!=expected and not (repaired and repaired['before_sha256']==expected and repaired['after_sha256']==current):
                raise RuntimeErrorBase('persona_study_source_changed')
    for name,expected in plan['source_files'].items():
        if sha((Path(plan['source_root'])/name).read_bytes())!=expected:raise RuntimeErrorBase('persona_study_original_changed')
    for position in plan['positions']:
        case=read(root/'inputs'/(position['id']+'.json'))
        if sha((root/'inputs'/(position['id']+'.json')).read_bytes())!=position['input_sha256']:
            raise RuntimeErrorBase('persona_study_input_changed')
        request=read(root/'positions'/position['id']/'request.json')
        if sha(canonical(request).encode())!=position['request_sha256']:
            raise RuntimeErrorBase('persona_study_request_changed')
        if check_source and build_request(model_for(position['id']),codec.build_messages(case['voice'],case['draft']))[0]!=request:
            raise RuntimeErrorBase('persona_study_request_changed')
    return plan


def parse_received(root,position,raw):
    case=read(root/'inputs'/(position['id']+'.json'))
    result={'id':position['id'],'state':'unusable','response_sha256':sha(raw),'usage':None,'artifact':None,'error_code':None}
    try:
        data=json.loads(raw)
        if not isinstance(data,dict):raise RuntimeErrorBase('persona_response_json_not_object')
        result['usage']=usage_fields(data.get('usage'),deepseek=True)
        choice=data['choices'][0]
        if choice.get('finish_reason')!='stop' or choice['message'].get('tool_calls'):raise RuntimeErrorBase('persona_completion_unusable')
        codec=realizer(read(root/'plan.json')['profile'])
        result['artifact']=codec.decode_output(choice['message']['content'],case['draft']);result['state']='completed'
    except (ValueError,TypeError,KeyError,IndexError) as error:
        result['error_code']=str(error) if re_code(str(error)) else 'persona_response_unusable'
    return result


def reconcile(root,position):
    folder=root/'positions'/position['id'];path=folder/'response.raw.json';result_path=folder/'result.json'
    repair_path=folder/'recovered-result.json'
    if repair_path.exists():
        recovery=read(repair_path)
        if recovery['original_result_sha256']!=sha(result_path.read_bytes()) or recovery['result']['response_sha256']!=sha(path.read_bytes()):
            raise RuntimeErrorBase('persona_local_recovery_receipt_changed')
        if parse_received(root,position,path.read_bytes())!=recovery['result']:raise RuntimeErrorBase('persona_local_recovery_result_changed')
        return recovery['result']
    if result_path.exists():
        saved=read(result_path)
        if path.exists() and saved.get('response_sha256')!=sha(path.read_bytes()):raise RuntimeErrorBase('persona_received_response_changed')
        return saved
    if not path.exists():return None
    result=parse_received(root,position,path.read_bytes());write_once(result_path,result);return result


def recover_segmentation(root,case_id):
    plan=load(root,check_source=False)
    if plan['version']!=CONTINUATION_VERSION:raise RuntimeErrorBase('persona_local_recovery_profile_invalid')
    position=next((p for p in plan['positions'] if p['id']==case_id),None)
    if not position:raise RuntimeErrorBase('persona_local_recovery_case_invalid')
    folder=root/'positions'/case_id;original=read(folder/'result.json')
    if (folder/'recovered-result.json').exists():return reconcile(root,position)
    if original['state']!='unusable' or original['error_code']!='persona_response_fields_invalid':raise RuntimeErrorBase('persona_local_recovery_not_segmentation_failure')
    result=parse_received(root,position,(folder/'response.raw.json').read_bytes())
    if result['state']!='completed' or 'unexpected_segmentation_field:no_split' not in result['artifact']['notes']:
        raise RuntimeErrorBase('persona_local_recovery_not_segmentation_failure')
    write_once(folder/'recovered-result.json',{'original_result_sha256':sha((folder/'result.json').read_bytes()),
        'reason':'complete body, invalid segmentation transport; preserve body as single piece',
        'new_model_calls':0,'result':result})
    return result


def retain_protected_failure(root,case_id):
    plan=load(root,check_source=False)
    if plan['version']!=CONTINUATION_VERSION:raise RuntimeErrorBase('persona_retained_failure_scope_invalid')
    position=next((p for p in plan['positions'] if p['id']==case_id),None)
    if not position:raise RuntimeErrorBase('persona_retained_failure_scope_invalid')
    folder=root/'positions'/case_id;original=read(folder/'result.json');raw=(folder/'response.raw.json').read_bytes()
    if original['state']!='unusable' or original['error_code']!='protected_text_changed' or json.loads(raw)['choices'][0].get('finish_reason')!='stop':
        raise RuntimeErrorBase('persona_retained_failure_not_known_protected_failure')
    if parse_received(root,position,raw)['error_code']!='protected_text_changed':raise RuntimeErrorBase('persona_retained_failure_changed')
    path=folder/'retained-failure.json'
    data={'original_result_sha256':sha((folder/'result.json').read_bytes()),'response_sha256':sha(raw),
          'disposition':'retain unusable model response; never re-dispatch; only unattempted positions may continue','new_model_calls':0}
    if path.exists():
        if read(path)!=data:raise RuntimeErrorBase('persona_retained_failure_changed')
    else:write_once(path,data)
    return data


def re_code(value):return bool(value and len(value)<100 and all(c.islower() or c.isdigit() or c=='_' for c in value))


async def execute(root,*,encrypted_key='',client=None,continue_after_known_unusable=False):
    plan=load(root)
    codec=realizer(plan['profile'])
    if client is not None and plan['trial_kind']!='offline_fixture':raise RuntimeErrorBase('persona_fixture_client_requires_fixture_plan')
    client=client or ExpressionHTTPClient();stopped=None
    for position in plan['positions']:
        folder=root/'positions'/position['id'];saved=reconcile(root,position)
        if saved:
            if saved['state']!='completed':
                if continue_after_known_unusable and (folder/'retained-failure.json').exists():
                    retain_protected_failure(root,position['id']);continue
                stopped=position['id'];break
            continue
        if (folder/'dispatch.json').exists():stopped=position['id'];break
        if len(list((root/'positions').glob('*/dispatch.json')))>=plan['max_physical_calls']:raise RuntimeErrorBase('persona_call_budget_used')
        case=read(root/'inputs'/(position['id']+'.json'));model=model_for(position['id'])
        marker={'id':position['id'],'attempt_id':identifier(),'profile':plan['profile'],'started_at':timestamp(),
            'request_sha256':position['request_sha256'],'trial_kind':plan['trial_kind']}
        write_once(folder/'dispatch.json',marker)
        def received(raw):
            path=folder/'response.raw.json'
            if path.exists():
                if path.read_bytes()!=raw:raise RuntimeErrorBase('persona_raw_response_conflict')
            else:
                with path.open('xb') as output:output.write(raw)
        started=time.monotonic();outcome={'id':position['id'],'state':'unknown','diagnostics':{}}
        cancelled=None
        try:
            response=await client.complete(model|{'encrypted_key':encrypted_key},codec.build_messages(case['voice'],case['draft']),save_raw=received)
            outcome.update(state='returned',diagnostics=response['diagnostics'])
        except BaseException as error:
            diag=getattr(error,'diagnostics',{})
            known_unsent=diag.get('request_headers_sent') is False and diag.get('response_headers_received') is False
            known_failed=diag.get('response_headers_received') is True and diag.get('http_status',200)!=200
            outcome.update(state='not_dispatched' if known_unsent else 'failed' if known_failed else 'unknown',
                error_code=str(error) if re_code(str(error)) else type(error).__name__,diagnostics=diag)
            if isinstance(error,asyncio.CancelledError):cancelled=error
        finally:
            outcome['elapsed_ms']=round((time.monotonic()-started)*1000,3);write_once(folder/'outcome.json',outcome)
        saved=reconcile(root,position)
        print(json.dumps({'position':position['id'],'artifact_state':saved['state'] if saved else outcome['state'],'trial_kind':plan['trial_kind']}),flush=True)
        if cancelled is not None:raise cancelled
        if not saved or saved['state']!='completed':stopped=position['id'];break
    return {'stopped_at':stopped,'reserved_positions':len(list((root/'positions').glob('*/dispatch.json')))}


def verify_credentials(root,home):
    config=ConfigStore(RuntimePaths(home));start=read(root/'start.json')
    if sha(config.file.read_bytes())!=start['source_configuration_sha256']:raise RuntimeErrorBase('persona_source_config_changed')
    secret=config.load()['model']['encrypted_key'];plain=unprotect(secret)
    for path in root.rglob('*'):
        if path.is_file() and path.name!='run.lock' and any(n in path.read_bytes() for n in (secret.encode(),plain.encode())):
            raise RuntimeErrorBase('persona_source_key_persisted')
    return {'source_configuration_unchanged':True,'source_keys_not_persisted':True,'new_model_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','run','report','verify','recover','retain-failure'))
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--source-root',type=Path);parser.add_argument('--credential-home',type=Path)
    parser.add_argument('--position')
    parser.add_argument('--continue-after-known-unusable',action='store_true')
    args=parser.parse_args();root=args.root.resolve()
    if args.action=='prepare':
        if not args.source_root:parser.error('prepare requires --source-root')
        plan=prepare(root,args.source_root);print(json.dumps({'positions':len(plan['positions']),'max_physical_calls':16,'profile':PROFILE}));return
    if args.action=='report':
        from .review import report
        print(json.dumps(report(root),ensure_ascii=False));return
    if args.action=='recover':
        if not args.position:parser.error('recover requires --position')
        value=recover_segmentation(root,args.position);print(json.dumps({'position':args.position,'state':value['state'],'new_model_calls':0}));return
    if args.action=='retain-failure':
        if not args.position:parser.error('retain-failure requires --position')
        print(json.dumps(retain_protected_failure(root,args.position)));return
    if not args.credential_home:parser.error('credential home required')
    home=args.credential_home.resolve()
    if args.action=='verify':print(json.dumps(verify_credentials(root,home)));return
    plan=load(root)
    if plan['trial_kind']!='real':raise RuntimeErrorBase('persona_cli_real_plan_required')
    config=ConfigStore(RuntimePaths(home));before=sha(config.file.read_bytes());encrypted=config.load()['model']['encrypted_key']
    if not encrypted or len(unprotect(encrypted))<8:raise RuntimeErrorBase('persona_credentials_missing')
    outbound_proxy('https://api.deepseek.com/chat/completions')
    if not (root/'start.json').exists():write_once(root/'start.json',{'started_at':timestamp(),'source_configuration_sha256':before})
    elif read(root/'start.json')['source_configuration_sha256']!=before:raise RuntimeErrorBase('persona_source_config_changed')
    lock=InstanceLock(root/'run.lock')
    try:result=asyncio.run(execute(root,encrypted_key=encrypted,continue_after_known_unusable=args.continue_after_known_unusable))
    finally:lock.close()
    proof=verify_credentials(root,home)
    finished=root/'execution-finished.json'
    if finished.exists():finished=root/('execution-resumed-'+identifier()+'.json')
    write_once(finished,result|proof|{'finished_at':timestamp()})
    print(json.dumps(result|proof))


if __name__=='__main__':main()
