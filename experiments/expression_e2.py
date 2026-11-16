"""E2公开合成材料的有限真实学习与本地接线验证，不触碰正式角色。"""
import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path

from persona_runtime.archive import scope
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.expression_assets import ExpressionAssets, ExpressionAssetError
from persona_runtime.expression_candidates import ExpressionCandidates
from persona_runtime.expression_learning import ExpressionLearning
from persona_runtime.models import PersonaSpec, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from experiments.expression_e1.current import build_messages
from experiments.expression_e1.runner import sha


REPO = Path(__file__).resolve().parents[1]
SAMPLES = [
    {'name': '桥南', 'style': '坦率、好奇，轻松时有点幽默，认真时说清步骤。', 'samples': [
        '对方说今天累得不想动，我接一句：那先瘫一会儿，今天的事又不会趁你休息自己跑来。',
        '对方纠正了刚才的说法，我会接：噢，那就按你刚说的这个来。',
        '认真讨论步骤时，先区分最容易混在一起的两个动作，再说实际先做哪一个。']},
    {'name': '宁舟', 'style': '沉静直接，用克制的幽默接话，尊重拒绝。', 'samples': [
        '听到对方说不想解释，我会回：嗯，原因先放着。你在这儿待着就行。',
        '遇到生活里的小荒唐，用一句平平的反问接过去，留点余味，不急着安排解决方案。']},
]


