"""Offline artificial-data evaluation; never opens the configured user database."""
import argparse
import json
from pathlib import Path
import statistics
import tempfile
import time
import sqlite3
import threading
import tracemalloc

from persona_runtime.archive import ArchiveService
from persona_runtime.archive_search import ArchiveSearchService
from persona_runtime.archive_query import Budget, normal, lexical, encoded_terms, matches, validate
from persona_runtime.archive_corpus import capture
from persona_runtime.archive_index import ArchiveIndex
from persona_runtime.chat import terms as rule_terms
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.models import generic_persona
from persona_runtime.service import RuntimeService
from tests.m71_fixtures import seed_fixture, seed_turn


def evaluate(store,pid,data):
    api=ArchiveSearchService(store);results=[]
    for query in data['queries']:
        if query.get('path')=='exact':
            response=ArchiveService(store).get(persona_id=pid,channel='desktop',target=query['target'])
            ids=[response['turn_id']] if response['status']=='found' else []
            passed=response['status']=='found' and set(ids)==set(query['expected_group_ids'])
        else:
            spec={k:query[k] for k in ('query','time_axis','window','local_date') if k in query}
            response=api.search(persona_id=pid,channel='desktop',**spec)
            ids=[g['turn_id'] for g in response.get('groups',[])]
            members={m['id'] for g in response.get('groups',[]) for m in g['members']}
            targets={t['id'] for g in response.get('groups',[]) for t in g['matched_targets']}
            passed=(response['status']==query['expected_status'] and set(ids)==set(query['expected_group_ids'])
                    and set(query['required_member_ids'])<=members and set(query['matched_target_ids'])<=targets
                    and not (set(query['forbidden_ids'])&set(ids)))
        expected=set(query['expected_group_ids']);found=set(ids)
        reciprocal=next((1/rank for rank,key in enumerate(ids[:5],1) if key in expected),0)
        results.append({'id':query['id'],'passed':passed,'status':response['status'],'groups':ids,
                        'recall_at_5':len(expected&found)/len(expected) if expected else None,
                        'hit_at_5':int(bool(expected&set(ids[:5]))) if expected else None,
                        'reciprocal_rank_at_5':reciprocal if expected else None})
    chinese=[r['recall_at_5'] for r in results if r['id'].startswith('C') and r['recall_at_5'] is not None]
    return {'passed':sum(r['passed'] for r in results),'total':len(results),
            'chinese_macro_recall_at_5':statistics.mean(chinese),
            'chinese_hit_at_5':statistics.mean(r['hit_at_5'] for r in results if r['id'].startswith('C') and r['hit_at_5'] is not None),
            'chinese_mrr_at_5':statistics.mean(r['reciprocal_rank_at_5'] for r in results if r['id'].startswith('C') and r['reciprocal_rank_at_5'] is not None),
            'results':results}


def candidates_comparison():
    # Development queries only. The accepted set is evaluated separately above.
    data=json.loads((Path(__file__).parents[1]/'tests/fixtures/m71/corpus.json').read_text(encoding='utf-8'))
    docs=[(t['id'],text) for t in data['turns'] if not t.get('excluded') for text in t['members']]
    report={}
    for method in ('substring','rules','bigram','trigram'):
        begin=time.perf_counter();output={}
        with sqlite3.connect(':memory:') as db:
            if method in ('bigram','trigram'):
                db.execute('CREATE VIRTUAL TABLE texts USING fts5(tokens)')
                for i,(_,text) in enumerate(docs):
                    terms=lexical(text,'trigram' if method=='trigram' else 'bigram',document=True)[0]
                    db.execute('INSERT INTO texts(rowid,tokens) VALUES(?,?)',(i+1,' '.join(encoded_terms(terms))))
            for case in data['development_queries']:
                query=case['query'];terms,literal=lexical(query,'trigram' if method=='trigram' else 'bigram')
                if method in ('substring','rules') or literal:
                    selected=range(len(docs))
                else:
                    selected=[r[0]-1 for r in db.execute('SELECT rowid FROM texts WHERE texts MATCH ?',(' AND '.join('"'+t+'"' for t in encoded_terms(terms)),))]
                output[case['id']]=sorted({docs[i][0] for i in selected if
                    (bool(rule_terms(normal(docs[i][1])) & rule_terms(normal(query))) if method=='rules'
                     else matches(docs[i][1],query,strategy='substring' if method=='substring' else 'trigram' if method=='trigram' else 'bigram'))})
        report[method]={'build_and_query_ms':round((time.perf_counter()-begin)*1000,3),'matches':output}
    return report


def rule_baseline(data):
    docs=[(t['id'],text) for t in data['turns'] if not t.get('excluded') for text in t['members']]
    scores=[]
    for case in data['queries']:
        if not case['id'].startswith('C') or not case['answerable']:continue
        query=normal(case['query']);qt=rule_terms(query);groups={}
        for key,text in docs:
            overlap=len(qt&rule_terms(normal(text)))
            if overlap:groups[key]=max(groups.get(key,0),overlap)
        found=set(sorted(groups,key=lambda k:(-groups[k],k))[:5]);expected=set(case['expected_group_ids'])
        scores.append(len(found&expected)/len(expected))
    return {'kind':'same-corpus terms-overlap projection; not memory.search benchmark',
            'chinese_macro_recall_at_5':statistics.mean(scores),'queries':len(scores)}


