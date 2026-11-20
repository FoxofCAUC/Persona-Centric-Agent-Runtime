"""Package 4 exercises immutable receipts through real SQLite readers, writers and backup restore."""
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest.mock import Mock

from persona_runtime import local_knowledge as lib
from persona_runtime.automatic_backup import Backups
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.ordinary_providers import ReviewedPrivateChatProviders
from tests.test_workbench_operations import InstanceTest
from tests import test_knowledge_study as study_fixture


class RevisionTests(InstanceTest):
    def setUp(self):
        super().setUp()
        with self.store.connection(write=True) as db:
            self.pid=db.execute('select persona_id from sessions where id=?',(self.sid,)).fetchone()[0]
            db.execute('INSERT OR REPLACE INTO knowledge_control VALUES(?,?)',
                       ('knowledge.policy.'+self.pid,json.dumps({'revision':1,'data':{'enabled':True,'channels':['desktop']}})))

    def admit(self,db,value,url,receipt,at,*,state='fact',conditions=(),published=None,since=None,until=None,
              expires=None,previous=None,body=None):
        body=body or value
        doc=lib.put_document(db,scope='instance',url=url,title='701',body=body,source_kind='page_content',
                             receipt=receipt,acquired_at=at,published_at=published,versioned=True)
        return lib.put_fact(db,doc,subject='甲',predicate='属性乙',value=value,quote=body,start=0,end=len(body),
                            evidence_state=state,conditions=conditions,valid_from=since,valid_until=until,expires_at=expires,
                            previous_id=previous,versioned=True,verification_receipt=receipt)

    def find(self,db,q='甲的属性乙',**kw):
        return lib.search(db,self.pid,self.sid,{'enabled':False,'revision':0},q,versioned=True,now='2026-11-20',**kw)

    def test_duplicate_rechecks_and_two_sources_form_one_claim(self):
        with self.store.connection(write=True) as db:
            a=self.admit(db,'701','https://EXAMPLE.org:443/a#one','a','2026-01-02',published='2026-01-01')
            self.assertEqual(self.admit(db,'701','https://example.org/a','a','2026-01-02',published='2026-01-01'),a)
            self.admit(db,'701','https://example.org/a','b','2026-01-04',published='2026-01-03')
            self.admit(db,'701','https://example.net/b','c','2026-01-05')
            self.assertEqual(db.execute('select count(*) from local_knowledge_documents').fetchone()[0],2)
            self.assertEqual(db.execute("select count(*) from knowledge_control where key like 'private.study.acquisition.%'").fetchone()[0],3)
            outcome,rows=self.find(db)
            self.assertEqual((outcome,len(rows)),('hit',1))
            self.assertEqual(sorted(len(e['receipts']) for e in rows[0]['evidence']),[1,2])
            self.assertEqual(rows[0]['evidence'][1]['relation'],'same_content')
            self.assertEqual(self.find(db,fact_id=a)[1][0]['id'],a)

    def test_rumor_announcement_observation_and_late_old_receipt(self):
        with self.store.connection(write=True) as db:
            for v,state,day in [('703','observed','2026-03-01'),('701','rumor','2026-01-01'),('702','announced','2026-02-01')]:
                self.admit(db,v,'https://example.org/'+v,v,day,state=state,since=day)
            for question,val in [('现在甲的属性乙','703'),('原来甲属性乙的爆料怎么说','701'),
                                 ('截至2026年2月15日甲的属性乙','702'),('甲属性乙的公告','702')]:
                outcome,rows=self.find(db,question)
                self.assertEqual((outcome,[r['value'] for r in rows]),('hit',[val]),question)
                self.assertTrue(rows[0]['url'].endswith(val))

    def test_versions_expiry_and_conflict(self):
        with self.store.connection(write=True) as db:
            a=self.admit(db,'701','https://example.org/1','a','2026-01-01',state='observed',conditions=['1.9版'])
            b=self.admit(db,'702','https://example.org/2','b','2026-03-01',state='observed',conditions=['1.10版'],
                         expires='2026-06-01')
            self.assertEqual(self.find(db)[0],'expired')
            self.assertEqual(self.find(db,'现在甲1.10版的属性乙')[0],'expired')
            self.assertEqual(self.find(db,'甲1.9版属性乙')[1][0]['id'],a)
            self.assertNotEqual(self.find(db,fact_id=b)[0],'hit')
            self.admit(db,'702','https://example.org/2','c','2026-11-19',state='observed',conditions=['1.10版'])
            self.assertEqual(self.find(db)[1][0]['value'],'702')
            self.admit(db,'705','https://example.net/3','d','2026-11-19',state='observed',conditions=['1.10版'])
            self.assertEqual(self.find(db),('conflict',[]))
            self.assertEqual(self.find(db,fact_id=a),('conflict',[]))

    def test_versions_keep_unchanged_claim_and_scope(self):
        with self.store.connection(write=True) as db:
            for version,day in [('1.0版','2026-01-01'),('2.0版','2026-03-01')]:
                self.admit(db,'701','https://example.org/a',version,day,conditions=[version,'平台：PC'],since=day)
            self.admit(db,'702','https://example.org/mobile','m','2026-03-01',conditions=['2.0版','平台：移动'])
            old=self.find(db,'甲1.0版PC的属性乙')[1][0]
            new=self.find(db,'甲现在PC的属性乙')[1][0]
            self.assertEqual(old['claim_id'],new['claim_id'])
            self.assertNotEqual(old['revision_id'],new['revision_id'])
            self.assertEqual(self.find(db,'甲移动的属性乙')[1][0]['value'],'702')
            self.assertNotEqual(self.find(db)[0],'hit')

    def test_withdrawn_document_is_not_reactivated_by_rechecking_identical_content(self):
        with self.store.connection(write=True) as db:
            self.admit(db,'701','https://example.org/a','a','2026-01-01')
            self.admit(db,'701','https://example.net/b','b','2026-01-02')
            doc=db.execute("SELECT id FROM local_knowledge_documents WHERE url='https://example.org/a'").fetchone()[0]
            lib.withdraw_document(db,doc)
        with self.assertRaisesRegex(RuntimeErrorBase,'local_knowledge_document_withdrawn'),self.store.connection(write=True) as db:
            self.admit(db,'701','https://example.org/a','c','2026-01-03')
        with self.store.connection() as db:
            facts=self.find(db)[1]
            self.assertEqual([e['url'] for e in facts[0]['evidence']],['https://example.net/b'])

    def test_late_replayed_page_does_not_overwrite_a_newer_correction(self):
        with self.store.connection(write=True) as db:
            for value,received,published in [('702','2026-06-01','2026-05-01'),('701','2026-06-02','2026-01-01')]:
                self.admit(db,value,'https://example.org/a',value,received,published=published)
            self.assertEqual(self.find(db)[1][0]['value'],'702')
            self.assertEqual(self.find(db,'截至2026年2月1日甲属性乙')[1][0]['value'],'701')

    def test_return_to_previous_value_is_a_new_revision(self):
        with self.store.connection(write=True) as db:
            for value,day in [('701','2026-01-01'),('702','2026-02-01'),('701','2026-03-01')]:
                self.admit(db,value,'https://example.org/a',day,day,published=day)
            snapshots=[self.find(db,'截至2026年'+month+'月15日甲属性乙')[1][0] for month in ('1','2','3')]
            self.assertEqual([r['value'] for r in snapshots],['701','702','701'])
            self.assertEqual(len({r['claim_id'] for r in snapshots}),1)
            self.assertEqual(len({r['revision_id'] for r in snapshots}),3)

    def test_corroborated_chapter_still_belongs_to_the_complete_source_outline(self):
        from persona_runtime.knowledge_catalog import outline_groups
        entries=['第1章 801','第2章 802','第3章 803']
        with self.store.connection(write=True) as db:
            for url,items in [('https://example.org/a',entries),('https://example.net/b',[entries[1]])]:
                body='。'.join(items)
                doc=lib.put_document(db,scope='instance',url=url,title='701',body=body,source_kind='page_content',
                                     receipt='a',acquired_at='2026-01-01',versioned=True)
                for item in items:
                    start=body.index(item)
                    lib.put_fact(db,doc,subject='甲',predicate='目录',value=item,quote=item,start=start,end=start+len(item),versioned=True)
            status,rows=self.find(db,'甲目录')
            self.assertEqual((status,len(rows)),('hit',3))
            self.assertEqual([[f['value'] for f in group] for group in outline_groups(rows).values()],[entries])

    def test_failed_update_rolls_back_evidence_and_revision_guards(self):
        with self.store.connection(write=True) as db:
            self.admit(db,'701','https://example.org/a','a','2026-01-01',published='2026-01-01')
        with self.assertRaises(InterruptedError):
            with self.store.connection(write=True) as db:
                self.admit(db,'702','https://example.org/a','b','2026-02-01',published='2026-02-01')
                raise InterruptedError()
        with self.store.connection() as db:
            self.assertEqual(db.execute('select count(*) from local_knowledge_facts').fetchone()[0],1)
            self.assertEqual(self.find(db)[1][0]['value'],'701')

    def test_public_correction_restore_and_withdrawal(self):
        with self.store.connection(write=True) as db:
            a=self.admit(db,'701','https://example.org/1','a','2026-01-01',since='2026-01-01')
        backups=Backups(self.paths)
        backups.create(200000)
        with self.store.connection(write=True) as db:
            b=self.admit(db,'702','https://example.org/1','b','2026-05-01',since='2026-05-01',previous=a)
        for suffix,expected in [('corrected','702'),('withdrawn',None)]:
            if expected is None:
                with self.store.connection(write=True) as db:lib.withdraw(db,b)
            target=self.base/suffix
            report=backups.restore(target)
            self.assertTrue(report['knowledge_reconciliation']['live_evidence_checked'])
            self.assertEqual(report['database_sha256'],hashlib.sha256((target/'runtime.sqlite3').read_bytes()).hexdigest())
            with closing(sqlite3.connect(target/'runtime.sqlite3')) as db:
                db.row_factory=sqlite3.Row
                self.assertEqual([f['value'] for f in self.find(db)[1]],[expected] if expected else [])
                self.assertEqual(self.find(db,'截至2026年2月1日甲属性乙')[1][0]['id'],a)
                self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')

    def test_restore_distinguishes_a_locked_live_index_from_an_unavailable_one(self):
        from unittest.mock import patch
        from persona_runtime.knowledge_revision_restore import reconcile
        with self.store.connection(write=True) as db:
            self.admit(db,'701','https://example.org/a','a','2026-01-01')
        image=self.base/'image.sqlite3'
        connect=sqlite3.connect
        with closing(connect(self.store.path)) as live,closing(connect(image)) as dst:
            live.backup(dst)
            live.execute('BEGIN EXCLUSIVE')
            with patch('sqlite3.connect',side_effect=lambda *a,**kw:connect(*a,timeout=0.01,**kw)):
                with self.assertRaisesRegex(RuntimeErrorBase,'local_knowledge_storage_wait'):
                    reconcile(image,self.store.path,self.config)
            live.rollback()
        with closing(connect(image)) as db:self.assertEqual(db.execute('select withdrawn from local_knowledge_facts').fetchone()[0],0)
        result=reconcile(image,self.base/'unavailable.sqlite3',self.config)
        self.assertFalse(result['live_evidence_checked'])
        self.assertEqual(result['disabled_old_evidence'],1)

    def test_independent_transactions_never_expose_partial_merge(self):
        gate=Barrier(3)
        written=Event()
        def writer(n):
            gate.wait()
            with self.store.connection(write=True) as db:
                self.admit(db,'703','https://example.org/'+str(n),str(n),'2026-01-02')
                written.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.store.connection() as reader:
                self.assertEqual(self.find(reader)[0],'miss')
                jobs=[pool.submit(writer,n) for n in range(2)]
                gate.wait()
                self.assertTrue(written.wait(timeout=5))
                self.assertEqual(self.find(reader)[0],'miss')
            for job in jobs:job.result(timeout=15)
            with self.store.connection() as reader:
                facts=self.find(reader)[1]
                self.assertEqual((len(facts),len(facts[0]['evidence'])),(1,2))

    def test_organizer_reads_current_main_key_on_each_dispatch(self):
        from unittest.mock import AsyncMock
        adapter=object.__new__(ReviewedPrivateChatProviders)
        adapter.text_flash_account=None
        main={'name':'other-main','base_url':'https://api.deepseek.com','encrypted_key':'701','max_input_chars':5000}
        model=main|{'name':'deepseek-flash'}
        adapter.config=Mock()
        adapter.config.load.side_effect=lambda:{'model':main}
        adapter.study_enabled=True
        recipient={k:model[k] for k in ('name','base_url')}
        adapter.bundle={'search':recipient,'knowledge_study':{'model':recipient}}
        adapter.study_instructions='701'
        adapter.organizer=Mock(complete=AsyncMock(return_value=('{}',{})))
        for key in ('701','702'):
            main['encrypted_key']=key
            scoped=main|{'name':'deepseek-flash'}
            asyncio.run(adapter.organize('701',{},scoped))
            sent=adapter.organizer.complete.call_args.args[0]
            self.assertEqual((sent['name'],sent['encrypted_key']),('deepseek-flash',key))
        with self.assertRaises(RuntimeErrorBase):asyncio.run(adapter.organize('701',{},model))
        main['base_url']='https://example.org'
        with self.assertRaises(RuntimeErrorBase):asyncio.run(adapter.organize('701',{},scoped))
        self.assertEqual(adapter.organizer.complete.call_count,2)

    def test_structured_organizer_budget_is_independent_of_chat(self):
        from unittest.mock import AsyncMock
        from persona_runtime import knowledge_coverage
        adapter=object.__new__(ReviewedPrivateChatProviders)
        adapter.text_flash_account=None
        main={'name':'deepseek-flash','base_url':'https://api.deepseek.com','encrypted_key':'701',
              'max_input_chars':5000,'max_tokens':1024}
        adapter.config=Mock()
        adapter.config.load.return_value={'model':main}
        adapter.study_enabled=adapter.structured_study=True
        recipient={k:main[k] for k in ('name','base_url')}
        adapter.bundle={'search':recipient,'knowledge_study':{'model':recipient}}
        adapter.study_instructions='701'
        adapter.organizer=Mock(complete=AsyncMock(return_value=('{}',{})))
        for expanded,expected in ((False,knowledge_coverage.OUTPUT_TOKENS),(True,knowledge_coverage.RETRY_OUTPUT_TOKENS)):
            asyncio.run(adapter.organize('701',{},main|{'_study_length_retry':expanded}))
            wire=adapter.organizer.complete.call_args.args[0]
            self.assertEqual(wire['max_tokens'],expected)
            self.assertNotIn('_study_length_retry',wire)
        self.assertEqual(main['max_tokens'],1024)


