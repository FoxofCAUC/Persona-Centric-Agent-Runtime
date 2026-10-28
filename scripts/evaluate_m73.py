"""Artificial lifecycle fixtures and long-session contention; zero provider calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager,closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.models import RuntimeErrorBase,generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import SessionService
from persona_runtime.storage import Store
from persona_runtime.topics import TopicService
from persona_runtime.memory_policy import exclude_sources
from tests.m71_fixtures import seed_turn


def setup(home):
    paths=RuntimePaths(home);store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
    service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    engine=ChatEngine(store,cfg);sid=engine.session()['session_id']
    return store,service,engine,pid,sid


def check_case(case,root):
    with tempfile.TemporaryDirectory(prefix='fixture-',dir=root) as d:
        store,service,engine,pid,sid=setup(Path(d));now=[0.];sessions=SessionService(store,lambda:now[0]);topics=TopicService(store)
        with store.connection(write=True) as db:seed_turn(db,pid,sid,'sample',case['members'])
        args={'session_id':sid,'persona_id':pid};kind=case['scenario'];sessions.tick()
        if kind=='idle':
            now[0]=1799;assert sessions.tick()==[];sessions.show(**args);now[0]=1800;assert sessions.tick()[0]['state']=='sealed'
        else:
            row=sessions.show(**args);sessions.seal(**args,expected_revision=row['revision'],operation_id='seal')
        package=sessions.package(**args);assert len(package['manifest']['turns'][0]['member_ids'])==len(case['members'])
        assert not any(text in json.dumps(package,ensure_ascii=False) for text in case['members'])
        if kind=='withdraw':
            with store.connection(write=True) as db:exclude_sources(db,pid,['sample-e'+str(len(case['members']))],'fixture_withdrawal')
            assert not sessions.replay(**args)['groups'][0]['usable']
        elif kind=='scope':
            other=service.create_persona(generic_persona())['id']
            try:sessions.package(sid,other)
            except RuntimeErrorBase:pass
            else:raise AssertionError('cross-persona package exposed')
        elif kind=='topic':
            segment=topics.create(pid,'人工主题','不进入召回','topic',session_id=sid,turn_ids=['sample'])
            thread=topics.create(pid,'人工Thread','','thread',kind='thread')
            topics.link(thread['id'],segment['id'],pid,1,1,'link')
            assert topics.show(thread['id'],pid,kind='thread')['affects_recall'] is False
        elif kind=='restore':
            backup=Path(d)/'copy.sqlite3'
            with closing(sqlite3.connect(store.path)) as src,closing(sqlite3.connect(backup)) as dst:src.backup(dst)
            with closing(sqlite3.connect(backup)) as db:
                assert db.execute('SELECT content_hash FROM session_packages').fetchone()[0]==package['content_hash']
                assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
        elif kind=='late_source':
            try:service.append_event(sid,'note','late note')
            except RuntimeErrorBase as error:assert str(error)=='session_sealed'
            else:raise AssertionError('late append allowed')
        elif kind=='reopen':
            row=sessions.show(**args);new=sessions.reopen(**args,expected_revision=row['revision'],operation_id='reopen')
            assert new['session_id']!=sid;assert sessions.package(**args)==package
        assert sessions.package(**args)==package
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]==0
        return {'id':case['id'],'scenario':kind,'members':len(case['members']),'passed':True}


def benchmark(root):
    with tempfile.TemporaryDirectory(prefix='long-',dir=root) as d:
        store,service,engine,pid,sid=setup(Path(d));sessions=SessionService(store)
        with store.connection(write=True) as db:
            for i in range(1000):seed_turn(db,pid,sid,'long-'+str(i),['人工长会话 '+str(i),'补充资料 '+str(i)])
        revision=sessions.show(sid,pid)['revision'];writes=[];real=store.connection
        @contextmanager
        def timed(*,write=False):
            started=time.perf_counter()
            with real(write=write) as conn:yield conn
            if write:writes.append((time.perf_counter()-started)*1000)
        store.connection=timed;read_ms=[];ingress_ms=[]
        other=service.create_persona(generic_persona())['id'];service.use_persona(other);other_sid=engine.session()['session_id'];service.use_persona(pid)
        start=time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            work=pool.submit(sessions.seal,sid,pid,revision,'long-seal')
            n=0
            while not work.done():
                t=time.perf_counter()
                with store.connection() as db:assert db.execute('SELECT count(*) FROM session_packages').fetchone()[0] in (0,1)
                read_ms.append((time.perf_counter()-t)*1000)
                t=time.perf_counter()
                with store.connection(write=True) as db:seed_turn(db,other,other_sid,'concurrent-'+str(n),['人工并发接收 '+str(n)])
                ingress_ms.append((time.perf_counter()-t)*1000);n+=1;time.sleep(.01)
            result=work.result()
        assert result['state']=='sealed'
        assert len(sessions.package(sid,pid)['manifest']['turns'])==1000
        return {'turns':1000,'members':2000,'total_seal_ms':(time.perf_counter()-start)*1000,
                'write_transaction_max_ms':max(writes),'read_max_ms':max(read_ms,default=0),
                'concurrent_authoritative_ingress_max_ms':max(ingress_ms,default=0),'concurrent_inputs':n,
                'note':'Concurrent authoritative fixture inserts; HTTP health/delivery are tested separately.','passed':n>0}


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--development',action='store_true');p.add_argument('--benchmark',action='store_true');a=p.parse_args()
    if a.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Evidence outside source repository')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    cases=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/m73'/('development.json' if a.development else 'acceptance.json')).read_text(encoding='utf-8'))
    root=RuntimePaths.from_environment().home/'tmp';results=[check_case(c,root) for c in cases]
    report={'cases':results,'passed':all(r['passed'] for r in results),'count':len(results),'major_calls':0,'organization_calls':0,'qq_sends':0}
    if a.benchmark:report['benchmark']=benchmark(root);report['passed'] &= report['benchmark']['passed']
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in report.items() if k!='cases'}))

if __name__=='__main__':main()
