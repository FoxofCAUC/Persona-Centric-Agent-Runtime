"""Export fixed-history answers without showing identities on the voting page."""
import argparse
from html import escape
import json
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_store import outside_repository
from .corpus_reply_study import read_private
from .corpus_reply_trial_report import save_text_once
from .en5_style_packet import LABEL, STUDY, file_hash, require


def bound(root):
    root=outside_repository(root)
    contract=read_private(root/'input-contract.private.json')
    results=read_private(root/'results.private.json')
    key=read_private(root/'blind-key.private.json')
    sha=file_hash(root/'input-contract.private.json')
    require(contract['study_id']==results['study_id']==STUDY and results['version']=='en5-style-first-results-v1'
            and results['input_contract_sha256']==key['input_contract_sha256']==sha
            and results['blind_key_sha256']==file_hash(root/'blind-key.private.json')
            and results['approval_sha256']==file_hash(root/'author-use.private.json'),'unbound_results')
    baselines=read_private(root/'original-baselines.private.json')
    require(fingerprint(baselines)==contract['original_baselines_sha256'],'baseline_file_changed')
    previous={r['position']:r for r in baselines}
    require(set(results['outcomes'])==set(contract['requests']),'missing_result')
    cases=[];evidence=[]
    for p in contract['positions']:
        position=p['position']
        assigned=key['labels'][position]
        candidates={v['request_sha256'] for v in p['arms'].values()}
        require(set(assigned)==set('甲乙丙丁'[:len(candidates)]) and set(assigned.values())==candidates,'bad_mapping')
        before=[]
        for row in p['history']:
            src=previous[f"{position.split('-')[0]}-{row['turn']:02d}"]
            before.append({'turn':row['turn'],'user':src['wire']['messages'][-1]['content'],'reply':src['body']})
        options={}
        for label,wh in assigned.items():
            result=results['outcomes'][wh]
            require(result['request_hash']==wh and fingerprint(contract['requests'][wh])==wh,'result_wire_changed')
            if result['state']=='completed':
                require(isinstance(result['body'],str) and result['body'].strip(),'empty_success')
            else:require(result['body'] is None,'unverified_text')
            options[label]={'state':result['state'],'body':result['body']}
            variants={a:v for a,v in p['arms'].items() if v['request_sha256']==wh}
            evidence.append({'position':position,'label':label,'arms':variants,
                             'receipt':{k:v for k,v in result.items() if k not in ('raw_body','body')}})
        cases.append({'position':position,'user':p['input'],'before':before,'options':options})
    view={'study_id':STUDY,'input_contract_sha256':sha,'results_sha256':file_hash(root/'results.private.json'),
          'blind_key_sha256':results['blind_key_sha256'],'cases':cases}
    return view,evidence,results['accounting']


def export(root):
    root=outside_repository(root)
    view,evidence,counts=bound(root)
    target=root/'review/index.html'
    page=Path(__file__).with_suffix('.html').read_text(encoding='utf8')
    payload=json.dumps(view,ensure_ascii=False).replace('<',r'\u003c')
    save_text_once(target,page.replace('@@DATA@@',payload))
    notes=['<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>接话修复 · 技术记录</title>',
           '<style>body{max-width:56rem;margin:2em auto;padding:0 1rem;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
           '<main><h1>身份、复用及实际用量</h1><pre>',escape(json.dumps(counts,ensure_ascii=False,indent=2)),'</pre>']
    for q in evidence:
        title=q['position']+' · '+q['label']+' · '+' / '.join(LABEL[a] for a in q['arms'])
        notes.extend(['<details><summary>',escape(title),'</summary><pre>',escape(json.dumps(q,ensure_ascii=False,indent=2)),'</pre></details>'])
    save_text_once(target.with_name('evidence.html'),''.join(notes)+'</main>')
    plain=['角色 · 16个固定位置接话方式盲审','每轮单独匿名，完全相同的请求只列一次。']
    for q in view['cases']:
        plain.extend(['',q['position'],'用户　'+q['user']])
        for label,o in q['options'].items():
            plain.extend([label,o['body'] if o['state']=='completed' else '本位置缺口　'+o['state']])
    save_text_once(target.with_name('匿名正文全文.txt'),'\n'.join(plain)+'\n')
    return target


if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--root',type=Path,required=True)
    print(export(cli.parse_args().root))
