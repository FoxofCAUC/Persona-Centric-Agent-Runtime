"""冻结材料与有限调用位置；成功、未知及已预留位置均不能重放。"""
import hashlib
import json
from pathlib import Path
import random
import secrets
import shutil

from persona_runtime.models import RuntimeErrorBase,timestamp

VERSION='expression-E3-expanded-review-v1'
REPO=Path(__file__).resolve().parents[2]
PURPOSES=('primary','expression','jev','notice','native','tool_search')


def sha(data):return hashlib.sha256(data).hexdigest()


def digest(value):return sha(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())


def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def write_once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as output:json.dump(value,output,ensure_ascii=False,indent=2)


def validate_fixtures(fixtures):
    if fixtures.get('source')!='authored_public_synthetic':raise RuntimeErrorBase('review_public_fixtures_required')
    ids=set()
    for group in fixtures['groups']:
        if group['id'] in ids or group['persona'] not in fixtures['personas'] or type(group['network']) is not bool:
            raise RuntimeErrorBase('review_group_invalid')
        ids.add(group['id'])
        if len(group['turns'])!=3:raise RuntimeErrorBase('review_group_requires_three_turns')
        for turn in group['turns']:
            if not isinstance(turn.get('user'),str) or not turn['user'].strip():raise RuntimeErrorBase('review_user_text_missing')
            if 'public_task' in turn:
                if not group['network']:raise RuntimeErrorBase('review_public_task_requires_network_group')
                from persona_runtime.knowledge_native import public_task
                public_task(turn['public_task'])


def prepare(root,*,fixtures=None,trial_kind='real'):
    root=root.resolve()
    if root.is_relative_to(REPO):raise RuntimeErrorBase('review_evidence_must_be_outside_source')
    if root.exists():raise RuntimeErrorBase('review_plan_directory_exists')
    fixtures=fixtures or read(Path(__file__).with_name('fixtures.json'));validate_fixtures(fixtures)
    if trial_kind not in ('real','offline_fixture'):raise RuntimeErrorBase('review_trial_kind_invalid')
    root.mkdir(parents=True)
    source_hashes={}
    for directory in ('persona_runtime','experiments/expression_e3_review'):
        for path in sorted((REPO/directory).rglob('*')):
            if path.is_file() and path.suffix in ('.py','.json'):
                rel=path.relative_to(REPO);target=root/'frozen-source'/rel;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,target);source_hashes[rel.as_posix()]=sha(path.read_bytes())
    positions=[]
    for group in fixtures['groups']:
        for index,turn in enumerate(group['turns']):
            positions.append({'id':group['id']+'-'+str(index+1),'group_id':group['id'],'index':index,
                'persona':group['persona'],'network':group['network'],**turn})
    network=sum(p['network'] for p in positions);queries=sum('public_task' in p for p in positions)
    limits={'primary':len(positions),'expression':len(positions),'jev':network,'notice':queries,'native':queries,'tool_search':queries}
    plan={'version':VERSION,'created_at':timestamp(),'trial_kind':trial_kind,'fixtures':fixtures,'positions':positions,
        'limits':limits,'physical_calls_max':sum(limits.values()),'per_slot_per_purpose_max':1,
        'source_hashes':source_hashes,'provider':'deepseek_official','model':'deepseek-flash',
        'expression_protocol':'expression-json-scoped-v3','primary_reply_profile':'released_v8',
        'paid_selector':0,'paid_splitter':0,'paid_learning':0,'automatic_retries_per_slot':0,
        'unknown_or_successful_replay':False,'consecutive_transport_errors_stop':3,
        'presentation':'headless_simulated_presenter; no desktop/user-read claim',
        'comparison':'same-turn saved primary versus post-expression; later turns consume actual expression history',
        'author_review_required_before_E4':True}
    write_once(root/'plan.json',plan);write_once(root/'plan-hash.json',{'sha256':sha((root/'plan.json').read_bytes())})
    choices=['primary']*(len(positions)//2)+['expression']*(len(positions)-len(positions)//2)
    random.Random(secrets.token_hex(24)).shuffle(choices)
    write_once(root/'review-key.json',{'plan_sha256':sha((root/'plan.json').read_bytes()),
        'left_source':{p['id']:choice for p,choice in zip(positions,choices)}})
    return plan


def load(root,*,check_source=True):
    plan=read(root/'plan.json')
    if plan.get('version')!=VERSION or sha((root/'plan.json').read_bytes())!=read(root/'plan-hash.json')['sha256']:
        raise RuntimeErrorBase('review_plan_changed')
    if check_source:
        for rel,expected in plan['source_hashes'].items():
            path=(REPO/rel).resolve()
            if not path.is_relative_to(REPO) or not path.is_file() or sha(path.read_bytes())!=expected:
                raise RuntimeErrorBase('review_frozen_source_changed')
    return plan


def reserve(root,plan,slot,purpose,request):
    if purpose not in PURPOSES or slot not in {p['id'] for p in plan['positions']}:raise RuntimeErrorBase('review_call_scope_invalid')
    previous=list((root/'positions').glob('*/calls/*.dispatch.json'))
    if len(previous)>=plan['physical_calls_max'] or sum(read(p)['purpose']==purpose for p in previous)>=plan['limits'][purpose]:
        raise RuntimeErrorBase('review_call_budget_used')
    path=root/'positions'/slot/'calls'/(purpose+'.dispatch.json')
    if path.exists():raise RuntimeErrorBase('review_call_already_reserved')
    row={'position':slot,'purpose':purpose,'ordinal':len(previous)+1,'request_hash':digest(request),
         'started_at':timestamp(),'trial_kind':plan['trial_kind'],'state':'dispatched'}
    write_once(path,row);write_once(path.with_name(purpose+'.request.json'),request)
    return row
