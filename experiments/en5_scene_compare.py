"""Run the author-selected scene versus v8 at the 16 frozen E.N5 positions."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing, contextmanager
from html import escape
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.reply_batch import ReplySchemaError, validate_reply
from persona_runtime.storage import Store

from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_trial import FirstReceipts
from .en5_decision_d1 import original_outcome
from .en5_decision_packet import baseline_records, file_hash
from .en5_scene_packet import LABELS, freeze_author_review
from .en5_scene_voice import require

STUDY = 'en5-scene-author-selected-v1'


def comparison(items, originals):
    before = {row['position']:row for row in originals}
    require(len(items)==16 and len({r['position'] for r in items})==16,'fixed_positions')
    plan = {}
    for item in items:
        pos, wire = item['position'], item['main']
        baseline = before[pos]
        original = baseline['wire']
        require(wire['model']==original['model']=='deepseek-v4-pro' and not wire.get('tools')
                and wire | {'messages':original['messages']} == original
                and wire['messages'][1:]==original['messages'][1:] and wire['messages'][0]['role']=='system',
                'fixed_comparison_drift')
        plan[pos] = (baseline, item['choice'], wire)
    return plan


def export(folder, binding, contract_hash, plan, results):
    key_path = folder/'blind-key.private.json'
    if not key_path.exists():
        rng = SystemRandom()
        save_once(key_path,{'binding':binding,'labels':{pos:dict(zip('甲乙',rng.sample('AB',2))) for pos in plan}})
    key = read_private(key_path)
    require(key['binding']==binding and set(key['labels'])==set(plan),'blind_key_changed')
    evidence = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
                '<title>场景对照 · 身份与回执</title><style>body{max-width:65rem;margin:auto;padding:1rem;font:16px system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
                '<h1>场景对照 · 身份与回执</h1><pre>',
                escape(json.dumps(results['accounting'],ensure_ascii=False,indent=2)), '</pre>']
    cases = []
    for pos, (original, scene, wire) in plan.items():
        mapping = key['labels'][pos]
        require(set(mapping)==set('甲乙') and set(mapping.values())==set('AB'),'blind_labels')
        options = {}
        for label, arm in mapping.items():
            reply = results['results'][pos][arm]
            options[label] = {k:reply[k] for k in ('body','state')}
            identity = '原 v8 Pro' if arm=='A' else '人工选卡＋Pro · '+LABELS[scene]
            record = {k:v for k,v in reply.items() if k!='body'}
            evidence += ['<details><summary>',escape(f'{pos} · {label} · {identity}'),'</summary><pre>',
                         escape(json.dumps(record,ensure_ascii=False,indent=2)),'</pre></details>']
        history = wire['messages'][1:-1]
        require(len(history)==2*len(original['history'])
                and all(m['role']==('user' if i%2==0 else 'assistant') for i,m in enumerate(history)),
                'history_pairs')
        pairs = [{'turn':entry['turn'],'user':u['content'],'reply':a['content']}
                 for entry,u,a in zip(original['history'],history[::2],history[1::2],strict=True)]
        cases.append({'position':pos,'user':wire['messages'][-1]['content'],'before':pairs,'options':options})
    view = {'study_id':STUDY,'input_contract_sha256':contract_hash,
            'results_sha256':file_hash(folder/'results.private.json'),'blind_key_sha256':file_hash(key_path),'cases':cases}
    template = Path(__file__).with_name('en5_style_review.html').read_text(encoding='utf-8')
    template = template.replace('接话修复16位审阅意见.json','场景手选16位审阅意见.json')
    template = template.replace('角色的接话方式盲审','角色 · 人工选场景与原 v8 对照')
    template = template.replace('角色 · 接话方式盲审','角色 · 人工选场景盲审')
    page = folder/'review/index.html'
    save_text_once(page,template.replace('@@DATA@@',json.dumps(view,ensure_ascii=False).replace('<',r'\u003c')))
    save_text_once(page.with_name('evidence.html'),''.join(evidence)+'</html>')
    return {'review':str(page),'evidence':str(page.with_name('evidence.html')),**results['accounting']}


async def run(root, feedback):
    root = outside_repository(root)
    revision = freeze_author_review(root,feedback)
    packet = read_private(root/'material.private.json')
    prior = outside_repository(packet['prior'])
    source, originals = baseline_records(prior)
    contract = revision/'requests.private.json'
    form = revision/'author-review.private.json'
    hashes = {p.name:file_hash(p) for p in (form, contract, revision/'catalog.json')}
    plan = comparison(read_private(contract),originals)
    folder = revision/'manual-pro'
    folder.mkdir(exist_ok=True)
    permit = {'study':STUDY,'approval':'author_submitted_confirmed_form','approved_files':hashes,
              'before':{p:b['request_sha256'] for p,(b,_,_) in plan.items()},
              'main_only':{p:fingerprint(wire) for p,(_,_,wire) in plan.items()}}
    with closing(InstanceLock(folder/'run.lock')):
        permission = folder/'author-use.private.json'
        require(permission.exists() or not (folder/'calls.sqlite3').exists(),'dispatch_permission_withdrawn')
        binding = save_once(permission,permit)
        terminal = folder/'results.private.json'
        if terminal.exists():
            old = read_private(terminal)
            require(old['binding']==binding,'saved_results_binding')
            return export(folder,binding,hashes[contract.name],plan,old)
        paths = RuntimePaths(source/'home')
        store = Store(paths)
        corpus = DatasetCorpusStore(read_private(source/'selection.private.json')['corpus_root'])
        credentials = ConfigStore(paths).load()['model']

        @contextmanager
        def window():
            with corpus.connection(write=True),store.connection(write=True):
                yield

        def check(_):
            require(file_hash(permission)==binding and all(file_hash(revision/n)==v for n,v in hashes.items()),
                    'dispatch_material_changed')
            require(baseline_records(prior)==(source,originals),'fixed_sources_changed')

        by_slot = {pos+'.pro.body':value[0] for pos,value in plan.items()}

        def route(slot, stage, model):
            baseline = by_slot[slot]
            require(stage=='body' and model==baseline['model_public']
                    and provider_identity(model)==provider_identity(ConfigStore(paths).load()['model']),
                    'provider_changed')
            guard_cloud_dispatch(store,baseline['persona_id'],'main',model)

        journal = FirstReceipts(folder,binding,client=GenerationHTTPClient(),guard=check,
                                authority_window=window,guard_route=route)
        rows = {}
        for pos, (baseline, scene, wire) in plan.items():
            original = original_outcome(baseline)
            candidate = original
            if fingerprint(wire)!=baseline['request_sha256']:
                deps = [baseline['request_sha256'],baseline['body_sha256'],hashes[form.name]]
                reply = await journal.send(pos+'.pro.body','body',(baseline['model_public'],wire),deps,credentials)
                raw_body = reply.pop('content')
                candidate = {**reply,'origin':'author_scene_pro','scene':scene,'body':None}
                if reply['state']=='completed':
                    try:
                        candidate['body'] = validate_reply(raw_body,'context_full',
                                                           baseline['model_public'].get('_expression_snapshot')).text
                    except (ValueError,TypeError,ReplySchemaError):
                        candidate.update(state='failed',error_code='body_validation_failed')
            rows[pos] = {'A':original,'B':candidate}
            print(pos,candidate['state'],flush=True)
        with window(),closing(journal.open()) as db:
            check(None)
            states = dict(db.execute('SELECT state,count(*) FROM requests GROUP BY state'))
            for pos, (baseline, _, wire) in plan.items():
                reply = rows[pos]['B']
                if not reply.get('attempt_id') or reply['origin']=='original_v8':
                    continue
                saved = db.execute('SELECT * FROM requests WHERE attempt_id=?',(reply['attempt_id'],)).fetchone()
                require(saved and json.loads(saved['wire'])==wire and fingerprint(wire)==reply['request_hash'],
                        'first_wire_changed')
                if saved['raw'] is not None:
                    require(digest(saved['raw'])==saved['raw_hash']==reply['raw_hash'],'first_response_changed')
                if reply['state']=='completed':
                    parsed = parse_receipt(saved['raw'], wire['stream'])
                    require(parsed['valid'] and parsed['model']==wire['model'] and saved['http']==200
                            and parsed['usage']==reply['usage'] and validate_reply(
                                parsed['body'],'context_full',baseline['model_public'].get('_expression_snapshot')).text
                            ==reply['body'],'first_body_changed')
        usage = [(r['B'].get('usage') or {}).get('total_tokens') for r in rows.values()
                 if r['B']['origin']=='author_scene_pro' and r['B'].get('attempt_id')]
        accounting = {'physical_requests':sum(states.values()),'original_replies':len(plan),'first_receipts':states,
                      'candidate_states':dict(Counter(r['B']['state'] for r in rows.values())),
                      'selected_scenes':dict(Counter(scene for _,scene,_ in plan.values())),
                      'known_tokens':sum(n for n in usage if type(n) is int),
                      'unknown_usage':sum(type(n) is not int for n in usage),'actual_charge':None}
        result = {'study':STUDY,'binding':binding,'results':rows,'accounting':accounting}
        save_once(terminal,result)
        return export(folder,binding,hashes[contract.name],plan,result)


if __name__=='__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    cli.add_argument('--author-review',type=Path,required=True)
    args = cli.parse_args()
    print(json.dumps(asyncio.run(run(args.root,args.author_review)),ensure_ascii=False))
