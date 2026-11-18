"""Audit the four completed desktop conversations and export a whole-session blind review."""
import argparse
from collections import Counter
from contextlib import closing
from html import escape
import json
from math import ceil
from pathlib import Path
from secrets import SystemRandom
import sqlite3
from statistics import median

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.deepseek_reply_decision import parse_receipt as parse_tool
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.paths import RuntimePaths
from persona_runtime.reply_batch import validate_reply
from persona_runtime.storage import Store
from persona_runtime.turn_input import input_text
from .corpus_reply_study import read_private,save_once
from .corpus_reply_trial_report import save_text_once
from .en5_decision_contract import persona_projection,require
from .en5_decision_packet import file_hash
from .en5_d3_live import source_inputs
from .en5_experience_replay import plan_for,snapshot
from .en5_experience_setup import MANIFEST


def summarize(rows):
    waits=sorted(r['first_visible_s'] for r in rows)
    decisions=[r['decision'] for r in rows if r['decision']]
    flash_ms=sorted(d['elapsed_ms'] for d in decisions if d['elapsed_ms'] is not None)
    return {'main':len(rows),'decision':len(decisions),'bubble_count':sum(len(r['segments']) for r in rows),
            'reasons':dict(Counter(d['reason'] for d in decisions)),
            'fallback_positions':[r['position'] for r in rows if r['decision'] and r['decision']['reason']!='selected'],
            'first_visible_median_s':round(median(waits),3),'first_visible_p95_s':round(waits[ceil(.95*len(waits))-1],3),
            'main_median_s':round(median(r['main_ms'] for r in rows)/1000,3),
            'flash_median_s':round(median(flash_ms)/1000,3) if flash_ms else None,
            'main_tokens':sum(r['main_usage']['total_tokens'] for r in rows if r['main_usage'].get('total_tokens') is not None),
            'flash_tokens':sum(d['usage']['total_tokens'] for d in decisions if d['usage'].get('total_tokens') is not None),
            'unknown_usage':sum(r['main_usage'].get('total_tokens') is None for r in rows)
                            +sum(d['usage'].get('total_tokens') is None for d in decisions),'actual_charge':None}


