"""Run the 16 separately authorized Flash decisions. Reuse the verified D1 Pro receipt on exact equality."""
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
from persona_runtime.instance import InstanceLock
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, guard_cloud_dispatch, provider_identity
from persona_runtime.reply_batch import ReplySchemaError, validate_reply
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_trial import FirstReceipts
from .en3_repair_feedback import unpack
from .en5_decision_contract import DEADLINE_SECONDS, pro_wire, require, validate_decision
from .en5_decision_d1 import approved
from .en5_decision_packet import baseline_records, build, file_hash


def materials(root):
    packet, requests, contract_sha, author_sha = approved(root)
    state = read_private(root.parent/'prompt-review-state.json')
    signoff = Path(state['en5_decision_d1_stage_review'])
    stage = read_private(signoff)
    d1 = root/'d1/results.private.json'
    require(signoff.parent == root/'d1' and stage['approved'] is True
            and file_hash(signoff) == state['en5_decision_d1_stage_review_sha256']
            and stage['results_sha256'] == file_hash(d1)
            and stage['feedback_sha256'] == state['en5_decision_d1_feedback_sha256'], 'd1_stage_approval')
    source, originals = baseline_records(Path(packet['prior']))
    lookup = {row['position']: row for row in originals}
    paths = RuntimePaths(source/'home')
    cfg = ConfigStore(paths).load()
    model = {'base_url':packet['official_endpoint'], 'name':'deepseek-flash', 'max_tokens':128,
             'timeout':cfg['model']['timeout'], '_json_output':True}
    permit = {'version':'en5-d2-use-v1','purpose':'dialogue_decision','approved':True,
              'instance_id':cfg['instance_id'],'persona_id':originals[0]['persona_id'],
              'provider':provider_identity(model),'material_approval':author_sha,'requests':contract_sha,
              'd1_stage_review':file_hash(signoff),'d1_results':file_hash(d1),
              'slots':{row['position']+'.decision':row['flash_sha256'] for row in requests['positions']},
              'pro_use':'only_valid_changed_decisions'}
    return packet, requests, read_private(d1), lookup, source, model, permit


def read_use(folder, permit):
    path = folder/'author-use.private.json'
    require(path.is_file(), 'd2_use_missing')
    actual = read_private(path)
    require({k:actual.get(k) for k in permit} == permit
            and isinstance(actual.get('author_message'),str) and actual['author_message'].strip(), 'd2_use_missing')
    return file_hash(path)


