"""Replay the reviewed study against both assemblers without contacting a provider."""
import argparse
import asyncio
from contextlib import closing
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.dialogue_receipt import parse_receipt
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_repair_packet import ComparisonAssembly, original_inputs, require


def saved_stage(db, slot, stage, expected):
    row = db.execute("""SELECT r.* FROM uses u JOIN requests r USING(hash)
                        WHERE u.slot=?""",(slot,)).fetchone()
    require(row and row['state']=='completed' and row['http']==200 and digest(row['raw'])==row['raw_hash']
            and row['raw_hash']==stage['raw_hash'] and row['attempt_id']==stage['attempt_id'],
            'trace_receipt_changed')
    wire=json.loads(row['wire'])
    require(wire==expected and fingerprint(wire)==stage['request_hash'],'trace_wire_changed')
    receipt=parse_receipt(row['raw'],wire.get('stream',False))
    require(receipt['valid'] and receipt['model']==wire['model'],'trace_receipt_invalid')
    return receipt['body']


async def replay(state_path, root, old_bundle, new_bundle):
    state=read_private(state_path)
    prior=outside_repository(state['en3_ling_input_contract']).parent
    _,materials=original_inputs(state)
    result=read_private(prior/'results.private.json')
    root=outside_repository(root)
    rows=[]
    with ComparisonAssembly(state_path,root,old_bundle,new_bundle) as assembly, closing(
        sqlite3.connect((prior/'calls.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        for scene,saved in zip(materials['scenes'],result['scenes'],strict=True):
            sid=scene['id']
            require(sid==saved['id'],'trace_scene_changed')
            history,recent=[],[]
            for n,(item,actual) in enumerate(zip(scene['turns'],saved['turns'],strict=True),1):
                entry=actual['bodies']['R']
                require(entry['state']=='completed' and item['text']==actual['user'],'trace_gap')
                stages={s['stage']:s for s in entry['stages']}
                baseline=await assembly.baseline(sid,n,item,history,'O')
                old=assembly.for_turn('O',sid,n,item,history,baseline,[])
                new=assembly.for_turn('R',sid,n,item,history,baseline,recent[-2:])
                old_wires,new_wires={},{}
                slot=f'{sid}.{n:02d}.R.'
                for stage in ('understanding','selection','body'):
                    kind='reply' if stage=='body' else stage
                    previous=thought if stage!='understanding' else None
                    selected=old_audit['requested_ids'] if stage=='body' else []
                    old_wires[stage]=assembly.wire('O',kind,old,previous,selected)[1]
                    if stage!='selection' or assembly.repaired.selectable(new):
                        projected=new_audit['applied_ids'] if stage=='body' else []
                        new_wires[stage]=assembly.wire('R',kind,new,previous,projected)[1]
                    received=saved_stage(db,slot+stage,stages[stage],old_wires[stage])
                    if stage=='understanding':
                        thought=received
                        require(thought==entry['understanding'],'trace_understanding_changed')
                    elif stage=='selection':
                        old_audit=assembly.applied('O',received,old,thought)
                        new_audit=assembly.applied('R',received,new,thought)
                    else:
                        require(received==entry['raw_body'],'trace_reply_changed')
                # Prior displayed words are observed v1 evidence; recent IDs below
                # are the shadow projection of v1 choices, never a claimed v2 output.
                base=baseline[1]['messages']
                msg=new_wires['body']['messages']
                require(msg[:len(base)-1]==base[:-1] and msg[-1]==base[-1],'trace_context_changed')
                optional=msg[len(base)-1:-1]
                require(all(thought not in part['content'] for part in optional),'trace_internal_text_visible')
                recent.append(new_audit['applied_ids'])
                history.append((item,entry['body'],item['accepted_at']))
                rows.append({'scene':sid,'turn':n,'body_observed':entry['body'],'understanding_observed':thought,
                    'old_projection':old_audit,'repair_shadow_projection':new_audit,'original_wires':old_wires,
                    'rebuilt_wires':new_wires,'repaired_optional_blocks':optional,
                    'stage_receipt_sha256':{k:s['raw_hash'] for k,s in stages.items()}})
    trace={'version':'en3-assembly-shadow-v2','prior_result_sha256':state['en3_ling_results_sha256'],
           'repair_bundle_sha256':digest(Path(new_bundle).read_bytes()),'new_provider_requests':0,'rows':rows}
    save_once(root/'repair-replay.private.json',trace)
    return trace


def report(root,trace):
    rows=trace['rows']
    html=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>角色 · 修复回放</title>',
          '<style>body{max-width:58rem;margin:2rem auto;padding:0 1rem;font:16px/1.7 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}section{border-top:1px solid #aaa}h2{font-size:1.16rem}</style><main><h1>上轮回复的修复回放</h1>',
          '<p>以下从上一轮保留的真实理解、选材和发言重建请求。旧版人物正文是实际回执，新版栏目展示在相同材料下会发给模型的内容。本页没有调用模型生成替代说法。</p>',
          '<p>旧版曾把真人正在做的事作为说话习惯交给角色，新候选只让经过筛选的表达方式进入选材。内部理解仍供选择器使用。人物模型直接接收原 v8 的对话及适用表达。</p>']
    for r in rows:
        old,new=r['old_projection'],r['repair_shadow_projection']
        html.extend([f"<section><h2>{escape(r['scene'])} · 第{r['turn']}轮</h2>",
                    '<details><summary>上轮实际人物正文</summary><pre>'+escape(r['body_observed'])+'</pre></details>',
                    f"<p>原请求采用 {len(old['applied_ids'])} 项表达。沿用同一次选择回执，新装配允许其中 {len(new['applied_ids'])} 项。</p>"])
        for stage,name in (('understanding','理解'),('selection','选材'),('body','人物发言')):
            html.append('<details><summary>'+name+'的完整请求对照</summary>')
            for key,title in (('original_wires','上轮实发'),('rebuilt_wires','修复后离线重建')):
                wire=r[key].get(stage)
                content=json.dumps(wire,ensure_ascii=False,indent=2) if wire is not None else '本位置无可选条目，跳过选材。'
                html.extend(['<h3>'+title+'</h3><pre>',escape(content),'</pre>'])
            html.append('</details>')
        html.append('</section>')
    html.append('</main></html>')
    path=outside_repository(root)/'repair-replay.html'
    save_text_once(path,''.join(html))
    return path


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('state','root','old-bundle','new-bundle'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    trace=asyncio.run(replay(args.state,args.root,args.old_bundle,args.new_bundle))
    print(report(args.root,trace))