def audit_arm(live,arm,approved,material,notes,*,scene_audit=None,spoken_projection=None):
    folder=live/arm;paths=RuntimePaths(folder/'home');store=Store(paths)
    binding=read_private(paths.home/'runtime/d3-binding.json')
    scope=read_private(paths.home/MANIFEST)
    script=read_private(folder/'materials.private.json')
    require(binding['parent_use_sha256']==file_hash(live/'author-use.private.json')
            and binding['author_use_sha256']==file_hash(folder/'author-use.private.json')
            and binding['material_sha256']==file_hash(folder/'materials.private.json'),'audit_scope')
    data=[]
    ledger=paths.home/'private/en5-d3/desktop.sqlite3'
    with store.connection() as db, closing(sqlite3.connect(ledger.as_uri()+'?mode=ro',uri=True)) as side:
        side.row_factory=sqlite3.Row
        for table,count in [('main_calls',30),('decisions',30 if arm=='candidate' and scene_audit is None else 0)]:
            require(side.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==count,'audit_call_count')
        for table in ('organization_attempts','dialogue_generation_stages','expression_runs'):
            require(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0,'unexpected_extra_stage')
        require(db.execute('SELECT count(*) FROM attempts').fetchone()[0]==30,'audit_main_attempt_count')
        for i,sid in enumerate(scope['sessions'],1):
            plan=plan_for(script,scope,i,file_hash(folder/'materials.private.json'))
            input_rows,batches,turns=snapshot(paths,binding,sid,plan)
            require(set(input_rows)=={b['request_id'] for grp in plan for b in grp},'audit_input_slots')
            require(len(turns)==len(batches)==len(plan)==15,'audit_session_count')
            history={};rows=[]
            for n,grp in enumerate(plan,1):
                users=[b['text'] for b in material['sessions'][i-1]['turns'][n-1]['bubbles']]
                require([b['text'] for b in grp]==users,'audit_script')
                t=turns[batches[n]]
                require(t['status']=='completed' and t['reply_event_id'] and not t['cancelled_at'],'incomplete_desktop_turn')
                tid=t['id'];body=t['text'];record=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                first=db.execute('SELECT first_epoch FROM input_batches WHERE id=?',(batches[n],)).fetchone()[0]
                main=side.execute('SELECT * FROM main_calls WHERE turn=?',(tid,)).fetchone()
                require(main and main['http']==200,'missing_pro_receipt')
                wire=json.loads(main['wire']); received=parse_receipt(main['raw'],wire['stream'])
                details=json.loads(db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(tid,)).fetchone()[0])
                spoken=spoken_projection(tid,received['body'],details) if spoken_projection else received['body']
                require(received['valid'] and received['model']==wire['model']=='deepseek-v4-pro'
                        and validate_reply(spoken,'context_full',record['expression_snapshot']).text==body,'main_receipt_mismatch')
                audit=json.loads(record['usage_json'])['generation_retry']['attempts']
                require(len(audit)==1 and audit[0]['request_hash']==fingerprint(wire) and audit[0]['status']=='completed'
                        and audit[0]['output_fingerprint']==digest(spoken.encode()),'attempt_mismatch')
                manifest=json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(tid,)).fetchone()[0])
                require(manifest['input_snapshot_hash']==fingerprint(wire['messages']),'audit_main_context_hash')
                if scene_audit is None:
                    require(details['en5_d3']['final_messages_sha256']==manifest['input_snapshot_hash'],'d3_main_context_hash')
                expected=[]; targets=[]
                for group in details['history_groups']:
                    past=history[group['turn_id']]
                    require(past['last']<=first,'history_not_yet_visible')
                    expected.extend([{'role':'user','content':past['user']},{'role':'assistant','content':past['body']}])
                    if group['kind']=='recent':
                        targets.extend(('user',u,['recent']) for u in past['bubbles'])
                        targets.append(('assistant',past['body'],['recent']))
                expected.append({'role':'user','content':input_text(db,record)})
                targets.extend(('user',u,['current']) for u in users)
                require(expected==wire['messages'][1:] and not details['references']['references'],'cross_chain_history')
                decision=None
                if scene_audit is not None:
                    decision=scene_audit(tid,wire,manifest,details,history,users)
                elif arm=='candidate':
                    raw=side.execute('SELECT * FROM decisions WHERE turn=?',(tid,)).fetchone()
                    require(raw and raw['http']==200 and raw['raw'] and raw['reason'] is not None,'flash_receipt_missing')
                    flash=json.loads(raw['wire']); args=parse_tool(raw['raw'],flash)
                    require(flash['model']=='deepseek-flash' and flash['tools'][0]['function']['strict'],'decision_route')
                    input_payloads=[(m['role'],json.loads(m['content'])) for m in flash['messages'][1:]]
                    require(input_payloads==[(role,{'id':j,'source':orig,'text':v}) for j,(role,v,orig) in enumerate(targets,1)],
                            'flash_context_drift')
                    prefix,person=flash['messages'][0]['content'].rsplit('\n\n',1)
                    projection=persona_projection(json.loads(record['profile_snapshot']))
                    require(prefix==notes['flash'] and {k:json.loads(person)[k] for k in projection}==projection,'flash_persona_drift')
                    choice=json.loads(raw['choice']) if raw['choice'] else None
                    reason=raw['reason']
                    if reason=='selected':
                        require(args['valid'] and json.loads(args['body'])==choice,'flash_choice_drift')
                        header,fields=wire['messages'][0]['content'].rsplit('\n\n',1)
                        handoff=json.loads(fields);address=handoff['target'];target=targets[choice['target']-1]
                        addressed=wire['messages'][address['message_index']]
                        member=address.get('member',1)
                        referent=addressed['content']
                        if addressed['role']=='user' and not referent==target[1]:
                            referent=json.loads(referent.rsplit('\n',1)[1])[member-1]['text']
                        require(header.endswith('\n\n'+notes['pro']) and handoff['action']==choice['action']
                                and handoff['task_state']==choice['task_state'] and address['role']==addressed['role']==target[0]
                                and referent==target[1],'pro_handoff_drift')
                    else:
                        require(details['en5_d3']['visible_messages_sha256']==fingerprint(wire['messages']),'fallback_changed_wire')
                    decision={'reason':reason,'choice':choice,'elapsed_ms':raw['elapsed_ms'],'usage':args['usage'] or {},
                              'parse_error':args.get('error_code'),'raw_sha256':digest(raw['raw'])}
                presentation=side.execute('SELECT * FROM plans WHERE turn=?',(tid,)).fetchone()
                segments=json.loads(presentation['frames'])
                paints=list(side.execute('SELECT idx,at FROM visible WHERE turn=? ORDER BY idx',(tid,)))
                require(not presentation['stopped'] and presentation['event']==t['reply_event_id'] and
                        ''.join(f['text'] for f in segments)==body and [p['idx'] for p in paints]==list(range(len(segments))),
                        'paint_receipt_mismatch')
                stamps=[p['at'] for p in paints]
                require(stamps==sorted(stamps) and first<=main['started']<=stamps[0],'invalid_clock')
                history[tid]={'user':expected[-1]['content'],'body':body,'bubbles':users,'last':stamps[-1]}
                rows.append({'position':f'{i}-{n:02d}','turn':tid,'user':users,'body':body,'decision':decision,
                             'segments':[{'text':f['text'],'at':p['at']} for f,p in zip(segments,paints,strict=True)],
                             'first_visible_s':round(stamps[0]-first,3),'main_ms':json.loads(main['diagnostics'])['elapsed_ms'],
                             'main_usage':received['usage'] or {},'request_hash':fingerprint(wire),'raw_sha256':digest(main['raw'])})
            data.append(rows)
    return {'sessions':data,'summary':summarize([r for group in data for r in group])}