def grant_file(folder, paths, permit):
    author_path = folder/'author-use.private.json'
    author_hash = read_use(folder,permit)
    policy = PrivatePersonas(paths).record_for_persona(permit['persona_id'])
    require(policy and policy['state']=='en5_visible_copy', 'd2_copy_required')
    data = {**permit, 'version':'en5-fixed-decision-use-v1','home':str(paths.home.resolve()),
            'author_use':str(author_path), 'author_use_sha256':author_hash, 'copy_binding':policy['_copy_binding']}
    raw = (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode()
    key = digest(raw)
    return paths.home/'private/en5/decision-uses'/(key+'.json'),data,key


def register(folder, paths, permit):
    path,data,key=grant_file(folder,paths,permit)
    require(save_once(path,data)==key,'grant_serialization')
    return key


def interpret(record, case):
    if record['state'] != 'completed':
        if record['state'] == 'failed' and (record.get('error_code') or '').startswith('reply_decision_'):
            return None, 'invalid'
        return None, record['state']
    try:
        result = validate_decision(unpack(record['content'].encode()), case['targets'])
    except (TypeError, ValueError, RuntimeErrorBase):
        return None, 'invalid'
    return result, 'undetermined' if result['target'] is None else 'selected'


def body_request(case, row, note, predicted, status):
    wire=pro_wire(case['original'],note,predicted,case['targets']) if status=='selected' else case['original']
    checksum=fingerprint(wire)
    if checksum==row['original']:return 'v8',wire
    if checksum==row['pro_sha256']:return 'D1',wire
    require(status=='selected' and predicted!=row['decision'],'unexpected_pro_request')
    return 'D2',wire


async def run(root, *, strict_tool=False):
    root = outside_repository(root)
    folder = root/'d2'
    if strict_tool:
        from .en5_decision_repair import repair_folder
        folder = repair_folder(root)
    with closing(InstanceLock(folder/'run.lock')):
        packet, docs, d1, originals, source, flash, permit = materials(root)
        require(fingerprint(build(Path(packet['prior']),packet['upstream']))==fingerprint(packet),'source_changed')
        journal_type, client = FirstReceipts, GenerationHTTPClient()
        if strict_tool:
            from .en5_decision_repair import load as load_repair
            from .en5_decision_tool import DecisionToolHTTPClient, ToolFirstReceipts
            by_slot, permit = load_repair(root,packet,docs,flash,permit)
            journal_type, client = ToolFirstReceipts, DecisionToolHTTPClient()
        else:
            by_slot = {c['position']+'.decision':(flash,c['flash']) for c in docs['positions']}
        author = read_use(folder,permit)
        paths = RuntimePaths(source/'home')
        store = Store(paths)
        path,_,grant=grant_file(folder,paths,permit)
        require(path.is_file() and file_hash(path)==grant,'d2_independent_use_required')
        binding = fingerprint([author,grant,permit])
        terminal = folder/'results.private.json'
        if terminal.exists():
            saved = read_private(terminal)
            require(saved['binding']==binding,'result_binding')
            return report(folder,saved,packet,d1)
        corpus = DatasetCorpusStore(read_private(source/'selection.private.json')['corpus_root'])
        def guard(_):
            require(read_use(folder,permit)==author and approved(root)[2:] == (permit['requests'],permit['material_approval'])
                    and file_hash(root/'d1/author-stage-review.private.json')==permit['d1_stage_review']
                    and file_hash(root/'d1/results.private.json')==permit['d1_results'],'d2_use_changed')
            if strict_tool:
                from .en5_decision_repair import CONTRACT, REVIEW
                require(file_hash(folder/CONTRACT)==permit['repair_contract_sha256']
                        and file_hash(folder/REVIEW)==permit['repair_review_sha256']
                        and file_hash(root/'d2/results.private.json')==permit['original_d2_results_sha256'],
                        'd2_repair_use_changed')
            _, live = baseline_records(Path(packet['prior']))
            require({b['position']:b for b in live}==originals,'source_changed')
        @contextmanager
        def window():
            with corpus.connection(write=True), store.connection(write=True):
                yield
        def route(slot,stage,model):
            if stage=='decision':
                require(slot in by_slot and model==by_slot[slot][0],'flash_route')
                guard_cloud_dispatch(store,permit['persona_id'],'dialogue_decision',model,
                                     decision_use=(grant,slot,by_slot[slot][1]))
            else:
                require(stage=='body' and slot.endswith('.body') and model==originals[slot[:-5]]['model_public'],
                        'pro_route')
                guard_cloud_dispatch(store,permit['persona_id'],'main',model)
        journal = journal_type(folder,binding,client=client,guard=guard,
                                authority_window=window,guard_route=route)
        credentials = ConfigStore(paths).load()['model']
        require(credentials['base_url']==flash['base_url'] and credentials['name']=='deepseek-v4-pro','credential_route')
        outcomes = {}
        for case,row in zip(packet['cases'],docs['positions'],strict=True):
            pos = case['position']
            require(row['position']==pos and row['scope']==case['scope'],'turn_changed')
            base = originals[pos]
            slot=pos+'.decision'
            dec = await journal.send(slot,'decision',by_slot[slot],[case['scope'],author],credentials)
            predicted, status = interpret(dec,case)
            ms = dec.get('elapsed_ms')
            slow = ms >= 1000*DEADLINE_SECONDS if ms is not None else None
            origin,wire=body_request(case,row,docs['author_text']['pro'],predicted,status)
            wh = fingerprint(wire)
            if origin=='v8':
                body = d1['results'][pos]['A']
            elif origin=='D1':
                body = d1['results'][pos]['B']
            else:
                body = await journal.send(pos+'.body','body',(base['model_public'],wire),
                                          [case['scope'],row['original'],dec['raw_hash']],credentials)
                raw = body.pop('content')
                body['body'] = None
                if body['state']=='completed':
                    try:
                        body['body'] = validate_reply(raw,'context_full',base['model_public'].get('_expression_snapshot')).text
                    except (ReplySchemaError,ValueError,TypeError):
                        body.update(state='failed',error_code='body_validation_failed')
            require(body['request_hash']==wh,'wrong_body_request')
            outcomes[pos] = {'ref':row['decision'],'decision':predicted,'decision_status':status,'flash':dec,
                             'over_deadline':slow,'body_origin':origin,'body':body}
            print(pos,status,origin,body['state'],flush=True)
        guard(None)
        with closing(journal.open()) as db:
            ledger = db.execute('SELECT slot,wire,http,state,raw,raw_hash FROM uses JOIN requests USING(hash)').fetchall()
        usage = Counter()
        unknown_usage=0
        for rec in ledger:
            pos,stage=rec['slot'].split('.')
            physical=outcomes[pos]['flash' if stage=='decision' else 'body']
            sent=json.loads(rec['wire'])
            require(fingerprint(sent)==physical['request_hash'],'wire_hash')
            if rec['raw'] is not None:
                require(digest(rec['raw'])==rec['raw_hash']==physical['raw_hash'],'receipt_hash')
            if rec['state']=='completed':
                parsed=journal.receipt(rec['raw'],sent)
                require(rec['http']==200 and parsed['valid'] and parsed['model']==sent['model']
                        and parsed['usage']==physical['usage'],'physical_receipt')
                if stage=='decision':
                    require(physical['content']==parsed['body'],'flash_body')
                elif physical['state']=='completed':
                    require(validate_reply(parsed['body'],'context_full',originals[pos]['model_public'].get('_expression_snapshot')).text
                            ==physical['body'],'pro_body')
            tokens=(physical.get('usage') or {}).get('total_tokens')
            if type(tokens) is int:usage[stage]+=tokens
            else:unknown_usage+=1
        stats={'physical':dict(Counter(r['slot'].split('.')[1] for r in ledger)),
               'states':dict(Counter(o['decision_status'] for o in outcomes.values())),
               'identical_decisions':sum(o['ref']==o['decision'] for o in outcomes.values()),
               'body_origins':dict(Counter(o['body_origin'] for o in outcomes.values())),
               'over_3_seconds':sum(o['over_deadline'] is True for o in outcomes.values()),
               'known_tokens':dict(usage),'unknown_usage':unknown_usage,'actual_charge':None}
        saved={'study':permit['version'],'binding':binding,'cases':outcomes,'stats':stats,'d1_sha256':permit['d1_results']}
        save_once(terminal,saved)
        return report(folder,saved,packet,d1)


def report(folder,saved,packet,d1):
    key_path=folder/'blind-key.private.json'
    if not key_path.exists():
        rng=SystemRandom()
        save_once(key_path,{'binding':saved['binding'],'labels':{
            c['position']:dict(zip('甲乙',rng.sample(['D1','D2'],2))) for c in packet['cases']}})
    key=read_private(key_path)
    require(key['binding']==saved['binding'] and set(key['labels'])==set(saved['cases']),'d2_blind_binding')
    page=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>D2 接话判断与正文</title>',
          '<style>body{max-width:56rem;margin:auto;padding:1rem;font:16px/1.65 system-ui}article{border-top:1px solid #ccd7cf;padding:.6rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf3ee;padding:.65rem}summary{cursor:pointer}</style>',
          '<h1>D2 · 角色的接话判断与正文</h1><p>每轮可先读两份匿名正文，再展开判断、身份与耗时。相同正文折叠呈现。3秒为桌面回退期限，本批保留首次完整判断。</p>']
    for c in packet['cases']:
        pos=c['position']; item=saved['cases'][pos]
        labels=key['labels'][pos]
        require(set(labels)=={'甲','乙'} and set(labels.values())=={'D1','D2'},'d2_bad_labels')
        bodies={'D1':d1['results'][pos]['B']['body'],'D2':item['body']['body']}
        user=escape(c['original']['messages'][-1]['content'])
        page.extend([f'<article><h2>{pos}</h2><p>{user}</p><details><summary>实际前文</summary>',
                     *['<pre>'+escape(('对方　' if m['role']=='user' else '角色　')+m['content'])+'</pre>'
                       for m in c['original']['messages'][1:-1]],'</details>'])
        if bodies['D1']==bodies['D2'] and bodies['D1'] is not None:
            page.append('<details><summary>甲乙正文相同 · 展开全文</summary><pre>'+escape(bodies['D1'])+'</pre></details>')
        else:
            for label,arm in labels.items():
                page.append('<b>'+label+'</b><pre>'+escape(bodies[arm] or '该位置没有完整正文')+'</pre>')
        def fmt(v):
            return ' · '.join(str(v[k]) for k in ('target','action','task_state')) if v else '未取得有效判断'
        ms=item['flash']['elapsed_ms']
        wait='未知' if ms is None else f'{ms/1000:.2f} 秒'
        state={'selected':'有效','undetermined':'未定','invalid':'字段无效','unknown':'结果未知',
               'failed':'调用失败','blocked':'未派发'}.get(item['decision_status'],item['decision_status'])
        rel='该轮按桌面期限将回退' if item['over_deadline'] else ''
        page.extend(['<details><summary>作者参照、Flash 判断与正文身份</summary>',
                     '<p>作者　'+escape(fmt(item['ref']))+'</p><p>Flash　'+escape(fmt(item['decision']))+'</p>',
                     '<p>'+escape(f'{state}　{wait}　{rel}　正文来源 {item["body_origin"]}')+'</p>',
                     '<p>'+escape('　'.join(f'{k} {v}' for k,v in labels.items()))+'</p></details></article>'])
    target=folder/'review/index.html'
    page.append('</html>')
    save_text_once(target,''.join(page))
    return {**saved['stats'],'review':str(target)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--strict-tool',action='store_true')
    args=parser.parse_args()
    print(json.dumps(asyncio.run(run(args.root,strict_tool=args.strict_tool)), ensure_ascii=False))
