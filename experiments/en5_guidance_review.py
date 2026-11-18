"""Publish the two anonymous arms and preserve the matching author ballot."""
import argparse
from contextlib import closing
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_receipt import parse_receipt
from persona_runtime.reply_batch import validate_reply
from .corpus_reply_study import read_private,save_once
from .corpus_reply_trial_report import save_text_once
from .en3_repair_feedback import unpack,save_raw
from .en5_guidance_removal import STUDY,first_use,remove_instruction
from .en5_style_feedback import decode
from .en5_style_packet import file_hash,require


NAMES={'B':'原 v8 Pro','N':'删除通用指导'}


def bound(root):
    root=outside_repository(root)
    mf=root/'input-contract.private.json'; rf=root/'results.private.json'; kf=root/'blind-key.private.json'
    m,r,k=map(read_private,(mf,rf,kf))
    sha=file_hash(mf)
    require(m['study_id']==r['study_id']==STUDY and r['version']=='en5-guidance-removal-results-v1'
            and r['input_contract_sha256']==k['input_contract_sha256']==sha
            and r['blind_key_sha256']==file_hash(kf) and r['approval_sha256']==first_use(root,m,sha),'unbound_results')
    cases=[]; evidence=[]; audit=[]
    with closing(sqlite3.connect((root/'calls.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        require(set(r['outcomes'])==set(k['mapping'])=={c['position'] for c in m['cases']},'unexpected_results')
        for c in m['cases']:
            pos=c['position']; b=c['B']; n=r['outcomes'][pos]
            require(remove_instruction(b['wire'])==(c['N'],m['removed_paragraph'])
                    and fingerprint(c['N'])==c['new_sha256']==n['request_hash']
                    and fingerprint(b['wire'])==b['request_sha256'] and digest(b['body'].encode())==b['body_sha256'],
                    'wire_or_original_body_changed')
            if n.get('attempt_id'):
                old=db.execute("""SELECT r.wire, r.raw, r.raw_hash, r.http, r.state, r.attempt_id
                                  FROM uses u JOIN requests r USING(hash) WHERE u.slot=?""",(pos+'.N.body',)).fetchone()
                require(old and json.loads(old[0])==c['N'] and old[5]==n['attempt_id'],'first_receipt_missing')
                if old[4]=='completed':
                    p=parse_receipt(old[1],c['N']['stream'])
                    require(old[3]==200 and digest(old[1])==old[2]==n['raw_hash'] and p['valid']
                            and p['model']==m['provider']['name'] and p['body']==n['raw_body'] and p['usage']==n['usage'],
                            'raw_body_changed')
                    try:
                        visible=validate_reply(p['body'],'context_full',b['model_public'].get('_expression_snapshot')).text
                    except (TypeError,ValueError):
                        require(n['state']=='failed' and n['error_code']=='body_validation_failed',
                                'body_validation_changed')
                    else:
                        require(n['state']=='completed' and visible==n['body'],'displayed_body_changed')
                    audit.append({'position':pos,'request_sha256':c['new_sha256'],'raw_sha256':old[2],'http_status':old[3]})
            mapping=k['mapping'][pos]
            require(set(mapping)==set('甲乙') and set(mapping.values())==set(NAMES),'key_changed')
            msgs=b['wire']['messages']; prior=msgs[1:-1]
            require(len(prior)==len(b['history'])*2,'unexpected_history_shape')
            before=[]
            for idx,h in enumerate(b['history']):
                user,reply=prior[2*idx:2*idx+2]
                require((user['role'],reply['role'])==('user','assistant'),'history_roles_changed')
                before.append({'turn':h['turn'],'user':user['content'],'reply':reply['content']})
            require((isinstance(n['body'],str) and n['body'].strip()) if n['state']=='completed' else n['body'] is None,
                    'invalid_body_state')
            choices={'B':{'state':'completed','body':b['body']},'N':{'state':n['state'],'body':n['body']}}
            cases.append({'position':pos,'user':msgs[-1]['content'],'before':before,
                          'options':{label:choices[arm] for label,arm in mapping.items()}})
            evidence.append({'position':pos,'mapping':mapping,'old_wire_sha256':b['request_sha256'],
                             'new_receipt':{x:v for x,v in n.items() if x not in ('raw_body','body')}})
    view={'study_id':STUDY,'input_contract_sha256':sha,'results_sha256':file_hash(rf),
          'blind_key_sha256':file_hash(kf),'cases':cases}
    return view,m,k,evidence,audit,r['accounting']


def export(root):
    root=outside_repository(root)
    view,_,_,evidence,audit,counts=bound(root)
    page=root/'review/index.html'
    template=Path(__file__).with_name('en5_style_review.html').read_text(encoding='utf8')
    template=template.replace('接话修复16位审阅意见','通用指导删除对照审阅意见')
    save_text_once(page,template.replace('@@DATA@@',json.dumps(view,ensure_ascii=False).replace('<',r'\u003c')))
    tech=['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>通用指导删除 · 技术记录</title>',
          '<style>body{max-width:57rem;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
          '<h1>身份、回执与用量</h1><p>技术页包含解盲身份。阅读全文时可先使用匿名页。</p>',
          '<p><a href="../inputs.html">查看删去的整段及每轮完整请求</a></p><pre>',escape(json.dumps(counts,ensure_ascii=False,indent=2)),'</pre>']
    for e in evidence:
        title=e['position']+' · '+' / '.join(label+' '+NAMES[arm] for label,arm in e['mapping'].items())
        tech.extend(['<details><summary>',escape(title),'</summary><pre>',
                     escape(json.dumps(e,ensure_ascii=False,indent=2)),'</pre></details>'])
    save_text_once(page.with_name('evidence.html'),''.join(tech))
    text=['角色 · 通用指导删除对照','每轮单独匿名，人物资料与前文相同。']
    for c in view['cases']:
        text.extend(['',c['position'],'用户　'+c['user']])
        for label,o in c['options'].items():text.extend([label,o['body'] or '本位置缺口　'+o['state']])
    save_text_once(page.with_name('匿名正文全文.txt'),'\n'.join(text)+'\n')
    save_once(root/'receipt-audit.private.json',{'study_id':STUDY,'results_sha256':view['results_sha256'],
        'input_contract_sha256':view['input_contract_sha256'],'verified_new_receipts':audit,
        'reused_originals':len(view['cases']),'anonymous_page_sha256':file_hash(page),'accounting':counts})
    return page


def feedback(root,path):
    root=outside_repository(root)
    view,m,k,_,_,_=bound(root)
    by_position={c['position']:{'B':c['B']['request_sha256'],'N':c['new_sha256']} for c in m['cases']}
    label_to_wire={pos:{label:by_position[pos][arm] for label,arm in values.items()}
                   for pos,values in k['mapping'].items()}
    contract={'positions':[{'position':pos,'arms':{a:{'request_sha256':w} for a,w in values.items()}}
                            for pos,values in by_position.items()]}
    raw=outside_repository(path).read_bytes()
    decoded=decode(unpack(raw),view,{'labels':label_to_wire},contract)
    sha=digest(raw); folder=root/'author-feedback'/sha
    folder.mkdir(parents=True,exist_ok=True)
    save_raw(folder/'submitted.private.json',raw)
    save_once(folder/'decoded.private.json',decoded|{'submitted_sha256':sha})
    return folder


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    cli.add_argument('--feedback',type=Path)
    a=cli.parse_args()
    print(feedback(a.root,a.feedback) if a.feedback else export(a.root))
