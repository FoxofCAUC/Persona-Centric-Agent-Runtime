"""Rebuild fixed, author-visible v8 Pro requests using the real context renderer. No transport."""
import argparse
from contextlib import closing
from html import escape
import json
from pathlib import Path
import sqlite3

from experiments.corpus_reply_study import read_private, save_once
from experiments.corpus_reply_trial_report import save_text_once
from experiments.en5_experience_material import verify_sources
from experiments.en5_experience_setup import binding_at, validate_use
from persona_runtime.archive import fingerprint, read_exact, turn_scope
from persona_runtime.character_context import select_context
from persona_runtime.context_m4 import build_context
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.profile import encode
from persona_runtime.provider_compat import build_request
from persona_runtime.provider_policy import require_generation_provider
from persona_runtime.style_study import ARMS, FIELD, KEY, VERSION, require
from persona_runtime.turn_input import load_turn_input


STUDY = 'en5-ling-style-factors-01'
SELECTED = ((3,4,6,7,11,12,13,15), (3,5,9,10,12,13,14,15))
LABEL = {'B':'原 v8 Pro','P':'只改人物材料','H':'只改额外历史','PH':'两项合用'}


def file_hash(path):
    return digest(path.read_bytes())


def implementation():
    repo=Path(__file__).resolve().parents[1]
    names=('persona_runtime/style_study.py','persona_runtime/context_policy.py','persona_runtime/context_m4.py',
           'persona_runtime/context_builders.py','persona_runtime/archive.py','persona_runtime/archive_query.py',
           'persona_runtime/chat.py','persona_runtime/profile.py','persona_runtime/turn_input.py',
           'persona_runtime/references.py','persona_runtime/companion_im.py',
           'persona_runtime/companion_native.py','persona_runtime/character_context.py','persona_runtime/provider_compat.py',
           'experiments/en5_style_packet.py')
    return {n:digest((repo/n).read_bytes().replace(b'\r\n',b'\n')) for n in names}


def frozen_evidence(db, turn, details):
    retrieval=details['retrieval']
    scope=turn_scope(db,turn)
    groups={}
    orders={}
    for tid in retrieval['window_turn_ids']:
        group=read_exact(db,scope,{'kind':'turn','id':tid})
        require(group.get('usable') and group['session_id']==turn['session_id'], 'history_withdrawn')
        groups[tid]=group
        orders[tid]=db.execute('SELECT rowid FROM turns WHERE id=?',(tid,)).fetchone()[0]
    return {'pool':list(groups.values()),'orders':orders,'context_mode':retrieval['mode'],'retrieval':retrieval,
            'items':[{'kind':item['kind'],'group':groups[item['turn_id']],'rank':rank}
                     for rank,item in enumerate(details['history_groups'])]}


def prior_turn(db, scope, t, accepted_input, audit):
    tid=t['turn_id']
    row=db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone()
    require(row is not None,'missing_turn')
    turn=dict(row)
    require(turn['persona_id']==scope['persona_id'] and turn['channel']=='desktop' and turn['status']=='completed'
            and read_exact(db,turn_scope(db,turn),{'kind':'turn','id':tid}).get('usable'), 'source_turn_changed')
    q=db.execute("""SELECT m.manifest_json,c.details_json,b.raw_fingerprint,e.text,a.usage_json
        FROM context_manifests m JOIN context_uses c ON c.attempt_id=m.attempt_id
        JOIN reply_batches b ON b.attempt_id=m.attempt_id JOIN attempts a ON a.id=m.attempt_id
        JOIN events e ON e.id=? WHERE m.turn_id=? AND a.turn_id=? AND a.status='completed'""",
        (turn['reply_event_id'],tid,tid)).fetchall()
    require(len(q)==1, 'first_receipt_required')
    manifest,details=map(json.loads,q[0][:2])
    attempts=json.loads(q[0][4])['generation_retry']['attempts']
    require(len(attempts)==1 and attempts[0]['status']=='completed','first_attempt_required')
    attempt=attempts[0]
    texts=[x['text'] for x in load_turn_input(db,turn)]
    require(t['user']==texts==[b['text'] for b in accepted_input['bubbles']]
            and t['body']==q[0][3]==audit['actual_body'] and not t['issues']
            and q[0][2]==attempt['output_fingerprint']==details['reply_output_fingerprint']==audit['raw_body_sha256']==digest(t['body'].encode()),
            'original_body_changed')
    require(attempt['id'] in {c['attempt_id'] for c in t['calls'] if c['stage']=='main'},'attempt_scope_changed')
    require(t['jev_need_web'] is False and not t['real_native'] and not t['notice'],'unexpected_knowledge')
    model=json.loads(turn['model_snapshot']) | {'_reply_mode':turn['reply_mode_snapshot'],
                                              '_expression_snapshot':json.loads(turn['expression_snapshot'])}
    require_generation_provider(model)
    require(model['name']=='deepseek-v4-pro' and model['_companion_profile']['version']=='companion-runtime-v8'
            and details['reply_provider']['response_model']==model['name'] and details['reply_provider']['finish_reason']=='stop',
            'original_model_changed')
    character=json.loads(turn['character_snapshot'])
    require(character['id']==audit_binding_character(scope,db) and character['persona_id']==scope['persona_id'],'character_changed')
    prepared=select_context(character,'\n\n'.join(texts),members=texts,scenario=[],example_limit=4)
    return turn,model,prepared,frozen_evidence(db,turn,details),manifest,details,attempt