def report(root):
    state,material,expected=source_inputs(root); live=root/'d3/live'
    approved=read_private(live/'author-use.private.json')
    require(all(approved.get(k)==v for k,v in expected.items()),'audit_use')
    notes=read_private(Path(state['en5_decision_confirmed_requests']))['author_text']
    arms={arm:audit_arm(live,arm,approved,material,notes) for arm in ('baseline','candidate')}
    record={'version':'en5-d3-continuous-results-v1','approval_sha256':file_hash(live/'author-use.private.json'),
            'material_sha256':approved['material_sha256'],'arms':arms,'author_decision':None}
    sha=save_once(live/'results.private.json',record)
    view,key=blind_view(live,sha,arms,material,approved['material_sha256'],'en5-d3-continuous-review-v1')
    return export_d3(live,view,key,arms)


def blind_view(live,sha,arms,material,material_sha,version):
    key_path=live/'blind-key.private.json'
    if not key_path.exists():
        save_once(key_path,{'results':sha,'groups':[dict(zip('甲乙',SystemRandom().sample(list(arms),2))) for _ in range(2)]})
    key=read_private(key_path);require(key['results']==sha and len(key['groups'])==2,'audit_blind_key')
    groups=[]
    for i,mapping in enumerate(key['groups']):
        require(set(mapping)==set('甲乙') and set(mapping.values())==set(arms),'audit_blind_mapping')
        rows=[]
        for n,t in enumerate(material['sessions'][i]['turns']):
            options={}
            for label,arm in mapping.items():
                r=arms[arm]['sessions'][i][n]
                options[label]={'body':r['body'],'bubbles':[x['text'] for x in r['segments']],
                                'offsets':[round(1000*(x['at']-r['segments'][0]['at'])) for x in r['segments']],
                                'wait':r['first_visible_s']}
            rows.append({'n':n+1,'users':[x['text'] for x in t['bubbles']],'options':options})
        groups.append({'title':material['sessions'][i]['title'],'rows':rows})
    view={'version':version,'results_sha256':sha,'key_sha256':file_hash(key_path),
          'materials_sha256':material_sha,'groups':groups}
    return view,key


def export_d3(live,view,key,arms):
    page=Path(__file__).with_suffix('.html').read_text(encoding='utf-8')
    target=live/'review/index.html'
    save_text_once(target,page.replace('@@DATA@@',json.dumps(view,ensure_ascii=False).replace('<',r'\u003c')))
    evidence=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
              '<title>D3 运行证据</title><style>body{max-width:60rem;margin:auto;padding:1rem;font:16px/1.6 system-ui}pre{white-space:pre-wrap}</style><h1>D3 运行证据与身份</h1>']
    for arm,value in arms.items():
        evidence.append('<h2>'+arm+'</h2><pre>'+escape(json.dumps(value['summary'],ensure_ascii=False,indent=2))+'</pre>')
    for i,labels in enumerate(key['groups'],1):
        evidence.append(f'<h2>第{i}段</h2><p>'+escape(str(labels))+'</p>')
        for row in arms['candidate']['sessions'][i-1]:
            d=row['decision']
            evidence.append('<p>'+escape(f'{row["position"]}　{d["reason"]}　{(d["elapsed_ms"] or 0)/1000:.3f} 秒　{d["choice"] or d["parse_error"] or ""}')+'</p>')
    save_text_once(target.with_name('evidence.html'),''.join(evidence)+'</html>')
    return target,{k:v['summary'] for k,v in arms.items()}


if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('--root',type=Path,required=True)
    args=cli.parse_args()
    page,counts=report(outside_repository(args.root));print(page);print(json.dumps(counts,ensure_ascii=False))