class WorkbenchRevisionTests(unittest.IsolatedAsyncioTestCase):
    reopen=study_fixture.Study.reopen
    submit=study_fixture.Study.submit
    consent=study_fixture.Study.consent
    paint=study_fixture.Study.paint
    show=study_fixture.Study.show
    continue_turn=study_fixture.Study.continue_turn
    cold=study_fixture.Study.cold

    def setUp(self):
        study_fixture.Study.setUp(self)
        self.providers.knowledge_revisions=True
        self.sources.append(self.sources[0]|{'url':'https://example.net/702'})
        initial=self.extract
        self.extract=lambda sources:[initial([s])[0] for s in sources]

    async def test_source_withdrawal_updates_details_and_blocks_selected_primary(self):
        from persona_runtime import private_lookup
        from persona_runtime.knowledge_service import KnowledgeService
        tid=await self.cold();self.show(tid,'main')
        facts=self.providers.bodies[-1][1]['research']['facts']
        self.assertEqual((len(facts),len(facts[0]['evidence'])),(1,2))
        first,second=facts[0]['evidence']
        with self.store.connection() as db:
            def source(e):
                return json.loads(db.execute('select spec_json from local_knowledge_facts where id=?',
                                             (e['receipts'][0],)).fetchone()[0])['turn_source'][1]
            first_key,second_key=source(first),source(second)
        service=KnowledgeService(self.store)
        private_lookup.edit_source(service,self.pid,second_key,'withdrawn',0,'701',None)
        details=private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        self.assertEqual([s['url'] for s in details['sources']],[first['url']])
        private_lookup.edit_source(service,self.pid,first_key,'corrected',1,'702','704')
        with self.assertRaises(RuntimeErrorBase):private_lookup.details(self.store,self.pid,self.sid,tid,'research')

    async def test_restoring_session_does_not_revive_revoked_grant(self):
        self.consent(True)
        tid=await self.cold();self.show(tid,'main')
        backups=Backups(self.store.paths);backups.create(200000)
        self.consent(False)
        restore_root=TemporaryDirectory()
        self.addCleanup(restore_root.cleanup)
        target=Path(restore_root.name)/'restored'
        report=backups.restore(target)
        self.assertGreater(report['knowledge_reconciliation']['disabled_old_evidence'],0)
        with closing(sqlite3.connect(target/'runtime.sqlite3')) as db:
            self.assertEqual(db.execute('select count(*) from local_knowledge_facts where withdrawn=0').fetchone()[0],0)

    async def test_workbench_correction_is_admitted_with_its_own_quote(self):
        from persona_runtime import private_lookup
        from persona_runtime.knowledge_service import KnowledgeService
        self.consent(True)
        self.sources=self.sources[:1]
        first=await self.cold();self.show(first,'main')
        original=self.providers.bodies[-1][1]['research']['facts'][0]
        with self.store.connection() as db:
            spec=json.loads(db.execute('select spec_json from local_knowledge_facts where id=?',(original['id'],)).fetchone()[0])
        private_lookup.edit_source(KnowledgeService(self.store),self.pid,spec['turn_source'][1],
                                   'corrected',0,'703','测试甲 属性乙 904。')
        self.extract=lambda s:[dict(source_id=s[0]['id'],subject='测试甲',predicate='属性乙',value='904',quote=s[0]['text'],state='fact')]
        again=await self.cold('帮我查一下测试甲属性乙，请联网核实')
        self.show(again,'main')
        fact=self.providers.bodies[-1][1]['research']['facts'][0]
        self.assertEqual((fact['source_kind'],fact['value'],fact['text']),('user_correction','904','测试甲 属性乙 904。'))
        self.assertEqual(fact['claim_id'],original['claim_id'])
        self.assertNotEqual(fact['revision_id'],original['revision_id'])