def audit_binding_character(scope,db):
    result=db.execute('SELECT version_id FROM character_heads WHERE persona_id=?',(scope['persona_id'],)).fetchone()
    require(result,'character_missing')
    return result[0]


def render_turn(db,turn,model,character,evidence,config=None):
    if config:
        ctx=json.loads(turn['context_policy_snapshot'])
        turn=dict(turn,context_policy_snapshot=json.dumps(ctx|{KEY:config}))
    messages,details=build_context(None,db,turn,evidence,character)
    wire,_=build_request(model,messages)
    require(wire['model']=='deepseek-v4-pro' and wire.get('thinking')=={'type':'disabled'}
            and not wire.get('tools') and wire.get('stream') is True,'unexpected_wire')
    return wire,details


def assemble(source,root):
    source,root=outside_repository(source),outside_repository(root)
    require(source!=root,'separate_destination_required')
    paths,scope=binding_at(source)
    materials=verify_sources(source)
    validate_use(source,material_sha256=file_hash(source/'materials.private.json'),require_live=True)
    report=read_private(paths.home/'exports/en5/report.private.json')
    audit=read_private(source/'style-audit/analysis.private.json')
    require(scope['sessions']==report['binding']['sessions'] and audit['evidence_sha256']==report['evidence_sha256']
            and audit['en5_input_contract_sha256']==file_hash(source/'input-contract.private.json'),'wrong_study_evidence')
    prior={row['id']:row for row in audit['rows']}
    require(len(prior)==30,'original_positions')
    span=read_private(root/'proposed-span.private.json')
    require(span['version']=='en5-style-proposed-span-v1' and span['field']==FIELD,'wrong_projector')
    ids={t['turn_id']:f"{s['number']}-{i:02d}" for s in report['segments'] for i,t in enumerate(s['turns'],1)}
    frozen=[]; positions=[]; requests={}; additional=set()
    dbpath=paths.home/'data/database/runtime.sqlite3'
    with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        require(db.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()[0]==scope['persona_id'],
                'selected_persona_changed')
        for seg,script in zip(report['segments'],materials['sessions'],strict=True):
            part=seg['number']
            require(part==script['part'] and len(seg['turns'])==len(script['turns'])==15,'scenario_shape')
            for number,(t,item) in enumerate(zip(seg['turns'],script['turns']),1):
                tid=t['turn_id'];reading=prior[tid]; pos=ids[tid]
                turn,model,ch,ev,manifest,original,attempt=prior_turn(db,scope,t,item,reading)
                require(turn['session_id']==scope['sessions'][part-1],'session_changed')
                wire,details=render_turn(db,turn,model,ch,ev)
                wh=fingerprint(wire)
                require(fingerprint(wire['messages'])==manifest['input_snapshot_hash']==reading['input_fingerprint']
                        and wh==attempt['request_hash'] and details['history_groups']==original['history_groups'],
                        'baseline_drift')
                field=json.loads(turn['character_snapshot'])['data']['social']['context_rules']
                start,end=span['span']
                require(span['character_version']==ch['version_id'] and digest(field.encode())==span['source_sha256']
                        and digest(field[start:end].encode())==span['removed_sha256'],'wrong_field_version')
                record={'position':pos,'turn_id':tid,'persona_id':turn['persona_id'],'session_id':turn['session_id'],
                    'character_version':ch['version_id'],'profile_sha256':fingerprint(json.loads(turn['profile_snapshot'])),
                    'character_snapshot_sha256':fingerprint(json.loads(turn['character_snapshot'])),
                    'policy_sha256':fingerprint(json.loads(turn['context_policy_snapshot'])),
                    'model_public':{k:v for k,v in model.items() if k!='encrypted_key'},
                    'input_sha256':manifest['input_snapshot_hash'],'request_sha256':wh,'attempt_id':attempt['id'],
                    'body_sha256':reading['raw_body_sha256'],'body':t['body'],'history':reading['history'],
                    'reading':reading['reading'],'assessment':reading['assessment'],'wire':wire}
                frozen.append(record)
                # Exercise both factors on every old turn. Only the selected positions can enter the new request set.
                entry={'position':pos,'input':item['text'],'original_body':t['body'],'history':reading['history'],
                       'original_assessment':reading['assessment'],'arms':{}}
                group_ids=[h['turn_id'] for h in details['history_groups']]
                require(len(wire['messages'])==2+2*len(group_ids),'unexpected_history_shape')
                for arm in ARMS:
                    policy={'version':VERSION,'arm':arm,'study_id':STUDY,'persona_id':turn['persona_id'],
                            'sessions':scope['sessions'],'character_version':ch['version_id'],'projection':span}
                    if arm=='B':
                        candidate,meta=wire,{'omitted_history':[],'field_projected':False}
                    else:
                        candidate,candidate_details=render_turn(db,turn,model,ch,ev,policy)
                        meta=candidate_details['style_study']
                    require(meta['field_projected']==(arm in ('P','PH')),'field_not_projected')
                    removed={x['turn_id'] for x in meta['omitted_history']}
                    require(removed<={h['turn_id'] for h in details['history_groups'] if h['kind']=='retrieval'}
                            and not removed.intersection(original['references']['turn_ids']),'unexpected_removal')
                    system=wire['messages'][0]['content']
                    if meta['field_projected']:
                        original_anchor={'kind':'anchor','id':FIELD,'data':field}
                        require(system.count(encode(original_anchor))==1,'ambiguous_field')
                        system=system.replace(encode(original_anchor),encode(original_anchor|{'data':field[:start]+field[end:]}))
                    expected=[{'role':'system','content':system}]
                    for i,gid in enumerate(group_ids):
                        if gid not in removed:expected.extend(wire['messages'][1+2*i:3+2*i])
                    expected.append(wire['messages'][-1])
                    require(candidate==wire|{'messages':expected},'unexpected_candidate_difference')
                    hw=fingerprint(candidate)
                    entry['arms'][arm]={'request_sha256':hw,'projected':meta['field_projected'],
                        'removed':[{'position':ids[x['turn_id']],'term':x['term'],'reason':x['reason']} for x in meta['omitted_history']]}
                    if number in SELECTED[part-1]:
                        require(hw not in requests or requests[hw]==candidate,'request_conflict')
                        requests[hw]=candidate
                        if hw!=wh:additional.add(hw)
                record['history_omissions']=entry['arms']['H']['removed']
                if number in SELECTED[part-1]:positions.append(entry)
    require(len(frozen)==30 and len(positions)==16,'positions_incomplete')
    packet={'version':'en5-style-input-contract-v1','study_id':STUDY,'source':str(source),'root':str(root),
            'source_report_evidence_sha256':report['evidence_sha256'],'source_audit_sha256':file_hash(source/'style-audit/analysis.private.json'),
            'source_approval_sha256':file_hash(source/'author-use.private.json'),'original_baselines_sha256':fingerprint(frozen),
            'projection':span,'original_field':field,'projected_field':field[:start]+field[end:],'positions':positions,
            'requests':requests,'new_requests':sorted(additional),'source_files':implementation(),
            'provider':{'base_url':model['base_url'],'name':model['name']},'web_enabled':False,'production_activation':False}
    return frozen,packet


