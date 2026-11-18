"""Verify and blind the continuous Jev/Pro trial against the original v8 desktop sessions."""
import argparse
from collections import Counter
from contextlib import closing
from html import escape
import json
from pathlib import Path
import sqlite3
from statistics import median

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.knowledge_web_decision import usage
from .corpus_reply_study import read_private,save_once
from .corpus_reply_trial_report import save_text_once
from .en5_d3_live_review import audit_arm,blind_view
from .en5_decision_packet import file_hash
from .en5_scene_continuous import REPAIR,REPAIR_MESSAGE,STUDY,reviewed,style_review
from .en5_scene_packet import LABELS
from .en5_scene_route import SceneSet
from .en5_scene_style import BUBBLE_TARGET, SOCIAL_LIMIT, VERSION as STYLE_VERSION, short_frames, speech, without_directions
from .en5_scene_voice import WAIT_SECONDS,context,decision,jev_request,require


def report(root,*,style_repair=False):
    state,material,rev,use,archive=reviewed(root)
    folder=rev/('continuous-style-repair' if style_repair else 'continuous')
    mode=REPAIR if style_repair else STUDY
    before=None
    if style_repair:
        opinion=folder/'style-review.private.json'
        feedback=style_review(rev,use,opinion)
        use=use|{'version':REPAIR,'style_profile':STYLE_VERSION,'style_feedback_sha256':file_hash(opinion),
                 'prior_continuous_results_sha256':feedback['results_sha256']}
        before=read_private(rev/'continuous/results.private.json')
    grant=read_private(folder/'author-use.private.json')
    require(all(grant.get(k)==v for k,v in use.items())
            and grant['stage_review_sha256']==file_hash(archive/'stage-review.private.json'),'review_author_scope')
    if style_repair:
        require(grant['author_message']==REPAIR_MESSAGE,'repair_author_scope')
    d3=root/'d3/live'
    baseline=audit_arm(d3,'baseline',read_private(d3/'author-use.private.json'),material,{})
    require(baseline==read_private(d3/'results.private.json')['arms']['baseline'],'v8_first_responses_changed')
    cards=SceneSet(rev,use['scene_revision'],read_private(rev/'catalog.json')['persona'])
    catalog=cards.catalog()
    branch=folder/'candidate'
    with closing(sqlite3.connect((branch/'scene-ledger/decisions.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        routes={r['turn']:dict(r) for r in db.execute('SELECT * FROM routes')}
    require(len(routes)==30,'scene_route_count')
    def verify(tid,wire,manifest,details,history,users):
        row=routes.pop(tid)
        require(row['revision']==cards.revision,'scene_receipt_revision')
        targets=[]
        for i,past in enumerate(list(history.values())[-2:]):
            for role,text in [('user',u) for u in past['bubbles']]+[('assistant',past['body'])]:
                targets.append({'text':text,'role':role,'origins':['recent'],
                                'pro_target':{'message_index':i*2+1+int(role=='assistant')}})
        targets.extend({'text':text,'role':'user','origins':['current'],'pro_target':{'message_index':5}} for text in users)
        state_input=context(targets)
        expected=jev_request(use['jev_model'],state_input,catalog['instruction'],catalog['criteria']) if state_input is not None else None
        require(json.loads(row['wire'])==expected and row['request_hash']==fingerprint(expected),'jev_visible_inputs')
        path=branch/'timings'/(tid+'.private.json')
        timing=read_private(path) if path.exists() else {}
        raw=row['raw']
        choice=None
        if raw is None:
            require(row['reason'] in ('budget','timeout','transport_unconfirmed','restart_unknown','restart_before_dispatch')
                    and row['scene']=='fallback','unconfirmed_decision')
            measured={}
        else:
            require(row['http']==timing['http'] and abs(timing['wire_ms']-row['elapsed_ms'])<=.001,'wire_clock')
            if row['http']==200:
                parsed=json.loads(raw)
                require(parsed.get('model')=='jev-1.14.0','response_model_changed')
                selection,reason=decision(raw)
                choice=parsed.get('answers',{}).get('scene')
                measured=usage(parsed) or {}
            else:
                selection,reason='fallback','http_error'
                measured=json.loads(row['usage'] or 'null') or {}
            if row['elapsed_ms']>=WAIT_SECONDS*1000:
                selection,reason='fallback','timeout'
            require((selection,reason)==(row['scene'],row['reason']),'scene_receipt_decision')
        card=cards.card(row['scene'])
        blocks=[b for b in manifest['prompt_assembly']['blocks'] if b['role']=='system']
        require(details['scene_voice']['revision']==cards.revision and details['scene_voice']['scene']==row['scene']
                and details['scene_voice']['card_sha256']==fingerprint(card) and blocks[0]['name']=='scene_voice'
                and sum(b['name']=='scene_voice' for b in blocks)==1,'card_binding')
        system=wire['messages'][0]['content']
        cursor=0
        for b in blocks:
            require(b['start_character']==cursor and b['name'] in
                    ('scene_voice','character','memory_summary','recall_support','explicit_references'),'unexpected_style_block')
            text=system[cursor:cursor+b['characters']]
            require(fingerprint(text)==b['content_hash'] and (b['name']!='scene_voice' or text==card),'card_wire_changed')
            cursor+=b['characters']
        require(cursor==len(system),'unaccounted_system_text')
        return {'scene':row['scene'],'reason':row['reason'],'choice':choice,'elapsed_ms':row['elapsed_ms'],
                'http':row['http'],'started':row['started'],'usage':measured,'timing':timing,
                'raw_sha256':digest(raw.encode()) if raw is not None else None}
    repairs={}
    def project_spoken(tid,raw,details):
        key=details['scene_voice']['scene']
        clean=without_directions(raw)
        spoken=speech(raw,key)
        repairs[tid]={'raw_chars':len(raw),'spoken_chars':len(spoken),
                      'parenthetical_edit':clean!=raw,'shortened':spoken!=clean,
                      'raw_sha256':digest(raw.encode()),'spoken_sha256':digest(spoken.encode())}
        return spoken
    candidate=audit_arm(folder,'candidate',grant,material,{},scene_audit=verify,
                        spoken_projection=project_spoken if style_repair else None)
    require(not routes,'extra_scene_routes')
    arms={'baseline':baseline,'candidate':candidate}
    for value in arms.values():
        summary=value['summary']
        summary['jev_median_s']=summary.pop('flash_median_s')
        summary['jev_tokens']=summary.pop('flash_tokens')
        choices=[r['decision'] for rows in value['sessions'] for r in rows if r['decision']]
        summary['forced_fallback_positions']=summary['fallback_positions']
        summary['fallback_positions']=[r['position'] for rows in value['sessions'] for r in rows
                                       if r['decision'] and r['decision']['scene']=='fallback']
        summary.update(jev_requests=sum(c['started'] is not None for c in choices),
                       scenes=dict(Counter(c['scene'] for c in choices)),
                       reused_connections=sum(c['timing'].get('connection_reused',False) for c in choices))
    if style_repair:
        rows=[r for group in candidate['sessions'] for r in group]
        for r in rows:
            repair=repairs[r['turn']]
            require([s['text'] for s in short_frames(r['body'],True)]==[s['text'] for s in r['segments']],
                    'short_bubble_plan_changed')
            r['style_projection']=repair
        previous=before['arms']['candidate']['sessions']
        candidate['summary']['presentation']={
            'parenthetical_edits':sum(p['parenthetical_edit'] for p in repairs.values()),
            'shortened_social_replies':sum(p['shortened'] for p in repairs.values()),
            'raw_median_chars':median(p['raw_chars'] for p in repairs.values()),
            'spoken_median_chars':median(p['spoken_chars'] for p in repairs.values()),
            'before_session_one_median_chars':median(len(r['body']) for r in previous[0]),
            'repaired_session_one_median_chars':median(len(r['body']) for r in candidate['sessions'][0]),
            'before_session_one_max_chars':max(len(r['body']) for r in previous[0]),
            'repaired_session_one_max_chars':max(len(r['body']) for r in candidate['sessions'][0]),
            'before_session_two_parenthetical_openings':sum(r['body'].lstrip().startswith(('（','(')) for r in previous[1]),
            'repaired_session_two_parenthetical_openings':sum(r['body'].lstrip().startswith(('（','(')) for r in candidate['sessions'][1]),
            'social_limit_chars':SOCIAL_LIMIT,'bubble_target_chars':BUBBLE_TARGET,
            'max_bubble_chars':max(len(s['text']) for r in rows for s in r['segments']),
            'bubbles_over_target':sum(len(s['text'])>BUBBLE_TARGET for r in rows for s in r['segments'])}
    result={'version':mode,'approval_sha256':file_hash(folder/'author-use.private.json'),
            'baseline_sha256':use['baseline_sha256'],'arms':arms,'author_review':None}
    sha=save_once(folder/'results.private.json',result)
    view,key=blind_view(folder,sha,arms,material,use['material_sha256'],mode)
    page=Path(__file__).with_name('en5_d3_live_review.html').read_text(encoding='utf-8')
    page=page.replace('D3 连续对话盲审','Jev 连续对话盲审').replace('D3连续对话盲审意见','Jev连续对话盲审意见')
    page=page.replace('en5-d3-continuous-author-feedback-v1','en5-scene-continuous-author-feedback-v1')
    if style_repair:
        page=page.replace('Jev连续对话盲审意见','Jev修复重跑盲审意见')
    target=folder/'review/index.html'
    save_text_once(target,page.replace('@@DATA@@',json.dumps(view,ensure_ascii=False).replace('<',r'\u003c')))
    lines=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">',
           '<title>连续对话 · 运行记录与身份</title><style>body{max-width:64rem;margin:auto;padding:1rem;font:16px/1.6 system-ui}pre{white-space:pre-wrap}</style>',
           '<h1>连续对话 · 运行记录与身份</h1>']
    for name,value in arms.items():
        lines+=['<h2>'+('原 v8 Pro' if name=='baseline' else 'Jev 1.14 选场景＋Pro')+'</h2><pre>',
                escape(json.dumps(value['summary'],ensure_ascii=False,indent=2)),'</pre>']
    for i,rows in enumerate(candidate['sessions']):
        lines+=['<h2>第'+str(i+1)+'段</h2><p>'+escape(str(key['groups'][i]))+'</p>']
        for row in rows:
            d=row['decision'];ms=d['elapsed_ms']
            lines.append('<p>'+escape(f"{row['position']}　{LABELS[d['scene']]}　{d['reason']}　"+
                                      ('无完整响应' if ms is None else f'{ms/1000:.3f} 秒'))+'</p>')
    save_text_once(target.with_name('evidence.html'),''.join(lines)+'</html>')
    return {'review':str(target),'summaries':{k:v['summary'] for k,v in arms.items()}}


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    cli.add_argument('--style-repair',action='store_true')
    args=cli.parse_args()
    print(json.dumps(report(outside_repository(args.root),style_repair=args.style_repair),ensure_ascii=False))
