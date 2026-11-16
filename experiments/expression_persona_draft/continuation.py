"""首批价值确认后的剩余40份v2回归；旧16位和主稿不重跑。"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import random
import secrets
import shutil
import sqlite3

from persona_runtime.models import RuntimeErrorBase,timestamp
from persona_runtime.provider_compat import build_request
from persona_runtime.expression_persona_draft import PROFILE,TASK,REQUEST_LIMIT,voice_card,build_messages,canonical
from . import study


def verify_review(first_root,review_path):
    plan=study.load(first_root,check_source=False);data=study.read(first_root/'review-data.json');key=study.read(first_root/'review-key.json');review=study.read(review_path)
    expected={c['id']:c for c in data['cases']};seen=set();results=[]
    if plan['version']!=study.VERSION or review['plan_sha256']!=study.sha((first_root/'plan.json').read_bytes()):
        raise RuntimeErrorBase('persona_first_review_plan_mismatch')
    for row in review['cases']:
        if row['id'] not in expected or row['id'] in seen:raise RuntimeErrorBase('persona_first_review_case_mismatch')
        old=expected[row['id']];seen.add(row['id'])
        if any(row[k]!=old[k] for k in ('kind','primary_hash','expression_hash')) or row['left_source']!=key['left_source'][row['id']]:
            raise RuntimeErrorBase('persona_first_review_body_mismatch')
        if row.get('choice') not in ('left','right','equal','both_problem'):raise RuntimeErrorBase('persona_first_review_incomplete')
        choice=row['choice'];preference=row['left_source'] if choice=='left' else ('primary' if row['left_source']=='expression' else 'expression') if choice=='right' else choice
        results.append({'id':row['id'],'preference':preference})
    if seen!=set(expected):raise RuntimeErrorBase('persona_first_review_incomplete')
    if len(list((first_root/'positions').glob('*/dispatch.json')))!=16 or any(study.read(first_root/'positions'/p['id']/'result.json')['state']!='completed' for p in plan['positions']):
        raise RuntimeErrorBase('persona_first_batch_unsettled')
    return plan,data,results


def prepare(root,first_root,review_path,*,author_continues=False,trial_kind='real'):
    root,first_root,review_path=root.resolve(),first_root.resolve(),review_path.resolve()
    if not author_continues:raise RuntimeErrorBase('persona_continuation_author_gate_required')
    if root.exists() or root.is_relative_to(study.REPO):raise RuntimeErrorBase('persona_study_target_invalid')
    if trial_kind not in ('real','offline_fixture'):raise RuntimeErrorBase('persona_study_kind_invalid')
    first,first_data,preferences=verify_review(first_root,review_path)
    if trial_kind=='real' and first['trial_kind']!='real':raise RuntimeErrorBase('persona_real_source_required')
    source_root=Path(first['source_root']);source=study.read(source_root/'review-data.json')
    excluded={c['source']['old_case_id'] for c in first_data['cases'] if c['kind']=='saved_real_primary'}
    if len(excluded)!=8 or len(source['cases'])!=48:raise RuntimeErrorBase('persona_continuation_source_count_invalid')
    cases=[]
    with closing(sqlite3.connect((source_root/'instance/data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        for old in source['cases']:
            if old['id'] in excluded:continue
            turn=db.execute('SELECT * FROM turns WHERE id=?',(old['turn_id'],)).fetchone()
            receipt=db.execute('SELECT * FROM expression_main_receipts WHERE turn_id=?',(old['turn_id'],)).fetchone()
            if not turn or not receipt or not receipt['valid'] or receipt['body']!=old['primary'] or receipt['body_hash']!=old['primary_hash'] or study.sha(receipt['body'].encode())!=receipt['body_hash']:
                raise RuntimeErrorBase('persona_source_body_changed')
            profile=json.loads(turn['profile_snapshot'])
            voice=voice_card({'core_traits':profile['core']['data']['core_traits'],'style':profile['expression']['data']['style']})
            cases.append({'id':'R1C-'+old['id'],'kind':'saved_real_primary','title':old['title'],'role':old['persona'],
                'role_name':old['role']['name'],'voice':voice,'draft':receipt['body'],
                'source':{'kind':'durable_primary_receipt','old_case_id':old['id'],'turn_id':old['turn_id'],'receipt_id':receipt['id'],
                    'physical_attempt_id':receipt['physical_attempt_id'],'body_hash':receipt['body_hash'],
                    'core_version':profile['core'].get('id'),'expression_version':profile['expression'].get('id')}})
    if len(cases)!=40:raise RuntimeErrorBase('persona_continuation_source_count_invalid')
    claim=first_root.parent/'remaining40-claim.json'
    if claim.exists():raise RuntimeErrorBase('persona_continuation_already_claimed')
    study.write_once(claim,{'parent_plan_sha256':study.sha((first_root/'plan.json').read_bytes()),'root':str(root),'max_new_calls':40})
    root.mkdir(parents=True);frozen={}
    for directory in ('persona_runtime','experiments/expression_persona_draft'):
        for path in sorted((study.REPO/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.json'):
                relative=path.relative_to(study.REPO);output=root/'frozen-source'/relative;output.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,output);frozen[relative.as_posix()]=study.sha(path.read_bytes())
    positions=[]
    for ordinal,case in enumerate(cases,1):
        path=root/'inputs'/(case['id']+'.json');study.write_once(path,case)
        request,_=build_request(study.model_for(case['id']),build_messages(case['voice'],case['draft']))
        if len(canonical(request))>REQUEST_LIMIT:raise RuntimeErrorBase('persona_request_capacity')
        study.write_once(root/'positions'/case['id']/'request.json',request)
        positions.append({'id':case['id'],'ordinal':ordinal,'kind':case['kind'],'role':case['role'],
            'input_sha256':study.sha(path.read_bytes()),'request_sha256':study.sha(canonical(request).encode())})
    plan={'version':study.CONTINUATION_VERSION,'created_at':timestamp(),'profile':PROFILE,'trial_kind':trial_kind,
        'source_root':str(source_root),'source_files':{name:study.sha((source_root/name).read_bytes()) for name in ('plan.json','review-data.json','review-key.json')},
        'source_hashes':frozen,'task_sha256':study.sha(TASK.encode()),'positions':positions,'max_physical_calls':40,
        'primary_calls':0,'jev_calls':0,'native_calls':0,'paid_selector':0,'paid_splitter':0,'paid_learning':0,
        'per_position_actual_dispatch_max':1,'automatic_retries':0,'stop_on_failure_or_unknown':True,
        'business_inputs':['core_traits_and_expression_style','complete_primary_draft'],
        'parent_first_batch':{'root':str(first_root),'profile':first['profile'],'plan_sha256':study.sha((first_root/'plan.json').read_bytes()),
            'review_sha256':study.sha(review_path.read_bytes()),'excluded_source_cases':sorted(excluded),'original_calls':16},
        'author_continues_after_verified_review':True,'source_uses_original_historical_v8_output':True,
        'profile_results_must_not_be_combined_as_same_version':True,'full_research_new_expression_calls_max':56}
    study.write_once(root/'plan.json',plan);study.write_once(root/'plan-hash.json',{'sha256':study.sha((root/'plan.json').read_bytes())})
    left=['primary']*20+['expression']*20;random.Random(secrets.token_hex(24)).shuffle(left)
    study.write_once(root/'review-key.json',{'plan_sha256':study.sha((root/'plan.json').read_bytes()),'left_source':{p['id']:v for p,v in zip(positions,left)}})
    study.write_once(root/'verified-first-review.json',{'cases':preferences,'author_continuation_instruction':True,'used_as_data_not_model_context':True})
    return plan


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--first-root',type=Path,required=True);parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--continue-after-review',action='store_true');args=parser.parse_args()
    plan=prepare(args.root,args.first_root,args.review,author_continues=args.continue_after_review)
    print(json.dumps({'positions':40,'profile':plan['profile'],'old_primary_calls':0,'first_batch_replayed':False}))


if __name__=='__main__':main()
