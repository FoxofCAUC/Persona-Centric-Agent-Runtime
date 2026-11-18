"""Three independent private-chat sessions per scene using the frozen research variants."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from decimal import Decimal
from html import escape
import json
from pathlib import Path
from statistics import median

from persona_runtime.config import atomic_json
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.instance import InstanceLock
from .body_repair_display import project_display
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import estimated_peak, save_text_once
from .fresh_scene_assembly import Assembly
from .fresh_scene_journal import Journal
from .fresh_scene_review import freeze_key


VERSION = 'fresh-private-dialogue-v1'
BRANCHES = ('B', 'S', 'R')


def require(ok, reason):
    if not ok:
        raise CorpusError('fresh_scene_' + reason)


def checksum(path):
    return digest(Path(path).read_bytes())


def implementation():
    files = ['fresh_scene_assembly.py', 'fresh_scene_material.py', 'body_repair_material.py', 'body_repair_display.py']
    return {f:digest(Path(__file__).with_name(f).read_bytes().replace(b'\r\n',b'\n')) for f in files}


def prepare(root, state_path):
    from .fresh_scene_material import verify_materials
    root = outside_repository(root)
    assembly = Assembly(state_path,root)
    corpus = DatasetCorpusStore(Path(assembly.plan['corpus_root']))
    path = root / 'materials.private.json'
    material = read_private(path)
    verify_materials(corpus,material,assembly.plan)
    scenes = material['scenes']
    require(root == assembly.sources.study.parent/'fresh-scenes-v1', 'canonical_study_root_required')
    require(len({s['id'] for s in scenes}) == len(scenes) and all(s['turns'] for s in scenes), 'scene_identity')
    key_hash = freeze_key(root,[s['id'] for s in scenes])
    plan = {'version':VERSION,'study_id':'fresh-private-dialogue-01','scenes':scenes,
            'materials_sha256':checksum(path),'state_path':str(state_path),'root':str(root),
            'prior_plan_sha256':assembly.sources.selector['study_plan_sha256'],
            'blind_key_sha256':key_hash,'assembly_hashes':implementation(),
            'asset_snapshot_hash':assembly.plan['assets']['snapshot_hash'],
            'source_home':assembly.plan['source_home'],'old_view_id':assembly.plan['view_id'],
            'billing_request_count_cap':None,'retries':0,'formal_activation':False,
            'context':'Each branch starts empty and carries its own actual displayed replies. Source peer replies are not replayed.',
            'request_deduplication':'A byte-equivalent canonical wire request reuses the first response, including failure or unknown status.',
            'routing':'No live tools; ordinary private conversation using revised, previous and original v8 Pro configurations.'}
    path = root/'manifest.private.json'
    save_once(path,plan)
    parts=['<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>新场景输入与三路对照</title><style>body{max-width:62em;margin:2em auto;padding:0 1em;color:#20362d;font:16px/1.7 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}details{padding:.6em;border-bottom:1px solid #ccc}summary{cursor:pointer}</style>'
           '<main><h1>新场景输入与三路对照</h1>',
           f'<p>{len(scenes)}组真实材料，合计{sum(len(s["turns"]) for s in scenes)}轮。主题标题只供审阅。每组都从新会话开始，三条链分别接续自己的实际发言。</p>']
    for s in scenes:
        parts.append(f'<details><summary>{escape(s["id"])} · {escape(s["title"])} · {len(s["turns"])}轮</summary>')
        for t in s['turns']:
            parts.append('<pre>'+escape(t['text'])+'</pre>')
        parts.append('</details>')
    parts.append('</main></html>')
    save_text_once(root/'inputs.html',''.join(parts))
    return path,plan


def missing(state, reason):
    return {'state':state,'body':None,'raw_body':None,'error':reason,'raw_hash':None,'request_hash':None}


async def branch(scene, name, assembly, journal, credentials):
    history, refs, records = [], [], []
    previous_failed = False
    for n, turn in enumerate(scene['turns'], 1):
        if previous_failed:
            records.append(missing('blocked','prior_branch_speech_missing'))
            continue
        current, moment = turn['text'], turn['accepted_at']
        deps = list(refs)
        async def call(stage, model_wire):
            model,wire = model_wire
            return await journal.call(f"{scene['id']}.{n:02}.{name}.{stage}",model,wire,list(deps),credentials)

        if name in ('B','R'):
            baseline = await assembly.baseline(scene['id'], n, current, moment, list(history), name)
        if name == 'B':
            reply = await call('body',baseline)
        else:
            understanding = await call('understanding',assembly.understand(current,moment,history))
            if understanding['state'] != 'completed':
                records.append(missing('blocked','understanding_' + understanding['state']))
                previous_failed = True
                continue
            deps.append(understanding['raw_hash'])
            thought = understanding['body']
            selection = await call('selection',assembly.select(current,moment,history,thought))
            if selection['state'] != 'completed':
                records.append(missing('blocked','selection_' + selection['state']))
                previous_failed = True
                continue
            deps.append(selection['raw_hash'])
            try:
                ids = assembly.parse_ids(selection['body'])
            except CorpusError:
                records.append(missing('blocked','invalid_selected_ids'))
                previous_failed = True
                continue
            request = (assembly.speak_s(current,moment,history,thought,ids) if name == 'S' else
                       assembly.speak_r(*baseline,current,thought,ids))
            reply = await call('body',request)
            reply = reply | {'source_ids':ids, 'understanding_hash':understanding['raw_hash'],
                             'selection_hash':selection['raw_hash']}
        reply = dict(reply,raw_body=reply.get('body'),history_hash=fingerprint(history))
        if reply['state'] == 'completed':
            if name == 'R':
                view = project_display(reply['body'],user_text=current)
                reply.update(body=view['display_body'],removed_spans=view['removed_spans'])
            reply['body_sha256'] = digest(reply['body'].encode())
            history.append((current,reply['body'],moment))
            refs.append(reply['raw_hash'])
        else:
            previous_failed = True
        records.append(reply)
    return records


def accounting(journal):
    cost = Decimal(0)
    counts, totals, by_model = Counter(),Counter(),{}
    unknown_cost = unknown_usage = 0
    times=[]
    with closing(journal.open()) as db:
        logical=db.execute('SELECT count(*) FROM uses').fetchone()[0]
        for row in db.execute('SELECT * FROM requests ORDER BY started'):
            name=json.loads(row['wire'])['model']
            usage=json.loads(row['result'] or '{}').get('usage')
            counts[row['state']]+=1
            per=by_model.setdefault(name,Counter());per['requests']+=1
            if not usage or type(usage.get('total_tokens')) is not int:unknown_usage+=1
            for k,v in (usage or {}).items():
                if type(v) is int:
                    totals[k]+=v
                    per[k]+=v
            estimate=estimated_peak('U' if name=='deepseek-flash' else 'D',usage)
            if estimate is None:unknown_cost+=1
            else:cost+=Decimal(estimate)
            ms=json.loads(row['diagnostics'] or '{}').get('elapsed_ms')
            if ms is not None:times.append(ms)
    return {'attempts':sum(counts.values()),'completed':counts['completed'],'physical_states':dict(counts),
            'logical_slots':logical,'exact_request_reuses':logical-sum(counts.values()),
            'by_model':by_model,'usage':dict(totals),'unknown_usage_requests':unknown_usage,
            'peak_cost_estimate_CNY':str(cost) if not unknown_cost else None,'actual_bill_CNY':None,
            'request_median_ms':median(times) if times else None}


async def run(root, approval_path, *, client=None, load_credentials=None):
    from .fresh_scene_material import verify_materials
    root=outside_repository(root)
    mp=root/'manifest.private.json'
    manifest_hash=checksum(mp); approval_hash=checksum(approval_path)
    plan=read_private(mp); use=read_private(approval_path)
    require(plan['version']==VERSION and Path(plan['root'])==root
            and use['version']=='fresh-scenes-use-v1' and use['manifest_sha256']==manifest_hash
            and Path(use['manifest_path'])==mp and use['approved'] is True
            and 'billing_request_count_cap' in use and use['billing_request_count_cap'] is None, 'scope_changed')
    state_path=Path(plan['state_path'])
    assembly=Assembly(state_path,root)
    require(root==assembly.sources.study.parent/'fresh-scenes-v1'
            and plan['assembly_hashes']==implementation() and plan['blind_key_sha256']==checksum(root/'blind-key.private.json'),
            'frozen_contract_changed')
    corpus=DatasetCorpusStore(Path(assembly.plan['corpus_root']))
    material=read_private(root/'materials.private.json')
    require(checksum(root/'materials.private.json')==plan['materials_sha256']
            and material['scenes']==plan['scenes'] and plan['asset_snapshot_hash']==assembly.plan['assets']['snapshot_hash'],
            'materials_changed')
    verify_materials(corpus,material,assembly.plan)
    def guard(slot):
        require(checksum(mp)==manifest_hash and checksum(approval_path)==approval_hash
                and checksum(root/'materials.private.json')==plan['materials_sha256'], 'authority_changed')
        assembly.guard()
    with closing(InstanceLock(root/'run.lock')):
        journal=Journal(root,manifest_hash,client=client,guard=guard)
        if load_credentials is None:
            from persona_runtime.config import ConfigStore
            from persona_runtime.paths import RuntimePaths
            credentials=ConfigStore(RuntimePaths(Path(plan['source_home']))).load()['model']
        else:
            credentials=load_credentials()
        active=asyncio.Semaphore(3)
        async def scene_session(scene):
            async with active:
                rows=await asyncio.gather(*(branch(scene,k,assembly,journal,credentials) for k in BRANCHES))
                cases=[{'scene_id':scene['id'],'title':scene['title'],'turn':n+1,'input':t['text'],
                        'bodies':{k:v[n] for k,v in zip(BRANCHES,rows,strict=True)}} for n,t in enumerate(scene['turns'])]
                print(json.dumps({'scene':scene['id'],'turns':len(cases),'body_states':dict(Counter(
                    b['state'] for c in cases for b in c['bodies'].values()))}),flush=True)
                return cases
        batches=await asyncio.gather(*(scene_session(s) for s in plan['scenes']))
        cases=[c for group in batches for c in group]
        summary=accounting(journal)
        summary.update(scenes=len(batches),turns=len(cases),body_states=dict(Counter(
            b['state'] for c in cases for b in c['bodies'].values())),presentation_changes=sum(
                bool(c['bodies']['R'].get('removed_spans')) for c in cases))
        results={'version':'fresh-scene-results-v1','study_id':plan['study_id'],'manifest_sha256':manifest_hash,
                 'cases':cases,'summary':summary}
        save_once(root/'results.private.json',results)
        return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'))
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--state',type=Path)
    parser.add_argument('--approval',type=Path)
    args=parser.parse_args()
    if args.action=='prepare':
        path,plan=prepare(args.root,args.state)
        print(json.dumps({'manifest':str(path),'groups':len(plan['scenes']),'turns':sum(len(x['turns']) for x in plan['scenes'])}))
    else:
        print(json.dumps(asyncio.run(run(args.root,args.approval)),ensure_ascii=False,indent=2))
