"""Execute approved fixed-history differences and retain at most one first response per wire."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing, contextmanager
import json
from pathlib import Path
from secrets import SystemRandom
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_store import outside_repository
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch, provider_identity
from persona_runtime.reply_batch import validate_reply
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once
from .en3_holdout_trial import FirstReceipts
from .en5_style_packet import STUDY, assemble, file_hash, inputs_page, require
from persona_runtime.corpus_files import digest


def scope(contract, checksum):
    return {'version':'en5-style-use-v1','study_id':STUDY,'approved':True,'contract_sha256':checksum,
            'preview_sha256':contract['preview_sha256'],'projection':contract['projection'],
            'baseline_set':contract['original_baselines_sha256'],'requests':contract['new_requests'],
            'provider':contract['provider'],'fixed_history':True,'web_enabled':False,'formal_activation':False}


def authorized(root, contract, checksum):
    raw=root/'author-use.private.json'
    require(raw.is_file(),'specific_input_approval_required')
    value=read_private(raw)
    require(all(key in value and fingerprint(value[key])==fingerprint(v)
                for key,v in scope(contract,checksum).items())
            and isinstance(value.get('author_message'),str) and value['author_message'].strip(),'approval_scope_changed')
    require(file_hash(root/'input-contract.private.json')==checksum and file_hash(root/'inputs.html')==contract['preview_sha256'],
            'review_changed')
    return file_hash(raw)


def blind_key(root,contract,checksum):
    path=root/'blind-key.private.json'
    if not path.exists():
        require(not (root/'calls.sqlite3').exists() and not (root/'results.private.json').exists(),
                'original_key_required')
        rng=SystemRandom()
        order={}
        for p in contract['positions']:
            wires=list(dict.fromkeys(v['request_sha256'] for v in p['arms'].values()))
            rng.shuffle(wires)
            order[p['position']]=dict(zip('甲乙丙丁',wires))
        save_once(path,{'version':'en5-style-position-key-v1','input_contract_sha256':checksum,'labels':order})
    key=read_private(path)
    require(key['input_contract_sha256']==checksum and set(key['labels'])=={p['position'] for p in contract['positions']},
            'key_scope_changed')
    for p in contract['positions']:
        wires=set(v['request_sha256'] for v in p['arms'].values())
        labels=key['labels'][p['position']]
        require(set(labels.values())==wires and set(labels)==set('甲乙丙丁'[:len(wires)]),'key_positions_changed')
    return file_hash(path)


def order(contract):
    seen=set()
    scheduled=[]
    for i,pos in enumerate(contract['positions']):
        arms=['P','H','PH']
        for arm in arms[i%3:]+arms[:i%3]:
            key=pos['arms'][arm]['request_sha256']
            if key not in seen and key in contract['new_requests']:
                seen.add(key)
                scheduled.append((pos['position'],key))
    require(seen==set(contract['new_requests']),'unregistered_request')
    return scheduled


async def run(root):
    root=outside_repository(root)
    with closing(InstanceLock(root/'run.lock')):
        contract=read_private(root/'input-contract.private.json')
        sha=file_hash(root/'input-contract.private.json')
        require(contract['study_id']==STUDY and outside_repository(contract['root'])==root,
                'wrong_study')
        approval=authorized(root,contract,sha)
        source=outside_repository(contract['source'])
        frozen, rebuilt=assemble(source,root)
        rebuilt['preview_sha256']=digest(inputs_page(rebuilt).encode())
        require(rebuilt==contract and frozen==read_private(root/'original-baselines.private.json'),'assembly_changed')
        key_sha=blind_key(root,contract,sha)
        terminal=root/'results.private.json'
        if terminal.is_file():
            old=read_private(terminal)
            require((old['input_contract_sha256'],old['approval_sha256'],old['blind_key_sha256'])==(sha,approval,key_sha),
                    'results_binding_changed')
            return old['accounting']
        plan=order(contract)
        base={r['position']:r for r in frozen}
        paths=RuntimePaths(source/'home')
        source_store=Store(paths)
        corpus=DatasetCorpusStore(read_private(source/'selection.private.json')['corpus_root'])

        @contextmanager
        def window():
            # Lock source withdrawal only while authorizing the local first dispatch or checking the returned receipt.
            with corpus.connection(write=True),source_store.connection(write=True):
                yield

        def guard(slot):
            require(authorized(root,contract,sha)==approval and file_hash(root/'blind-key.private.json')==key_sha,
                    'approval_withdrawn')
            # Verify the original data grant and the live archive with the same renderer as the input review.
            current, now=assemble(source,root)
            require(now=={k:v for k,v in contract.items() if k!='preview_sha256'} and current==frozen,'source_changed')
        def route(slot,stage,model):
            require(stage=='body' and (model['base_url'],model['name'])==(contract['provider']['base_url'],
                    contract['provider']['name']),'route_changed')
            guard_cloud_dispatch(source_store, frozen[0]['persona_id'],'main',model)
        credentials=ConfigStore(paths).load()['model']
        require(provider_identity(credentials)==provider_identity(contract['provider']),'credential_route_changed')
        journal=FirstReceipts(root,fingerprint([sha,approval,key_sha]),client=GenerationHTTPClient(),
                              guard=guard,authority_window=window,guard_route=route)
        outcomes={}
        for pos in contract['positions']:
            b=base[pos['position']]
            wh=b['request_sha256']
            outcomes[wh]={'state':'completed','body':b['body'],'raw_sha256':b['body_sha256'],'origin':'original_B',
                          'attempt_id':b['attempt_id'],'request_hash':wh,'usage':None,'actual_charge':None}
        for pos,wh in plan:
            b=base[pos]
            wire=contract['requests'][wh]
            require(fingerprint(wire)==wh and wh not in outcomes,'duplicate_or_changed_request')
            result=await journal.send(pos+'.'+wh,'body',(b['model_public'],wire),
                [b['input_sha256'],b['request_sha256'],b['body_sha256']],credentials)
            state=result['state'];raw=result.pop('content')
            body=None
            if state=='completed':
                try:body=validate_reply(raw,'context_full',b['model_public'].get('_expression_snapshot')).text
                except (ValueError,TypeError):
                    state='failed';result['error_code']='body_validation_failed'
            outcomes[wh]=result|{'origin':'new','state':state,'body':body,'raw_body':raw}
        guard(None)
        require(set(outcomes)==set(contract['requests']),'result_set_incomplete')
        with closing(journal.open()) as db:
            states=dict(db.execute('SELECT state,count(*) FROM requests GROUP BY state').fetchall())
        fresh=[o for o in outcomes.values() if o['origin']=='new' and o.get('attempt_id')]
        usage=[o.get('usage') or {} for o in fresh]
        accounting={'new_physical_requests':sum(states.values()),'http_ledger_states':states,'reused_originals':len(outcomes)-len(plan),
                    'new_position_states':dict(Counter(o['state'] for o in outcomes.values() if o['origin']=='new')),
                    'known_tokens':sum(u['total_tokens'] for u in usage if type(u.get('total_tokens')) is int),
                    'unknown_usage':sum(type(u.get('total_tokens')) is not int for u in usage),'actual_charge':None}
        save_once(terminal,{'version':'en5-style-first-results-v1','study_id':STUDY,'input_contract_sha256':sha,
                            'approval_sha256':approval,'blind_key_sha256':key_sha,'outcomes':outcomes,'accounting':accounting})
        return accounting


if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--root',type=Path,required=True)
    print(json.dumps(asyncio.run(run(cli.parse_args().root)),ensure_ascii=False))