def pretty(value):
    return escape(json.dumps(value,ensure_ascii=False,indent=2))


def inputs_page(packet):
    p=packet['projection'];start,end=p['span'];field=packet['original_field']
    chunks=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
            '<title>角色 · 接话修复输入审阅</title><style>body{max-width:62rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui;color:#20362d}details,article{border-top:1px solid #c7d3cb;padding:.5rem 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eff4ef;padding:.7rem}del{background:#fce5dc}nav a{margin-right:1em}</style>',
            '<main><h1>角色的接话修复 · 输入审阅</h1>',
            f'<p>30份原版已与最终输入指纹、实发请求指纹和原生正文逐份比对。16个位置有64个方案位置，合计 {len(packet["requests"])} 份不同请求，其中 {len(packet["new_requests"])} 份拟新发给官方 DeepSeek Pro。前文固定为原回合实际呈现的对话。</p>',
            '<h2>人物字段的拟议范围</h2><p>带删除线的区间将在 P 和 PH 中省略。B 和 H 沿用原文。</p><p>',
            escape(field[:start]),'<del>',escape(field[start:end]),'</del>',escape(field[end:]),'</p>',
            '<p>候选的人物字段全文</p><pre>',escape(packet['projected_field']),'</pre>',
            '<p>H 和 PH 只省略未获近期支持的人物侧单一短词匹配的额外历史。每轮明示省略位置。原文、当轮受话对象和其余材料都可在下方逐一核对。</p>',
            '<nav>']
    chunks.extend(f'<a href="#p{p["position"]}">{p["position"]}</a>' for p in packet['positions'])
    chunks.append('</nav>')
    for pos in packet['positions']:
        chunks.extend(['<article id="p'+pos['position']+'"><h2>'+pos['position']+'</h2><p>用户</p><pre>',escape(pos['input']),
                       '</pre><details><summary>原已呈现回复及诊断</summary><pre>',escape(pos['original_body']),'</pre><p>',
                       escape(pos['original_assessment']),'</p></details>'])
        history=pos['history']
        chunks.append('<p>原版前文轮次　'+('、'.join(str(g['turn']) for g in history) or '无')+'</p>')
        old=pos['arms']['B']['request_sha256']
        seen={}
        for arm,data in pos['arms'].items():
            key=data['request_sha256']
            if key in seen:
                chunks.append('<p>'+esc_label(arm)+' 与 '+esc_label(seen[key])+' 完全相同，沿用该位置的同一份回执。</p>')
                continue
            seen[key]=arm
            reason='；'.join(g['position']+' 匹配“'+g['term']+'”' for g in data['removed']) or '无'
            omitted={g['position'] for g in data['removed']}
            part=pos['position'].split('-')[0]
            kept='、'.join(f"{part}-{g['turn']:02d}" for g in history if f"{part}-{g['turn']:02d}" not in omitted) or '无'
            chunks.extend(['<details><summary>',esc_label(arm),' · 完整请求</summary><p>人物区间',
                '省略' if data['projected'] else '保留','，省略的历史　',escape(reason),
                '</p><p>实际保留的前文　',escape(kept),'</p><p>','原回执复用' if key==old else '拟新派发',' · ',key,
                '</p><pre>',pretty(packet['requests'][key]),'</pre></details>'])
        chunks.append('</article>')
    chunks.append('</main></html>')
    return ''.join(chunks)


def esc_label(arm):
    return escape(arm+' '+LABEL[arm])


def prepare(source,root):
    source,root=outside_repository(source),outside_repository(root)
    frozen,packet=assemble(source,root)
    page=inputs_page(packet)
    packet['preview_sha256']=digest(page.encode())
    save_once(root/'original-baselines.private.json',frozen)
    save_once(root/'input-contract.private.json',packet)
    save_text_once(root/'inputs.html',page)
    return packet


if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--source',required=True,type=Path)
    cli.add_argument('--root',required=True,type=Path)
    args=cli.parse_args()
    result=prepare(args.source,args.root)
    print(len(result['positions']),len(result['new_requests']))
