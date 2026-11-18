"""Freeze two real 15-turn timelines for local author review. No provider client."""
import argparse
from datetime import datetime
from html import escape
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_store import outside_repository
from .corpus_reply_study import read_private,save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_material import excluded_inputs,load_fragment
from .en5_decision_contract import require
from .en5_decision_packet import file_hash
from .fresh_scene_material import LOCAL_TIME,clean_text,fragment_messages,text_key


def timeline(fragment,rows,first,title,used):
    anchor=next(i for i,m in enumerate(rows) if m['ordinal']==first)
    speaker=rows[anchor]['role']
    if anchor:
        prev=rows[anchor-1]
        require(prev['role']!=speaker or prev['kind']!='text' or rows[anchor]['epoch']-prev['epoch']>=5,
                'partial_first_batch')
    batches=[]
    for i in range(anchor,len(rows)):
        m=rows[i]
        if m['role']!=speaker or m['kind']!='text':continue
        if batches and i==batches[-1][-1]+1 and m['epoch']-rows[i-1]['epoch']<5:
            batches[-1].append(i)
        else:
            if len(batches)==15:break
            batches.append([i])
        require(clean_text(m) and m['verbatim'] and m['target_candidate'] and not m['flags'],'review_text_ineligible')
    require(len(batches)==15,'incomplete_review_window')
    turns=[]
    for n,b in enumerate(batches,1):
        bubbles=[{'id':rows[i]['message_id'],'ordinal':rows[i]['ordinal'],'text':rows[i]['text'],
                  'sha256':rows[i]['text_hash'],'epoch':rows[i]['epoch']} for i in b]
        previous=batches[n-2][-1] if n>1 else anchor-1
        gap=rows[previous+1:b[0]]
        seconds=rows[b[0]]['epoch']-rows[previous]['epoch'] if n>1 else 0
        require(0<=seconds<=1800 and not any(g['kind']=='text' and g['role']==speaker for g in gap),'broken_review_window')
        omitted=[{'ordinal':g['ordinal'],'role':g['role'],'kind':g['kind'],'epoch':g['epoch'],
                  'text':g['text'] if clean_text(g) else None} for g in gap]
        turns.append({'n':n,'bubbles':bubbles,'gap_seconds':seconds,'omitted_local_only':omitted,
                      'exact_prior_text_overlap':sum(text_key(x['text']) in used for x in bubbles)})
    return {'title':title,'speaker':speaker,'first':first,'group_id':fragment['group_id'],
            'fragment_id':fragment['id'],'split':fragment['split'],'turns':turns}


def build(reviews, selection):
    require(isinstance(selection,list) and len(selection)==2,'review_selection_count')
    state=read_private(reviews/'prompt-review-state.json')
    desktop=read_private(reviews/'en5-ling-desktop-v1/materials.private.json')
    oldsel=read_private(reviews/'en5-ling-desktop-v1/selection.private.json')
    fresh=outside_repository(state['fresh_scene_study_root'])
    plan=read_private(fresh.parent/'el4-run-v1/plan.private.json')
    corpus=DatasetCorpusStore(oldsel['corpus_root'])
    with corpus.connection() as db:
        db.execute('BEGIN')
        view=corpus._view_status(db,oldsel['view_id'])
        require(view['payload_hash']==oldsel['view_payload_hash'] and view['source_proof_hash']==oldsel['source_proof_hash'],
                'review_source_changed')
        groups,used=excluded_inputs(db,view,plan,read_private(fresh/'materials.private.json'))
        for name in ('el5-en3-ling-20261118','el5-en3-ling-repair-v2'):
            prior=read_private(reviews/name/'materials.private.json')
            require(prior['materials_hash']==fingerprint({k:v for k,v in prior.items() if k!='materials_hash'}),'prior_changed')
            for scene in prior['scenes']:
                groups.add(scene['group_id'])
                used.update(text_key(b['text']) for t in scene['turns'] for b in t['bubbles'])
        for part in desktop['sessions']:
            for turn in part['turns']:
                groups.update(b['group_id'] for b in turn['bubbles'])
                used.update(text_key(b['text']) for b in turn['bubbles'])
        sessions=[]
        for spec in selection:
            require(isinstance(spec,dict) and set(spec)=={'split','first','title'},'invalid_review_selection')
            split,first,title=spec['split'],spec['first'],spec['title']
            require(split in ('final_holdout','dev_review') and type(first) is int and first>0
                    and isinstance(title,str) and 0<len(title)<=80,'invalid_review_selection')
            fragment,rows=(load_fragment(db,view,first) if split=='final_holdout'
                           else fragment_messages(db,view,first,groups))
            require(fragment['split']==split and fragment['group_id'] not in groups,'reviewed_group_reused')
            sessions.append(timeline(fragment,rows,first,title,used))
            groups.add(fragment['group_id'])
    return {'version':'en5-d3-real-input-proposal-v1','status':'awaiting_author_review','view_id':view['id'],
            'view_sha256':view['payload_hash'],'source_proof_sha256':view['source_proof_hash'],
            'corpus_root':str(corpus.root),'decision_material_sha256':state['en5_decision_confirmed_requests_sha256'],
            'sessions':sessions}


def prepare(root):
    root=outside_repository(root)
    reviews=root.parent
    folder=root/'d3/materials'
    data=build(reviews,read_private(folder/'selection.private.json'))
    checksum=save_once(folder/'materials.proposed.private.json',data)
    page=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>D3 真人原话审阅</title>',
          '<style>body{max-width:54rem;margin:1rem auto;padding:1rem;font:16px/1.6 system-ui}article{border-top:1px solid #ccd7cf;padding:.65rem 0}blockquote{white-space:pre-wrap;overflow-wrap:anywhere;background:#ebf3ee;padding:.6rem;margin:.3rem 0}small{color:#4c6259}</style>',
          '<h1>D3 · 两段连续真人原话</h1><p>两条链各自从空会话开始，依次接续角色在本链实际显示的发言。下面引文是待审用户输入，时间、原对方插话、附件位置和标题只在本机展示。</p>',
          '<p>基线由官方 Pro 发言；候选由官方 Flash 读取获审性格、当轮和最近两轮实际发言作判断，再由 Pro 发言。两段均关闭联网。材料等待作者审阅及 D2 的阶段结论。</p>']
    for s in data['sessions']:
        partition='留出分区' if s['split']=='final_holdout' else '开发分区 · 与此前研究的来源组分离'
        page+=['<h2>'+escape(s['title'])+'</h2><p>'+partition+'</p>']
        for t in s['turns']:
            bubbles=t['bubbles']
            at=datetime.fromtimestamp(bubbles[0]['epoch'],LOCAL_TIME).isoformat()
            page+=[f'<article><b>第{t["n"]}轮</b> <small>{at}　距上一轮 {t["gap_seconds"]} 秒</small>',
                   *['<blockquote>'+escape(b['text'])+'</blockquote>' for b in bubbles]]
            if t['omitted_local_only']:
                page.append('<details><summary>这两轮之间省略的原始消息</summary><ul>')
                for m in t['omitted_local_only']:
                    label='同一人' if m['role']==s['speaker'] else ('系统' if m['role']=='SYS' else '原对方')
                    page.append('<li>'+escape(label+' · '+(m['text'] if m['text'] else m['kind']))+'</li>')
                page.append('</ul></details>')
            page.append('</article>')
    page.append('<p>材料版本　'+checksum[:16]+'</p></html>')
    target=folder/'index.html'
    save_text_once(target,''.join(page))
    return target,checksum,file_hash(target)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    print(prepare(cli.parse_args().root))