def performance(base,size):
    paths=RuntimePaths(base/f'size-{size}');store=Store(paths);store.initialize()
    service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid);sid=service.create_session()['id']
    with store.connection(write=True) as db:
        for i in range(size):seed_turn(db,pid,sid,f'perf-{i:05d}',[f'人工档案编号 item_{i:05d}，主题书店与工程检索，保留原始来源'])
    api=ArchiveSearchService(store);args={'persona_id':pid,'channel':'desktop','query':f'item_{size-1:05d}'}
    times=[];statuses=[]
    for _ in range(6):
        begin=time.perf_counter();response=api.search(**args);times.append((time.perf_counter()-begin)*1000);statuses.append(response['status'])
    write_samples=[];stop=threading.Event()
    def writer():
        while not stop.is_set():
            begin=time.perf_counter()
            with store.connection(write=True) as db:
                db.execute("UPDATE settings SET value=value WHERE key='current_persona'")
            write_samples.append((time.perf_counter()-begin)*1000)
            stop.wait(.01)
    thread=threading.Thread(target=writer);thread.start()
    try:
        concurrent=api.search(**args)['status']
    finally:
        stop.set();thread.join()
    tracemalloc.start()
    measured=capture(store,validate(args),Budget(seconds=60))
    _,peak=tracemalloc.get_traced_memory();tracemalloc.stop();del measured
    with store.connection() as db:
        tables={r[0]:db.execute('SELECT count(*) FROM '+r[0]).fetchone()[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('sqlite_')}
    begin=time.perf_counter()
    with store.connection(write=True) as db:
        db.execute('UPDATE settings SET value=value WHERE key=\'current_persona\'')
    write_ms=(time.perf_counter()-begin)*1000
    return {'documents':size,'cold_ms':round(times[0],3),'warm_p50_ms':round(statistics.median(times[1:]),3),
            'warm_p95_ms':round(sorted(times[1:])[-1],3),'statuses':statuses,
            'cache_bytes':api.index.path.stat().st_size if api.index.path.exists() else 0,'standalone_core_write_ms':round(write_ms,3),
            'concurrent_query_status':concurrent,'concurrent_write_max_ms':round(max(write_samples),3),
            'python_projection_peak_bytes':peak,
            'main_attempts':tables.get('attempts',0),'organization_attempts':tables.get('organization_attempts',0)}


def file_layout_comparison(base):
    store=Store(RuntimePaths(base/'layout-source'));store.initialize()
    service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid);sid=service.create_session()['id']
    with store.connection(write=True) as db:
        for i in range(1000):seed_turn(db,pid,sid,f'layout-{i}',[f'人工布局对照 document_{i}'])
    spec=validate({'persona_id':pid,'channel':'desktop','query':'document_999'})
    corpus=capture(store,spec,Budget())
    result={}
    for layout in ('single','per_scope'):
        begin=time.perf_counter();paths=set()
        for i in range(7):
            index=ArchiveIndex(Store(RuntimePaths(base/('layout-'+layout)/('shared' if layout=='single' else str(i)))))
            selected=spec|{'scope':spec['scope']|{'persona_id':f'artificial-scope-{i}'}}
            sample=corpus|{'scope':selected['scope'],'digest':f'artificial-digest-{i}'}
            with index.locked():
                state=index.synchronize(sample,selected,Budget())
                documents,_=index.candidates(selected,state,Budget())
                assert len(documents)==1
            paths.add(index.path)
        result[layout]={'build_and_query_ms':round((time.perf_counter()-begin)*1000,3),
                        'files':len(paths),'cache_bytes':sum(p.stat().st_size for p in paths),'scopes':7}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--sizes',default='1000,10000');args=parser.parse_args()
    output=args.output.resolve();repo=Path(__file__).resolve().parents[1]
    if output.is_relative_to(repo) or output.exists():parser.error('Choose a new D-drive evidence file outside the repository')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='m71-evaluate-',dir=RuntimePaths.from_environment().home/'tmp') as folder:
        base=Path(folder);store=Store(RuntimePaths(base/'functional'));store.initialize();pid,_,data=seed_fixture(store)
        report={'synthetic_only':True,'production_database_accessed':False,'provider_calls':0,'qq_sends':0,
                'quality':evaluate(store,pid,data),'development_comparison':candidates_comparison(),
                'rule_baseline':rule_baseline(data),
                'layout_comparison':file_layout_comparison(base),
                'performance':[performance(base,int(n)) for n in args.sizes.split(',') if n]}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'quality_passed':report['quality']['passed'],'quality_total':report['quality']['total'],
                      'recall':report['quality']['chinese_macro_recall_at_5'],'performance':report['performance']},ensure_ascii=False))
    if report['quality']['passed']!=report['quality']['total']:raise SystemExit(1)


if __name__=='__main__':main()
