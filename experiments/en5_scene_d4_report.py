"""Verify D4 from real transport, native tool and desktop display receipts."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime,timezone
from html import escape
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from statistics import median

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.paths import RuntimePaths
from persona_runtime.presentation import batch_view
from persona_runtime.storage import Store
from persona_runtime.turn_input import input_text
from .corpus_reply_study import read_private,save_once
from .corpus_reply_trial_report import save_text_once
from .en5_d3_live import replay_plans
from .en5_decision_packet import file_hash
from .en5_experience_replay import snapshot
from .en5_experience_report import digest,get_control,read_turn,summarize
from .en5_experience_setup import MANIFEST
from .en5_scene_d4 import AUTHOR_QUERY_STUDY,TARGETED_STUDY,materials
from .en5_scene_packet import LABELS
from .en5_scene_style import short_frames,speech,without_directions
from .en5_scene_voice import WAIT_SECONDS,context,decision,jev_request,require


def paint(phase,source,body,at):
    return {'phase':phase,'source_id':source,'body_sha256':digest(body),
            'first_visible_at':datetime.fromtimestamp(at,timezone.utc).isoformat()}


def collect(root,feedback,*,targeted=False,author_input=None):
    _,rev,_,material,folder,use=materials(root,feedback,targeted=targeted,author_input=author_input)
    focused='replay_slice' in use
    branch=folder/'candidate'; paths=RuntimePaths(branch/'home')
    manifest=read_private(paths.home/MANIFEST)
    binding=read_private(paths.home/'runtime/d4-binding.json')
    require(read_private(folder/'author-use.private.json')==use and binding['parent_use_sha256']==file_hash(folder/'author-use.private.json')
            and binding['author_use_sha256']==file_hash(branch/'author-use.private.json'),'d4_author_binding')
    script=read_private(branch/'materials.private.json')
    plans=replay_plans(script,manifest,file_hash(branch/'materials.private.json'),use.get('replay_slice'))
    target_count=sum(map(len,plans.values()))
    require(set(plans)==set(binding['sessions']),'d4_replay_sessions')
    catalog=read_private(rev/'catalog.json')
    cards=read_private(rev/'author-review.private.json')['bodies']
    require(fingerprint(catalog)==use['scene_revision'] and catalog['cards']=={k:fingerprint(v) for k,v in cards.items()},'d4_voice_revision')
    groups=[]
    totals=Counter();unknown=Counter();actions=Counter()
    tool_counts=Counter()
    with Store(paths).connection() as db,closing(sqlite3.connect(
        (paths.home/'private/en5-d3/desktop.sqlite3').as_uri()+'?mode=ro',uri=True)) as side,closing(sqlite3.connect(
        (branch/'scene-ledger/decisions.sqlite3').as_uri()+'?mode=ro',uri=True)) as scene:
        side.row_factory=scene.row_factory=sqlite3.Row
        require(side.execute('SELECT count(*) FROM main_calls').fetchone()[0]==target_count,'d4_main_count')
        require(scene.execute('SELECT count(*) FROM routes').fetchone()[0]==target_count,'d4_scene_count')
        earlier_session_sources=set()
        for sid,slots in plans.items():
            part=manifest['sessions'].index(sid)+1
            session_acquired=set()
            sessionscript=material['sessions'][part-1]
            accepted,batches,turns=snapshot(paths,binding,sid,slots)
            require(len(slots)==len(batches)==len(turns) and len(accepted)==sum(map(len,slots)),'d4_input_count')
            prior_sources=set();history={};rows=[]
            public_at=next(g[0]['n'] for g in slots if g[-1]['public_task'])
            for slot in slots:
                n=slot[0]['n']
                t=turns[batches[n]];tid=t['id']
                users=[b['text'] for b in sessionscript['turns'][n-1]['bubbles']]
                require(users==[b['text'] for b in slot] and t['status']=='completed' and t['reply_event_id'],'d4_unfinished')
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                route=scene.execute('SELECT * FROM routes WHERE turn=?',(tid,)).fetchone()
                require(route and route['revision']==use['scene_revision'],'d4_scene_binding')
                targets=[]
                for i,past in enumerate(rows[-2:]):
                    events=[('user',u) for u in past['user']]
                    if past['notice']:
                        events.append(('assistant',past['notice']))
                    events.append(('assistant',past['body']))
                    targets.extend({'text':txt,'role':role,'origins':['recent'],
                                    'pro_target':{'message_index':2*i+1+int(role=='assistant')}} for role,txt in events)
                targets.extend({'text':txt,'role':'user','origins':['current'],'pro_target':{'message_index':5}} for txt in users)
                data=context(targets)
                expected=jev_request(use['jev_model'],data,catalog['instruction'],catalog['criteria']) if data is not None else None
                require(json.loads(route['wire'])==expected and fingerprint(expected)==route['request_hash'],'d4_scene_history')
                if route['http']==200:
                    require(json.loads(route['raw']).get('model')=='jev-1.14.0','d4_scene_model')
                    k,why=decision(route['raw'])
                    if route['elapsed_ms']>=1000*WAIT_SECONDS: k,why='fallback','timeout'
                    require((k,why)==(route['scene'],route['reason']),'d4_scene_decision')
                else:
                    require(route['scene']=='fallback','d4_scene_failure')
                key=route['scene'];main=side.execute('SELECT * FROM main_calls WHERE turn=?',(tid,)).fetchone()
                wire=json.loads(main['wire']);parsed=parse_receipt(main['raw'],wire['stream'])
                require(main['http']==200 and parsed['valid'] and parsed['model']==wire['model']=='deepseek-v4-pro'
                        and speech(parsed['body'],key)==t['text'],'d4_pro_receipt')
                diag=json.loads(main['diagnostics'])
                physical=json.loads(turn['usage_json'])['generation_retry']['attempts']
                require(len(physical)==1 and physical[0]['status']=='completed'
                        and physical[0]['request_hash']==fingerprint(wire)
                        and physical[0]['usage']==parsed['usage'] and physical[0]['output_fingerprint']==digest(t['text'])
                        and diag.get('raw_complete') and diag['network_route']=='system_proxy' and diag['request_headers_sent']
                        and diag['response_sha256']==sha256(main['raw']).hexdigest(),
                        'd4_main_first_response')
                contexts=list(db.execute("""SELECT m.manifest_json,c.details_json FROM context_uses c
                    JOIN attempts a ON a.id=c.attempt_id JOIN context_manifests m ON m.attempt_id=a.id
                    WHERE c.turn_id=? AND a.status='completed' AND m.task_type='dialogue'""",(tid,)))
                require(len(contexts)==1,'d4_published_context')
                m,details=map(json.loads,contexts[0])
                require(details['scene_voice']['card_sha256']==fingerprint(cards[key]) and wire['messages'][0]['content'].startswith(cards[key])
                        and fingerprint(wire['messages'])==m['input_snapshot_hash'],'d4_pro_card')
                expected_history=[]
                for group in details['history_groups']:
                    past=history[group['turn_id']]
                    expected_history.extend([{'role':'user','content':past['user']},{'role':'assistant','content':past['reply']}])
                require(wire['messages'][1:]==expected_history+[{'role':'user','content':input_text(db,turn)}],'d4_pro_history')
                plan=side.execute('SELECT * FROM plans WHERE turn=?',(tid,)).fetchone()
                shown=batch_view(t['text'])[0]
                frames=json.loads(plan['frames']);visible=list(side.execute('SELECT idx,at FROM visible WHERE turn=? ORDER BY idx',(tid,)))
                require(not plan['stopped'] and plan['event']==turn['reply_event_id'] and ''.join(x['text'] for x in frames)==shown
                        and short_frames(shown,True)==frames and [i['idx'] for i in visible]==list(range(len(frames))),'d4_visible_main')
                paints={'main':paint('main',turn['reply_event_id'],shown,visible[0]['at'])}
                notice=get_control(db,'knowledge.search_notice.turn.'+tid)
                if notice:
                    observed=side.execute('SELECT * FROM scene_notices WHERE turn=?',(tid,)).fetchone()
                    body=db.execute('SELECT text FROM events WHERE id=?',(notice['event_id'],)).fetchone()[0]
                    require(observed and observed['source']==notice['notice_id'] and observed['body']==fingerprint(body)
                            and notice.get('fully_presented') and observed['at']<=main['started'],'d4_notice_paint')
                    paints['notice']=paint('notice',notice['notice_id'],body,observed['at'])
                    reply=side.execute("SELECT * FROM d4_calls WHERE turn=? AND stage='conversation_search_notice'",(tid,)).fetchone()
                    req=json.loads(reply['wire']);value=parse_receipt(reply['raw'],req['stream'])
                    require(reply['http']==200 and value['valid'] and value['model']=='deepseek-v4-pro'
                            and without_directions(value['body'])==body and req['messages'][0]['content'].startswith(cards[key])
                            and json.dumps(body,ensure_ascii=False) in wire['messages'][0]['content'],'d4_spoken_notice')
                data=read_turn(db,turn,paints,prior_sources,displayed_body=shown)
                if not data['main_remote_receipt']:
                    # The raw SSE client attests raw_complete + response_sha256 separately from ChatCompletionsClient.
                    require(sum(c['stage']=='main' and c['attempt_id']==physical[0]['id'] for c in data['calls'])==1,
                            'd4_main_attempt_identity')
                    data['issues'].remove('缺少与正文匹配的 Pro 网络回执')
                    data['main_remote_receipt']=True
                require(data['main_visible_epoch'] is not None and data['user']==users
                        and data['explicit_public_tasks']==[x['public_task'] for x in slot if x['public_task']],'d4_turn_proof')
                data['body']=shown
                data['prior_session_source_count']=len(earlier_session_sources & set(details['knowledge']['source_ids']))
                if data['material_acquired']:
                    record=get_control(db,'knowledge.conversation.record.'+tid)
                    session_acquired.update(source['id'] for source in record['sources'])
                data['frames']=[x['text'] for x in frames]
                data['scene']=key;data['scene_reason']=route['reason']; data['position']=f'{part}-{n:02d}'
                raw_calls=list(side.execute('SELECT * FROM d4_calls WHERE turn=? ORDER BY started',(tid,)))
                web=[c for c in raw_calls if c['stage']=='conversation_web_decision']
                require(len(web)<=1,'replayed_web_decision')
                data['web_choice']=None
                if web and web[0]['http']==200:
                    answer=json.loads(web[0]['raw'])
                    require(answer['model']=='jev-1.14.0','d4_web_model')
                    data['web_choice']=answer['answers']['route']['choice']
                    require(data['jev_need_web']==(data['web_choice']=='web'),'d4_web_outcome')
                if focused:
                    require(len(web)==1,'d4_targeted_web_request')
                    web_input=json.loads(web[0]['wire'])['state']
                    data['web_cache']={'source_hints':len(web_input['available_sources']),
                                       'note_chars':len(web_input['cached_public_notes']),
                                       'public_task_available':web_input['public_task_available'],
                                       'sent_sources':len(details['knowledge']['source_ids'])}
                    require(web_input['public_task_available']==(n==public_at)
                            and (n>public_at or not (web_input['available_sources'] or web_input['cached_public_notes'])),
                            'd4_targeted_input_isolation')
                    if n<public_at or (n==public_at and not data['material_acquired']):
                        require(not details['knowledge']['source_ids'],'d4_targeted_unrelated_sources')
                    if author_input is not None and n==public_at:
                        require(web_input['explicit_public_query_requested'] is True,'d4_author_query_not_recognized')
                        data['explicit_public_request']=True
                for c in raw_calls:
                    if c['stage']=='conversation_native':
                        req=json.loads(c['wire'])
                        require(n==public_at and notice and c['started']>=observed['at'] and
                                req['thinking']['type']=='enabled' and req['reasoning_effort']=='high'
                                and req['model']=='deepseek-flash','d4_search_order')
                    actions[c['stage']]+=1
                data['designated_search']=n==public_at
                data['designated_followup']=n==public_at+1
                data['verified_search']=bool(data['designated_search'] and data['web_choice']=='web' and
                                              data['real_native'] and data['material_acquired'])
                data['verified_followup']=bool(data['designated_followup'] and rows[-1]['verified_search']
                                               and data['web_choice']=='none' and data['reused_without_search'])
                for c in data['calls']:
                    if c['tokens'] is None: unknown[c['stage']]+=1
                    else: totals[c['stage']]+=c['tokens']
                    tool_counts.update(c.get('completed_tools',{}))
                u=json.loads(route['usage'] or 'null') or {}
                if type(u.get('total_tokens')) is int:totals['scene']+=u['total_tokens']
                else:unknown['scene']+=1
                history[tid]={'user':wire['messages'][-1]['content'],'reply':'\n'.join(x for x in [data['notice'],shown] if x)}
                rows.append(data)
            groups.append(rows)
            earlier_session_sources.update(session_acquired)
    all_rows=sum(groups,[])
    metrics=summarize(all_rows)
    summary={'turns':len(all_rows),'scene_requests':target_count,'main_requests':target_count,'managed_requests':dict(actions),
             'known_tokens':dict(totals),'unknown_usage':dict(unknown),'native_tools':dict(tool_counts),
             'tool_estimated_cny':metrics['tool_estimated_cny_known'],'actual_charge':None,
             'first_visible_median_s':median(r['first_visible_s'] for r in all_rows),
             'visible_bubbles':sum(len(r['frames']) for r in all_rows),'eligible':metrics['eligible_turns'],
             'search_positions':[r['position'] for r in all_rows if r['verified_search']],
             'reuse_positions':[r['position'] for r in all_rows if r['verified_followup']],
             'prior_session_source_positions':[r['position'] for r in all_rows if r['prior_session_source_count']],
             'web_choices':dict(Counter(r['web_choice'] or 'unconfirmed' for r in all_rows))}
    summary['engineering_ready']=bool(summary['eligible']==30 and len(summary['search_positions'])==2 and summary['reuse_positions'])
    if focused:
        original=rev/'d4-approved-material/verified/results.private.json'
        require(file_hash(original)==use['original_d4_results_sha256'],'d4_targeted_historical_results')
        previous=read_private(original)['summary']
        summary['verified_d4_searches']=sorted(set(previous['search_positions'])|set(summary['search_positions']))
        summary['verified_d4_reuses']=sorted(set(previous['reuse_positions'])|set(summary['reuse_positions']))
        summary['engineering_ready']=bool(previous['eligible']==30 and summary['eligible']==target_count
                and len(summary['verified_d4_searches'])>=2 and summary['verified_d4_reuses'])
    record={'study':use['version'],'grant_sha256':file_hash(folder/'author-use.private.json'),
            'material_sha256':use['material_sha256'],'groups':groups,'summary':summary}
    result_sha=save_once(folder/'verified/results.private.json',record)
    page=folder/'verified/review/index.html'
    save_text_once(page,html(record,result_sha))
    return {'review':str(page),**summary}


def html(record,sha):
    summary=record['summary']
    explicit=record['study']==AUTHOR_QUERY_STUDY
    targeted=record['study']==TARGETED_STUDY or explicit
    title='角色 · D4 明确查询补验' if explicit else '角色 · D4 定向补验' if targeted else '角色 · D4 联网验收'
    items=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>'+title+'</title><style>body{max-width:55rem;margin:auto;padding:1rem;background:#faf9f5;color:#24312e;font:16px/1.6 system-ui}article{border-top:1px solid #bbb;padding:8px 0}p,pre{white-space:pre-wrap;overflow-wrap:anywhere}blockquote{margin:.3rem 0;background:#eaf0e6;width:fit-content;padding:.35rem .7rem;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}select,textarea,button{font:inherit}textarea{width:96%}</style>',
           '<h1>%s</h1><p>已显示 %s/%s 轮，已核实带可见预告的搜索 %s/%s 处，指定后续轮直接复用 %s/1 处。</p>'%
           (title,summary['turns'],summary['turns'] if targeted else 30,len(summary['search_positions']),
            1 if targeted else 2,len(summary['reuse_positions'])),
           '<details><summary>用量、工具和首泡等待</summary><pre>'+escape(json.dumps(summary,ensure_ascii=False,indent=2))+'</pre></details>']
    if targeted:
        items.append('<p>联同原 D4 回执，已核实 %d/2 处独立联网及 %d 处同段复用。</p>'
                     %(len(summary['verified_d4_searches']),len(summary['verified_d4_reuses'])))
    if summary['prior_session_source_positions']:
        items.append('<p>第二段有 %d 轮正文请求带入了第一段的公开来源。逐轮的来源数可在“查找与回执”中核对，这些记录单独统计。</p>'
                     %len(summary['prior_session_source_positions']))
    for i,rows in enumerate(record['groups'],1):
        items.append('<h2>明确查找与同题后续</h2>' if explicit else
                     '<h2>原第二段 · 定向回放</h2>' if targeted else f'<h2>第{i}段</h2>')
        for r in rows:
            items.append('<article><h3>'+r['position']+'</h3>')
            for text in r['user']:items.append('<p>对方　'+escape(text)+'</p>')
            if r['notice']:items.append('<blockquote>角色　'+escape(r['notice'])+'</blockquote>')
            for text in r['frames']:items.append('<blockquote>角色　'+escape(text)+'</blockquote>')
            items+=['<details><summary>查找与回执</summary><pre>'+escape(json.dumps({
                '场景':LABELS[r['scene']],'场景判断状态':r['scene_reason'],'公开任务':r['explicit_public_tasks'],
                '联网决定':r['web_choice'],'实际取得资料':r['material_acquired'],'同段资料复用':r['reused_without_search'],
                '指定后续轮复用':r['verified_followup'],'等待秒数':r['first_visible_s'],'问题':r['issues'],
                '来自前一段的来源数':r['prior_session_source_count'],
                **({'联网判断输入的资料状态':r['web_cache']} if targeted else {}),
                '检索记录':r['retrieval_errors'],'用途回执':r['calls']},ensure_ascii=False,indent=2))+'</pre></details></article>']
    items.append('<h2>审阅意见</h2><form id="review">')
    first_fields=[] if targeted else [('第一段自然度','natural_1'),('第一段等待','wait_1')]
    for label,key in first_fields+[('第二段自然度','natural_2'),('第二段等待','wait_2'),('费用','cost'),('整体体验','overall')]:
        items.append(f'<p><label>{label} <select name="{key}"><option value="">未评</option><option>可接受</option><option>需要修复</option></select></label></p>')
    items.append('<label>备注<textarea name="note"></textarea></label><button type="button" id="export">导出审阅意见</button></form>')
    items.append('<script>const sha='+json.dumps(sha)+''',form=document.getElementById('review');
const k='pcar-d4-'+sha;try{for(const [name,v] of Object.entries(JSON.parse(localStorage.getItem(k)||'{}')))form.elements[name].value=v}catch{}
form.oninput=()=>{try{localStorage.setItem(k,JSON.stringify(Object.fromEntries(new FormData(form))))}catch{}};
document.getElementById('export').onclick=()=>{const data={version:'en5-d4-review-v1',results_sha256:sha,...Object.fromEntries(new FormData(form))};const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='E.N5-角色-D4联网审阅意见.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1500)};
</script></html>''')
    page=''.join(items)
    if explicit:
        return page.replace('en5-d4-review-v1','en5-d4-author-query-review-v1').replace('D4联网审阅意见','D4明确查询补验审阅意见')
    return page.replace('en5-d4-review-v1','en5-d4-targeted-review-v1').replace('D4联网审阅意见','D4定向补验审阅意见') if targeted else page


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True);cli.add_argument('--feedback',type=Path,required=True)
    mode=cli.add_mutually_exclusive_group()
    mode.add_argument('--targeted',action='store_true');mode.add_argument('--author-input',type=Path)
    args=cli.parse_args()
    print(json.dumps(collect(outside_repository(args.root),args.feedback,
                             targeted=args.targeted,author_input=args.author_input),ensure_ascii=False))