def load(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def prepare(root):
    root = root.resolve()
    if root.exists() or root.is_relative_to(REPO):raise ValueError('fresh_external_e2_root_required')
    root.mkdir(parents=True)
    paths = RuntimePaths(root / 'instance');ConfigStore(paths).initialize();store = Store(paths);store.initialize()
    service, assets, learning = RuntimeService(store), ExpressionAssets(store), ExpressionLearning(store)
    model = {'base_url': 'https://api.deepseek.com', 'name': 'deepseek-flash', 'encrypted_key': ''}
    cases = []
    for sample in SAMPLES:
        spec = generic_persona().to_dict();spec.update(name=sample['name'], expression_style=sample['style'])
        pid = service.create_persona(PersonaSpec.from_dict(spec))['id'];selected = scope(pid, 'desktop')
        assets.configure(pid, {'enabled': True, 'learning_enabled': True, 'learning_hourly_limit': 2}, 0, identifier())
        sources = [assets.register_source(pid, selected, kind='authored_examples', parts=[{'kind': 'text', 'text': value}],
                   permissions=['use', 'learn'], attribution='synthetic E2 evaluation only; not a production learning source', operation_id=identifier()) for value in sample['samples']]
        curated = assets.put(pid, selected, situation='对方很累想休息', style='顺着眼前的小事轻轻接话', keywords=['累','休息'],
                  source_refs=[{'source_id': sources[0]['id'], 'revision': 1}], operation_id=identifier())
        job = learning.prepare(pid, selected, [s['id'] for s in sources], model, identifier())
        cases.append({'persona_id': pid, 'scope': selected, 'name': sample['name'], 'spec': spec,
                      'sources': sources, 'job_id': job['id'], 'curated_asset': curated})
    dependency_paths = [p.relative_to(REPO).as_posix() for p in (REPO / 'persona_runtime').glob('expression_*.py')]
    dependency_paths += ['persona_runtime/schema_expression_assets.py', 'persona_runtime/storage.py',
                         'persona_runtime/private_personas.py', 'persona_runtime/provider_policy.py',
                         'persona_runtime/provider_compat.py', 'persona_runtime/migration.py',
                         'persona_runtime/vendor/maibot_expression_style.py', 'persona_runtime/vendor/maibot_expression_learning_prompt.py',
                         'experiments/expression_e2.py', 'pyproject.toml', 'uv.lock']
    plan = {'version': 'expression-e2-real-learning-v1', 'kind': 'real', 'materials': 'synthetic fixtures; never formal enrollment',
            'source_hashes': {p: sha((REPO / p).read_bytes()) for p in dependency_paths},
            'cases': cases, 'base_url': model['base_url'], 'model': model['name'],
            'max_physical_model_calls': 2, 'max_local_attempts': 3, 'known_unsent_replacement_limit': 1,
            'stop_known_tokens': 30000, 'stop_estimated_cny': '0.50',
            'price_per_million_cny': {'input_conservative': '2', 'output_conservative': '8'},
            'actual_cny': None, 'formal_roles_used': False, 'new_reply_model_calls': 0}
    with store.connection() as db:
        plan['request_hashes'] = {r['id']: r['request_hash'] for r in db.execute('SELECT id,request_hash FROM expression_learning_jobs')}
    atomic_json(root / 'plan.json', plan);atomic_json(root / 'plan-hash.json', {'sha256': sha((root / 'plan.json').read_bytes())})
    return {'prepared_roles': len(cases), 'max_physical_calls': 2, 'paid_calls': 0}


def verify(root, plan):
    if sha((root / 'plan.json').read_bytes()) != load(root / 'plan-hash.json')['sha256']:raise ValueError('e2_plan_changed')
    for p, digest in plan['source_hashes'].items():
        if sha((REPO / p).read_bytes()) != digest:raise ValueError('e2_learning_source_changed')
    with Store(RuntimePaths(root / 'instance')).connection() as db:
        for job, digest in plan['request_hashes'].items():
            row = db.execute('SELECT request_hash FROM expression_learning_jobs WHERE id=?', (job,)).fetchone()
            if not row or row[0] != digest:raise ValueError('e2_learning_request_changed')


def billing(store, plan):
    rows = []
    with store.connection() as db:
        for raw in db.execute('SELECT id,state,attempt_id,usage_json,transport_json,result_json FROM expression_learning_jobs WHERE attempt_id IS NOT NULL'):
            value = dict(raw);usage = json.loads(value['usage_json'] or 'null');result = json.loads(value['result_json'] or '{}')
            unsent = result.get('status') == 'not_dispatched'
            known = isinstance(usage, dict) and all(type(usage.get(k)) is int for k in ('prompt_tokens','completion_tokens'))
            estimated = str((Decimal(usage['prompt_tokens'])*2+Decimal(usage['completion_tokens'])*8)/Decimal(1000000)) if known else None
            rows.append({'id':value['id'],'state':value['state'],'not_dispatched':unsent,'usage':usage,'known':known,'estimated_cny':estimated})
    billable = [r for r in rows if not r['not_dispatched']]
    unknown = sum(not r['known'] for r in billable)
    return {'physical_calls':len(billable),'local_attempts':len(rows),'not_dispatched':sum(r['not_dispatched'] for r in rows),
            'known_tokens':sum(r['usage']['prompt_tokens']+r['usage']['completion_tokens'] for r in billable if r['known']),
            'unknown_usage_records':unknown,'estimated_cny':str(sum((Decimal(r['estimated_cny']) for r in billable),Decimal(0))) if not unknown else None,
            'actual_cny':None,'rows':rows}


async def run(root, credential_home):
    plan = load(root / 'plan.json');verify(root, plan)
    source_config = ConfigStore(RuntimePaths(credential_home.resolve()));model = source_config.load()['model'];require_generation_provider(model)
    if not model['encrypted_key']:raise ValueError('deepseek_key_missing')
    before = sha(source_config.file.read_bytes())
    live = model | {'base_url': plan['base_url'], 'name': plan['model']}
    paths = RuntimePaths(root / 'instance');store = Store(paths);worker = ExpressionLearning(store)
    try:
        for case in plan['cases']:
            verify(root, plan)
            if sha(source_config.file.read_bytes()) != before:raise ValueError('e2_credential_source_changed')
            report = billing(store, plan)
            if report['unknown_usage_records'] or report['physical_calls'] >= 2 or report['local_attempts'] >= 3:
                break
            if report['known_tokens'] >= plan['stop_known_tokens'] or Decimal(report['estimated_cny']) >= Decimal(plan['stop_estimated_cny']):break
            result = await worker.run(case['job_id'], live)
            atomic_json(root / ('result-' + case['persona_id'] + '.json'), result)
            if result.get('result', {}).get('status') == 'not_dispatched' and report['not_dispatched'] == 0:
                with store.connection() as db:
                    old = dict(db.execute('SELECT * FROM expression_learning_jobs WHERE id=?',(case['job_id'],)).fetchone())
                    diagnostic=json.loads(old['transport_json']);assert diagnostic['request_headers_sent'] is False and diagnostic['response_headers_received'] is False and old['raw_response'] is None
                retry = worker.prepare(case['persona_id'],case['scope'],[s['id'] for s in case['sources']],live,'e2-proven-unsent:'+case['job_id'])
                atomic_json(root/'unsent-replacement.json',{'old':case['job_id'],'replacement':retry['id'],'prior_not_dispatched':True})
                result=await worker.run(retry['id'],live);atomic_json(root/('replacement-result-'+case['persona_id']+'.json'),result)
            print(json.dumps({'role':case['name'],'state':result['state'],'proposals':len(result.get('result',{}).get('proposals',[]))},ensure_ascii=False),flush=True)
    finally:
        atomic_json(root/'credential-check.json',{'config_unchanged':sha(source_config.file.read_bytes())==before,'credentials_persisted':False})
    report=billing(store,plan);atomic_json(root/'billing.json',report)
    return {k:v for k,v in report.items() if k!='rows'}


def verify_publication(root):
    plan=load(root/'plan.json');verify(root,plan);store=Store(RuntimePaths(root/'instance'));assets=ExpressionAssets(store);worker=ExpressionLearning(store)
    if (root/'publication-integration.json').exists():return load(root/'publication-integration.json')|{'already_verified':True}
    results=[]
    for case in plan['cases']:
        with store.connection() as db:
            proposals=[dict(r) for r in db.execute("SELECT * FROM expression_asset_proposals WHERE persona_id=? ORDER BY rowid",(case['persona_id'],))]
        if not proposals:raise ValueError('real_learning_proposal_missing')
        candidate=proposals[0];data=json.loads(candidate['data_json'])
        record=worker.publish(case['persona_id'],case['scope'],candidate['id'],'fixture-publication:'+candidate['id'])
        engine=ExpressionCandidates(store);snapshot=engine.select(case['persona_id'],case['scope'],data['situation'])
        rows=engine.validate(case['persona_id'],case['scope'],snapshot['id'],snapshot['data_hash']);assert rows
        material={'persona':case['persona_id'],'role_projection':case['spec'],'history':[],'user':data['situation'],
                  'already_spoken':[],'draft':'先接着眼前的话题聊。','protected':[], 'candidate_ids':[r['id'] for r in rows]}
        fixtures={'assets':[{'id':r['id'],'persona':case['persona_id'],'situation':r['situation'],'style':r['style']} for r in rows]}
        messages,_=build_messages(material,fixtures);assert '返回一个JSON对象' in messages[0]['content']
        results.append({'persona_id':case['persona_id'],'asset':record,'proposal_count':len(proposals),
                        'snapshot':snapshot['id'],'E1_context_connected':True,'new_reply_call':False,'fixture_admin_publication_only':True})
    first=plan['cases'][0];row=results[0]
    with store.connection() as db:
        chosen=db.execute('SELECT source_id,source_revision FROM expression_asset_supports WHERE asset_id=? AND asset_revision=?',(row['asset']['id'],row['asset']['revision'])).fetchone()
    assets.revise_source(first['persona_id'],first['scope'],chosen[0],expected_revision=chosen[1],permissions=['use','learn'],revoke=True,operation_id=identifier())
    try:ExpressionCandidates(store).validate(first['persona_id'],first['scope'],row['snapshot'])
    except ExpressionAssetError:withdrawn=True
    else:withdrawn=False
    assert withdrawn
    other=plan['cases'][1];assert ExpressionCandidates(store).validate(other['persona_id'],other['scope'],results[1]['snapshot'])
    result={'passed':True,'roles':results,'withdrawal_invalidates_snapshot':True,'other_role_unaffected':True,'formal_assets_published':False,'new_model_calls':0}
    atomic_json(root/'publication-integration.json',result);return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','run','verify'));p.add_argument('--root',type=Path,required=True);p.add_argument('--credential-home',type=Path)
    args=p.parse_args()
    value=prepare(args.root) if args.action=='prepare' else asyncio.run(run(args.root,args.credential_home)) if args.action=='run' else verify_publication(args.root)
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
