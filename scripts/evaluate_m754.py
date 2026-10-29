"""Locked acceptance A/B. Never calibrates parameters or changes formal data."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.character import CharacterService,read_fact
from persona_runtime.character_context import select_context
from persona_runtime.character_embedding import CharacterEmbeddingProvider,file_hash
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_retrieval import CharacterCandidateRetriever,load_config
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService


def run(evidence):
    root=Path(__file__).resolve().parents[1];fixture=root/'tests/fixtures/m754_locked.json'
    frozen=json.loads((evidence/'locked-freeze.json').read_text())
    assert file_hash(fixture)==frozen['sha256']
    assert file_hash(root/'persona_runtime/character_retrieval_config.json')==frozen['config_sha256']
    corpus=json.loads(fixture.read_text(encoding='utf-8'));cfg=load_config()
    sandbox=evidence/'quality-runtime'
    if sandbox.exists():raise RuntimeError('Existing evidence: inspect checkpoint, do not silently rerun')
    store=Store(RuntimePaths(sandbox));store.initialize();service=RuntimeService(store)
    pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    chars=CharacterService(store);v=chars.update(pid,corpus['character'],chars.current(pid)['id'],identifier())
    other=service.create_persona(generic_persona())['id']
    changed=deepcopy(corpus['character']);changed['appearance']['height_cm']=199
    chars.update(pid,changed,v['id'],identifier())
    wide=deepcopy(changed);next(r for r in wide['lorebook'] if r['id']=='root')['text']='预算边界完整正文。'*180
    large=chars.update(pid,wide,chars.current(pid)['id'],identifier())
    home=RuntimePaths.from_environment()
    provider=CharacterEmbeddingProvider(home,home.home/'models/character-embedding'/cfg['model_fingerprint'])
    index=CharacterSemanticIndex(store,provider);retriever=CharacterCandidateRetriever(store,index,cfg)
    rows=[]
    with patch('socket.socket.connect',side_effect=AssertionError('offline')),patch('socket.getaddrinfo',side_effect=AssertionError('offline')):
        index.build(pid,v['id']);index.build(pid,large['id'])
        for case in corpus['cases']:
            version=large if case['contract']=='dependency_budget' else v
            if case['contract']=='foreign_version':
                try:retriever.retrieve(other,v['id'],case['members'])
                except RuntimeErrorBase as exc:
                    rows.append({'id':case['id'],'group':'contract','passed':str(exc)=='record_not_in_selected_persona','error':str(exc)})
                else:rows.append({'id':case['id'],'group':'contract','passed':False})
                continue
            legacy=select_context(version,'\n\n'.join(case['members']),members=case['members'])
            value=retriever.retrieve(pid,version['id'],case['members'])
            chosen=set(value['lore_ids']+value['example_ids']);expected=set(case['expected_ids'])
            forbidden=chosen & set(case['forbidden_ids'])
            baseline=legacy['lore_ids']+legacy['example_ids']
            ranking=[r['item_id'] for r in value['candidates'].get(case['source_kind'],[])][:5]
            hits=len(expected & set(ranking))/len(expected) if expected else None
            oldhits=len(expected & set(baseline[:5]))/len(expected) if expected else None
            rr=next((1/(n+1) for n,item in enumerate(ranking) if item in expected),0) if expected else None
            oldrr=next((1/(n+1) for n,item in enumerate(baseline[:5]) if item in expected),0) if expected else None
            facts=[read_fact(version,p) for p in case['relevant_fact_paths']]
            fact_ok=set(case['relevant_fact_paths'])<=set(value['fact_paths'])
            if case['contract'] in ('known_fact','old_version'):fact_ok &= facts[0]['value']==171
            if case['contract']=='unknown_fact':fact_ok &= facts[0]['status']=='unset'
            passed=expected<=chosen and not forbidden and fact_ok and len(value['text'])<=3000 and value['degrade_reason'] is None
            row={'id':case['id'],'group':case['group'],'kind':case['source_kind'],'expected':sorted(expected),
                'selected':sorted(chosen),'lexical_selected':baseline,'forbidden_selected':sorted(forbidden),
                'recall5':hits,'lexical_recall5':oldhits,'mrr5':rr,'lexical_mrr5':oldrr,'passed':passed,
                'rejected':value['rejected'],'rankings':value['candidates'],'facts':facts,'budgets':value['budgets']}
            rows.append(row)
            (evidence/'quality-partial.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    positive=[r for r in rows if r['group']=='positive'];negative=[r for r in rows if r['group']=='negative'];contracts=[r for r in rows if r['group']=='contract']
    mean=lambda field:sum(r[field] for r in positive)/len(positive)
    metrics={'recall5':mean('recall5'),'lexical_recall5':mean('lexical_recall5'),'gain':mean('recall5')-mean('lexical_recall5'),
        'mrr5':mean('mrr5'),'lexical_mrr5':mean('lexical_mrr5'),'correct_positive':sum(r['passed'] for r in positive),
        'negative_activations':sum(bool(r['selected']) for r in negative),'contracts_passed':sum(r['passed'] for r in contracts)}
    per_kind={kind:{f:sum(r[f] for r in positive if r['kind']==kind)/sum(r['kind']==kind for r in positive) for f in ('recall5','lexical_recall5','mrr5','lexical_mrr5')} for kind in ('lore','example')}
    hard=all(r['passed'] for r in contracts) and not any(r['forbidden_selected'] for r in positive)
    gates={'recall':metrics['recall5']>=.85,'gain':metrics['gain']>=.10,'activation':metrics['correct_positive']>=17,
        'negative':metrics['negative_activations']<=1,'contracts_and_positive_identity':hard}
    report={'dataset_sha256':frozen['sha256'],'config':cfg,'model_fingerprint':provider.identity()['fingerprint'],
        'metrics':metrics,'per_kind':per_kind,'gates':gates,'passed':all(gates.values()),'cases':rows,
        'paid_calls':0,'qq_sends':0,'formal_changes':False,'local_query_count':47,'thresholds_changed':False}
    (evidence/'quality-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    checkpoint=json.loads((evidence/'checkpoint.json').read_text());checkpoint['P01']='passed' if report['passed'] else 'blocked_quality_gate'
    (evidence/'checkpoint.json').write_text(json.dumps(checkpoint,indent=2),encoding='utf-8')
    print(json.dumps({'metrics':metrics,'per_kind':per_kind,'gates':gates,'passed':report['passed']},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
    run(parser.parse_args().evidence)
