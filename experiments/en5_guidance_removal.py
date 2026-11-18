"""Compare reviewed v8 wires with the exact generic instruction removed."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing, contextmanager
from html import escape
import json
from pathlib import Path
from secrets import SystemRandom

from persona_runtime.archive import fingerprint
from persona_runtime.companion_im import INSTRUCTION
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import validate_reply
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .corpus_reply_trial_report import save_text_once
from .en3_holdout_trial import FirstReceipts
from .en5_style_packet import assemble, file_hash, inputs_page, require


STUDY = 'en5-ling-guidance-removal-v1'
POSITIONS = ('1-03', '1-06', '1-07', '1-11', '1-12', '1-13', '1-15', '2-03', '2-10', '2-15')
HEADER = '\n人物资料：'


def remove_instruction(wire):
    messages = wire['messages']
    system = messages[0]['content']
    require(INSTRUCTION.endswith(HEADER) and messages[0]['role']=='system' and system.startswith(INSTRUCTION),
            'guidance_boundary_changed')
    guidance = INSTRUCTION[:-len(HEADER)]
    revised = system[len(guidance):]
    require(revised.startswith(HEADER) and revised[len(HEADER)]=='{','material_boundary_changed')
    return wire|{'messages':[messages[0]|{'content':revised}, *messages[1:]]},guidance


def build(prior,root):
    prior,root=outside_repository(prior),outside_repository(root)
    require(root!=prior,'separate_study_required')
    pact=read_private(prior/'input-contract.private.json')
    source=outside_repository(pact['source'])
    frozen, rebuilt=assemble(source,prior)
    rebuilt['preview_sha256']=digest(inputs_page(rebuilt).encode())
    require(rebuilt==pact and frozen==read_private(prior/'original-baselines.private.json'),'original_study_changed')
    by_position={r['position']:r for r in frozen}
    cases=[]
    for pos in POSITIONS:
        record=by_position[pos]; original=record['wire']
        changed,removed=remove_instruction(original)
        require(original['messages'][1:]==changed['messages'][1:] and
                original|{'messages':changed['messages']}==changed and
                build_request(record['model_public'],changed['messages'])[0]==changed,
                'unintended_wire_difference')
        cases.append({'position':pos,'B':record,'N':changed,'new_sha256':fingerprint(changed)})
    require(len({x['new_sha256'] for x in cases})==len(POSITIONS),'duplicate_candidate')
    providers={(x['B']['model_public']['name'],x['B']['model_public']['base_url']) for x in cases}
    require(len(providers)==1,'provider_scope')
    name,url=providers.pop()
    return {'version':'en5-guidance-removal-input-v1','study_id':STUDY,'root':str(root),'prior':str(prior),
            'prior_contract_sha256':file_hash(prior/'input-contract.private.json'),
            'source':str(source),'source_report_evidence_sha256':pact['source_report_evidence_sha256'],
            'removed_paragraph':removed,'retained_boundary':HEADER,'provider':{'name':name,'base_url':url},
            'cases':cases,'implementation_sha256':digest(Path(__file__).read_bytes().replace(b'\r\n',b'\n')),
            'fixed_history':True,'web_enabled':False,'formal_activation':False}


def prepare(prior,root):
    root=outside_repository(root)
    manifest=build(prior,root)
    sections=['<!doctype html><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>角色 · 删除通用发言指导核对</title>',
              '<style>body{font:16px/1.6 system-ui;max-width:62rem;margin:2em auto;padding:0 1em}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>',
              '<h1>删除通用发言指导 · 请求核对</h1><p>每个位置从原来的完整请求中省略下面一段，保留随后的人物资料标题和文字。原版正文来自原首次回执。</p><pre>',
              escape(manifest['removed_paragraph']),'</pre>']
    for case in manifest['cases']:
        sections += ['<h2>',case['position'],'</h2><details><summary>原版完整请求</summary><pre>',
                     escape(json.dumps(case['B']['wire'],ensure_ascii=False,indent=2)),'</pre></details>',
                     '<details><summary>去掉通用指导后的完整请求</summary><pre>',
                     escape(json.dumps(case['N'],ensure_ascii=False,indent=2)),'</pre></details>']
    page=''.join(sections)
    manifest['preview_sha256']=digest(page.encode())
    save_once(root/'input-contract.private.json',manifest)
    save_text_once(root/'inputs.html',page)
    return manifest


def use_scope(manifest,sha):
    return {'version':'en5-guidance-removal-use-v1','study_id':STUDY,'approved':True,'input_contract_sha256':sha,
            'preview_sha256':manifest['preview_sha256'],'removed_sha256':digest(manifest['removed_paragraph'].encode()),
            'request_sha256':[c['new_sha256'] for c in manifest['cases']],
            'provider':manifest['provider'],'fixed_history':True,'web_enabled':False,'formal_activation':False}


def first_use(root,manifest,sha):
    f=root/'author-use.private.json'
    require(f.is_file(),'specific_use_approval_required')
    receipt=read_private(f)
    require(all(k in receipt and receipt[k]==v for k,v in use_scope(manifest,sha).items())
            and isinstance(receipt.get('author_message'),str) and receipt['author_message'].strip(),
            'use_scope_changed')
    require(file_hash(root/'input-contract.private.json')==sha and
            file_hash(root/'inputs.html')==manifest['preview_sha256'],'review_changed')
    return file_hash(f)


def freeze_key(root,manifest,sha):
    path=root/'blind-key.private.json'
    if not path.exists():
        require(not any((root/x).exists() for x in ('calls.sqlite3','results.private.json')),'original_key_required')
        mapping={c['position']:dict(zip('甲乙',SystemRandom().sample(['B','N'],2))) for c in manifest['cases']}
        save_once(path,{'version':'en5-guidance-removal-key-v1','input_contract_sha256':sha,'mapping':mapping})
    key=read_private(path)
    require(key['input_contract_sha256']==sha and set(key['mapping'])==set(POSITIONS)
            and all(set(m)==set('甲乙') and set(m.values())=={'B','N'} for m in key['mapping'].values()),'bad_key')
    return file_hash(path)


async def run(root):
    root=outside_repository(root)
    with closing(InstanceLock(root/'run.lock')):
        contract=root/'input-contract.private.json'; m=read_private(contract); sha=file_hash(contract)
        require(m['study_id']==STUDY and outside_repository(m['root'])==root,'wrong_study')
        approval=first_use(root,m,sha)
        key=freeze_key(root,m,sha)
        end=root/'results.private.json'
        if end.is_file():
            r=read_private(end)
            require((r['input_contract_sha256'],r['approval_sha256'],r['blind_key_sha256'])==(sha,approval,key),'result_changed')
            return r['accounting']
        source=outside_repository(m['source']); prior=outside_repository(m['prior'])
        paths=RuntimePaths(source/'home'); store=Store(paths)
        corpus=DatasetCorpusStore(read_private(source/'selection.private.json')['corpus_root'])

        @contextmanager
        def window():
            with corpus.connection(write=True),store.connection(write=True):
                yield

        def guard(_):
            require(first_use(root,m,sha)==approval and file_hash(root/'blind-key.private.json')==key,'approval_withdrawn')
            require(build(prior,root)=={k:v for k,v in m.items() if k!='preview_sha256'},'source_changed')

        def route(_,stage,model):
            require(stage=='body' and provider_identity(model)==provider_identity(m['provider']),'route_changed')
            guard_cloud_dispatch(store,m['cases'][0]['B']['persona_id'],'main',model)

        guard(None)
        credentials=ConfigStore(paths).load()['model']
        require(provider_identity(credentials)==provider_identity(m['provider']),'credential_route_changed')
        journal=FirstReceipts(root,fingerprint([sha,approval,key]),client=GenerationHTTPClient(),guard=guard,
                              authority_window=window,guard_route=route)
        outcomes={}
        for case in m['cases']:
            pos=case['position']; b=case['B']; wire=case['N']
            require(fingerprint(wire)==case['new_sha256'],'request_changed')
            r=await journal.send(pos+'.N.body','body',(b['model_public'],wire),
                [b['request_sha256'],b['input_sha256'],b['body_sha256']],credentials)
            state=r['state']; raw=r.pop('content'); body=None
            if state=='completed':
                try:body=validate_reply(raw,'context_full',b['model_public'].get('_expression_snapshot')).text
                except (ValueError,TypeError):
                    state='failed';r['error_code']='body_validation_failed'
            outcomes[pos]=r|{'state':state,'body':body,'raw_body':raw}
        guard(None)
        with closing(journal.open()) as db:
            physical=dict(db.execute('SELECT state,count(*) FROM requests GROUP BY state'))
        usage=Counter()
        for v in outcomes.values():
            for k,n in (v.get('usage') or {}).items():
                if type(n) is int:usage[k]+=n
        counts={'physical_attempts':sum(physical.values()),'ledger_states':physical,
                'visible_states':dict(Counter(v['state'] for v in outcomes.values())),'reused_original_B':len(POSITIONS),
                'known_usage':dict(usage),'actual_cny':None}
        save_once(end,{'version':'en5-guidance-removal-results-v1','study_id':STUDY,'input_contract_sha256':sha,
                       'approval_sha256':approval,'blind_key_sha256':key,'outcomes':outcomes,'accounting':counts})
        return counts


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',type=Path,required=True)
    print(json.dumps(asyncio.run(run(cli.parse_args().root)),ensure_ascii=False))
