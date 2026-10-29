"""Frozen development-only local calibration. No production calls or deployment."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.character import CharacterService
from persona_runtime.character_context import select_context
from persona_runtime.character_embedding import CharacterEmbeddingProvider, digest, safe_path, file_hash
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_retrieval import ALGORITHM, rank_candidates, assemble
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def run(evidence, model_directory):
    root=Path(__file__).resolve().parents[1]
    body=(root/'tests/fixtures/m752_development.json').read_bytes()
    corpus=json.loads(body);corpus_hash=hashlib.sha256(body).hexdigest()
    freeze=json.loads((evidence/'development-freeze.json').read_text())
    assert freeze['sha256']==corpus_hash
    data=corpus['character'];cases=corpus['cases']
    paths=RuntimePaths.from_environment()
    provider=CharacterEmbeddingProvider(paths,model_directory)
    model=provider.identity()['fingerprint']
    store=Store(RuntimePaths(evidence/'sandbox'))
    saved=evidence/'raw-scores.json'
    if saved.exists():
        raw=json.loads(saved.read_text(encoding='utf-8'))
        assert raw['corpus_hash']==corpus_hash and raw['model_fingerprint']==model
        version=CharacterService(store).show(raw['persona_id'],raw['version_id'])
        assert version['data']==data
    else:
        store.initialize();service=RuntimeService(store)
        pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
        chars=CharacterService(store);version=chars.update(pid,data,chars.current(pid)['id'],identifier())
        index=CharacterSemanticIndex(store,provider)
        with patch('socket.socket.connect',side_effect=AssertionError('no network')) as connect, \
             patch('socket.getaddrinfo',side_effect=AssertionError('no DNS')) as dns:
            index.build(pid,version['id'])
            before=file_hash(store.path)
            results={};timings={}
            for case in cases:
                started=time.perf_counter()
                results[case['id']]=index.query(pid,version['id'],case['members'],per_kind_limit=10)
                timings[case['id']]=time.perf_counter()-started
            assert file_hash(store.path)==before and connect.call_count==dns.call_count==0
        raw={'corpus_hash':corpus_hash,'model_fingerprint':model,'persona_id':pid,
             'version_id':version['id'],'results':results,'query_seconds':timings,'network_attempts':0}
        saved.write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    base={'algorithm':ALGORITHM,'rrf_k':60,'model_fingerprint':model,
          'development_sha256':corpus_hash,'thresholds':{'lore':.9,'example':.9},
          'status':'development_frozen_not_joint_acceptance'}
    grids={};chosen={}
    for kind,expected_key,selected_key in (('lore','expected_lore','lore_ids'),('example','expected_examples','example_ids')):
        trials=[]
        for step in range(16,37):
            threshold=step/40
            config=base | {'thresholds':base['thresholds'] | {kind:threshold}}
            negatives=hits=total=0
            for case in cases:
                ranked,_=rank_candidates(version,case['members'],raw['results'][case['id']]['by_kind'],config)
                selection=assemble(version,case['members'],ranked,2)
                actual=set(selection[selected_key]);expected=set(case[expected_key])
                if case['negative'] and actual:negatives+=1
                if not case['negative']:
                    hits+=len(actual & expected);total+=len(expected)
            trials.append({'threshold':threshold,'negative_activations':negatives,'hits':hits,'expected':total})
        feasible=[r for r in trials if r['negative_activations']==0]
        grids[kind]=trials
        if not feasible:
            (evidence/'calibration-blocked.json').write_text(json.dumps(grids,indent=2),encoding='utf-8')
            raise RuntimeError('No threshold meets frozen negative contract; preserve corpus and repair retrieval')
        chosen[kind]=max(feasible,key=lambda r:(r['hits'],r['threshold']))['threshold']
    config=base | {'thresholds':chosen}
    report=[];semantic_gains=[]
    for case in cases:
        ranked,rejected=rank_candidates(version,case['members'],raw['results'][case['id']]['by_kind'],config)
        selected=assemble(version,case['members'],ranked,2)
        lexical=select_context(version,'\n\n'.join(case['members']),members=case['members'])
        actual=set(selected['lore_ids']+selected['example_ids'])
        expected=set(case['expected_lore']+case['expected_examples'])
        legacy=set(lexical['lore_ids']+lexical['example_ids'])
        gain=sorted((expected & actual)-legacy)
        if gain:semantic_gains.append({'id':case['id'],'items':gain})
        passed=(not actual if case['negative'] else expected.issubset(actual)) and set(case['fact_paths']).issubset(selected['fact_paths'])
        report.append({'id':case['id'],'category':case['category'],'negative':case['negative'],
            'expected':sorted(expected),'selected':sorted(actual),'lexical_selected':sorted(legacy),
            'passed':passed,'rejected':rejected,'removed':selected['removed'],'budgets':selected['budgets'],
            'rankings':ranked})
    assert semantic_gains, 'Reject-all semantic configuration is not a completed implementation'
    assert not any(r['selected'] for r in report if r['negative'])
    output={'kind':'development_only','config':config,'config_fingerprint':digest(config),'grid':grids,
            'cases':report,'semantic_gains':semantic_gains,'passed':sum(r['passed'] for r in report),
            'total':len(report),'known_misses':[r['id'] for r in report if not r['passed']],
            'paid_calls':0,'qq_sends':0,'locked_set_used':False}
    (evidence/'development-report.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (root/'persona_runtime/character_retrieval_config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'thresholds':chosen,'passed':output['passed'],'total':32,'semantic_gains':semantic_gains,'known_misses':output['known_misses']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--model-directory',type=Path,required=True)
    args=parser.parse_args();safe_path(RuntimePaths.from_environment().home,args.evidence)
    run(args.evidence,args.model_directory)
