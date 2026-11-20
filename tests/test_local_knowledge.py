"""Package 2 on an isolated SQLite store; providers only return controlled receipts."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from persona_runtime import local_knowledge as library, private_lookup
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.private_reply_output import SEMANTIC_REVIEW_VERSION
from tests import test_private_reply_stage3 as fixture


class LocalKnowledge(unittest.IsolatedAsyncioTestCase):
    reopen=fixture.ResearchTests.reopen
    submit=fixture.ResearchTests.submit
    consent=fixture.ResearchTests.consent
    paint=fixture.ResearchTests.paint
    show=fixture.ResearchTests.show
    continue_turn=fixture.ResearchTests.continue_turn

    def setUp(self):
        fixture.ResearchTests.setUp(self)
        self.providers.local_first=True
        self.providers.semantic_lookup=False  # Frozen pre-vector route exercised by this compatibility suite.
        self.providers.grounded_search=True
        self.providers.output_contract_version=SEMANTIC_REVIEW_VERSION
        self.providers.route='retrieve'

    def add(self,db,value='903',scope='persona',revision=0,predicate='属性乙',**opts):
        body='701 '+value+' 702'
        doc=library.put_document(db,scope=scope,persona_id=self.pid if scope!='instance' else None,
            session_id=self.sid if scope=='session' else None,consent_revision=revision,instance=self.config.load()['instance_id'],
            url='https://example.org/'+value,title='701',body=body,source_kind='page_content',receipt='123',
            acquired_at='2026-01-01')
        return library.put_fact(db,doc,subject='测试甲',predicate=predicate,value=value,quote=value,
            start=4,end=4+len(value),aliases=['代号丙'],attributes=['参数'],**opts)

    def policy(self,prefix,value):
        with self.store.connection(write=True) as db:
            db.execute('UPDATE knowledge_control SET value=? WHERE key=?',(json.dumps({'data':value,'revision':2}),
                       'knowledge.'+prefix+self.pid))

    def find(self,question,**kw):
        with self.store.connection() as db:
            return library.search(db,self.pid,self.sid,{'enabled':False,'revision':0},question,**kw)

    def test_index_and_validity(self):
        with self.store.connection(write=True) as db:
            a=self.add(db,valid_from='2026-04-01',valid_until='2026-06-01')
            self.assertEqual(self.add(db,valid_from='2026-04-01',valid_until='2026-06-01'),a)
        self.assertEqual(self.find('测试甲的属性乙',now='2026-05-01')[1][0]['id'],a)
        self.assertEqual(self.find('代号丙的参数',now='2026-05-01')[0],'hit')
        self.assertEqual(self.find('测试甲的属性乙',now='2026-06-01')[0],'expired')
        self.assertEqual(self.find('截至2026年5月1日测试甲的属性乙')[0],'hit')
        for question in ('测试甲二号的属性乙','测试甲的属性丙','测试甲第二版的属性乙','测试甲的属性乙和数量'):
            self.assertNotEqual(self.find(question,now='2026-05-01')[0],'hit')
        with self.store.connection(write=True) as db:library.withdraw(db,a)
        self.assertNotEqual(self.find('测试甲的属性乙',now='2026-05-01')[0],'hit')

    def test_catalogue_slots_and_scalar_conflicts(self):
        for value in ('第10章 710','第3章 703','第三章：703'):
            with self.store.connection(write=True) as db:self.add(db,value,predicate='章节目录')
        status,facts=self.find('测试甲章节目录')
        self.assertEqual(status,'hit')
        self.assertEqual(facts[-1]['value'],'第10章 710')
        with self.store.connection(write=True) as db:
            bad=self.add(db,'第３章：704',predicate='章节目录')
            self.add(db,'第3章 703',predicate='首章名称')
            self.add(db,'第4章 704',predicate='首章名称')
        self.assertEqual(self.find('测试甲章节目录'),('conflict',[]))
        self.assertEqual(self.find('测试甲首章名称'),('conflict',[]))
        with self.store.connection(write=True) as db:library.withdraw(db,bad)
        self.assertEqual(self.find('测试甲章节目录')[0],'hit')

    def test_catalogue_conflict_checked_before_result_limit(self):
        with self.store.connection(write=True) as db:
            ids=[self.add(db,f'{n}. {700+n}',predicate='目录') for n in range(1,11)]
        outcome,facts=self.find('测试甲目录')
        self.assertEqual((outcome,[f['id'] for f in facts]),('hit',ids[:8]))
        self.assertEqual(self.find('测试甲目录',fact_id=ids[9])[1][0]['id'],ids[9])
        with self.store.connection(write=True) as db:self.add(db,'10. 999',predicate='目录')
        self.assertEqual(self.find('测试甲目录',fact_id=ids[0]),('conflict',[]))

    def test_catalogue_subsections_include_the_parent_position(self):
        entries=['第一百零二章 第1节 801','第1章 第1节 701','第1章 第2节 702']
        with self.store.connection(write=True) as db:
            for entry in entries:self.add(db,entry,predicate='章节目录')
        self.assertEqual([f['value'] for f in self.find('测试甲章节目录')[1]],entries[1:]+entries[:1])
        with self.store.connection(write=True) as db:self.add(db,'第102章 第1节 799',predicate='章节目录')
        self.assertEqual(self.find('测试甲章节目录'),('conflict',[]))

    def test_catalogue_mixes_labelled_chapters_and_dotted_sections(self):
        entries=['Chapter 1: 701','1.1 711','1.2 712','Chapter 2: 702','2.1 721']
        with self.store.connection(write=True) as db:
            for value in reversed(entries):self.add(db,value,predicate='Table of Contents')
        self.assertEqual([f['value'] for f in self.find('测试甲 Table of Contents')[1]],entries)
        with self.store.connection(write=True) as db:
            self.add(db,'Chapter 1.2: 799',predicate='Table of Contents')
        self.assertEqual(self.find('测试甲 Table of Contents'),('conflict',[]))

    def test_catalogue_treats_han_typographic_spacing_as_equivalent(self):
        with self.store.connection(write=True) as db:
            self.add(db,'第一章　总　　则',predicate='目录')
            self.add(db,'第1章 总则',predicate='目录')
        self.assertEqual(self.find('测试甲目录')[0],'hit')
        with self.store.connection(write=True) as db:
            self.add(db,'第1章 监督检查',predicate='目录')
        self.assertEqual(self.find('测试甲目录'),('conflict',[]))

    def test_syllabus_orders_sessions_within_their_week(self):
        entries=['Week 1, Session 1: 701','Week 1, Session 2: 702','Week 2: 720','Week 3, Session 1: 731']
        with self.store.connection(write=True) as db:
            for value in reversed(entries):self.add(db,value,predicate='syllabus')
        self.assertEqual([f['value'] for f in self.find('测试甲 syllabus')[1]],entries)
        with self.store.connection(write=True) as db:
            bad=self.add(db,'Week 1, Session 2: 799',predicate='syllabus')
        self.assertEqual(self.find('测试甲 syllabus'),('conflict',[]))
        with self.store.connection(write=True) as db:library.withdraw(db,bad)
        self.assertEqual([f['value'] for f in self.find('测试甲 syllabus')[1]],entries)

    def test_catalogue_coalesces_roman_and_decimal_chapter_numbers(self):
        entries=['Chapter I. 701','Chapter 2. 702','3. 703']
        with self.store.connection(write=True) as db:
            for value in reversed(entries):self.add(db,value,predicate='Table of Contents')
        self.assertEqual([f['value'] for f in self.find('测试甲 Table of Contents')[1]],entries)
        with self.store.connection(write=True) as db:self.add(db,'Chapter 1. 799',predicate='Table of Contents')
        self.assertEqual(self.find('测试甲 Table of Contents'),('conflict',[]))

    def test_catalogue_separates_appendix_positions_from_main_chapters(self):
        entries=['1. 701','1.1. 711','Appendix A. 801','A.1. 811','Appendix B. 802']
        with self.store.connection(write=True) as db:
            for value in reversed(entries):self.add(db,value,predicate='Table of Contents')
        self.assertEqual([f['value'] for f in self.find('测试甲 Table of Contents')[1]],entries)
        with self.store.connection(write=True) as db:self.add(db,'Appendix A.1. 899',predicate='Table of Contents')
        self.assertEqual(self.find('测试甲 Table of Contents'),('conflict',[]))

    def test_scope_conflict_and_atomic_intake(self):
        self.consent(True)
        from persona_runtime.session_privacy import status
        rev=status(self.store,self.config,self.pid,self.sid)['revision']
        with self.store.connection(write=True) as db:
            self.add(db,scope='instance')
            self.add(db,'905',scope='session',revision=rev)
        with self.store.connection() as db:
            for pid,sid,revision,expected in ((self.pid,self.sid,rev,'905'),(self.pid,self.sid,rev+1,'903'),
                                          (self.pid,'other',rev,'903'),('other',self.sid,rev,None)):
                outcome,facts=library.search(db,pid,sid,{'enabled':True,'revision':revision},'测试甲属性乙',
                                           instance=self.config.load()['instance_id'])
                self.assertEqual(facts[0]['value'] if facts else None,expected)
        with self.store.connection(write=True) as db:self.add(db,'906',scope='instance')
        self.assertEqual(self.find('测试甲属性乙')[0],'conflict')
        with self.assertRaises(RuntimeErrorBase),self.store.connection(write=True) as db:
            self.add(db,'907')
            library.put_fact(db,'missing',subject='701',predicate='702',value='0',quote='0',start=0,end=1)
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM local_knowledge_documents WHERE title='701' AND body LIKE '%907%'").fetchone())

    async def test_local_hit_works_offline_and_revalidates_at_offer(self):
        self.policy('web.policy.',{'enabled':False})
        with self.store.connection(write=True) as db:a=self.add(db)
        tid=await self.submit('测试甲的属性乙是什么？')
        self.assertFalse(self.providers.lookups)
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'][0]['id'],a)
        self.assertFalse([c for c in self.providers.calls if c[0]=='J5'])
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual(private_lookup.details(self.store,self.pid,self.sid,tid,'research')['sources'][0]['content'],'903')
        self.reopen()
        second=await self.submit('代号丙的参数是什么？')
        unit=self.out.list(self.pid,self.sid,second)[0];plan=self.out.plan(self.pid,self.sid,unit['id'])
        with self.store.connection(write=True) as db:library.withdraw(db,a)
        offered=self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'901')
        self.assertEqual(offered['state'],'stopped')
        with self.assertRaises(RuntimeErrorBase):private_lookup.details(self.store,self.pid,self.sid,tid,'research')
        self.assertFalse(self.providers.lookups)

    async def test_j4_none_and_miss_are_distinct(self):
        self.providers.route='none'
        with self.store.connection(write=True) as db:self.add(db)
        tid=await self.submit('测试甲属性乙');self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        self.providers.route='retrieve'
        self.policy('web.policy.',{'enabled':False})
        tid=await self.submit('未知属性');self.show(tid,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['status'],'insufficient')
        with self.store.connection() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT outcome FROM local_knowledge_reads ORDER BY rowid')],['not_requested','miss'])
        self.assertFalse(self.providers.lookups)

    async def test_pronoun_recovers_only_a_readable_subject(self):
        self.policy('web.policy.',{'enabled':False})
        with self.store.connection(write=True) as db:
            self.add(db);self.add(db,'904',predicate='属性丁')
        first=await self.submit('测试甲属性乙')
        self.assertIn('903',self.show(first,'main'))
        following=await self.submit('那它的属性丁呢？')
        self.assertIn('904',self.show(following,'main'))
        self.assertFalse(self.providers.lookups)

    async def test_qualified_source_needs_selection_and_works_with_web_disabled(self):
        self.policy('web.policy.',{'enabled':False})
        with self.store.connection(write=True) as db:self.add(db,conditions=['地区丙'])
        self.assertNotEqual(self.find('测试甲属性乙')[0],'hit')
        self.assertNotEqual(self.find('地区丁测试甲属性乙')[0],'hit')
        tid=await self.submit('地区丙测试甲的属性乙')
        self.assertIn('地区丙',self.show(tid,'main'))
        self.assertFalse(self.providers.lookups)
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND node='local.J5'",(tid,)).fetchone())

    async def test_local_storage_failure_resumes_without_duplicate_j4(self):
        with self.store.connection(write=True) as db:self.add(db)
        busy=sqlite3.OperationalError('database is locked')
        busy.sqlite_errorcode=sqlite3.SQLITE_BUSY
        with patch.object(library,'_search',side_effect=busy):
            tid=await self.submit('测试甲属性乙')
        self.assertEqual(self.chat.get(tid)['error_code'],'desktop_storage_wait')
        self.assertFalse(self.providers.lookups)
        self.assertFalse(self.providers.bodies)
        from persona_runtime.ingress import IngressService
        self.reopen();self.chat.recover()
        ingress=IngressService(self.chat)
        ingress.network_wake();ingress.schedule_committed()
        await self.chat.wait()
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual(sum(n=='J4' for n,*_ in self.providers.calls),1)
        self.assertFalse(self.providers.lookups)

    async def test_local_selection_can_fall_back_to_web_selection_without_replay(self):
        with self.store.connection(write=True) as db:self.add(db,conditions=['地区丙'])
        choose=self.providers.choose
        count=0
        async def select(node,attempt,state,options):
            nonlocal count
            if node=='J5':
                count+=1
                if count==1:return 'insufficient',{}
            return await choose(node,attempt,state,options)
        self.providers.choose=select
        tid=await self.submit('帮我查一下地区丙测试甲的属性乙')
        self.assertFalse(self.providers.lookups)
        self.show(tid,'search_notice');await self.continue_turn(tid)
        self.assertIn('903',self.show(tid,'main'))
        with self.store.connection() as db:
            nodes={r[0] for r in db.execute("SELECT node FROM private_reply_nodes WHERE turn_id=?",(tid,))}
            self.assertTrue({'local.J5','J5'}<=nodes)
            self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',(tid,)).fetchone()[0],'insufficient')
        self.assertEqual(len(self.providers.lookups),1)

    async def test_explicit_local_only_miss_never_starts_search(self):
        tid=await self.submit('只查本地资料，未知属性')
        self.show(tid,'main')
        self.assertEqual(self.providers.bodies[-1][1]['research']['status'],'insufficient')
        self.assertFalse(self.providers.lookups)

    async def test_legacy_turn_source_remains_bound_to_original_record(self):
        self.providers.local_first=False;self.providers.route='web'
        first=await self.submit('帮我查一下701')
        self.show(first,'search_notice');await self.continue_turn(first);self.show(first,'main')
        with self.store.connection(write=True) as db:
            source=private_lookup.evidence(db,self.pid,self.sid,first)['sources'][0]
            library.admit_turn_source(db,self.pid,self.sid,first,source['id'],subject='测试甲',predicate='属性乙',
                                      value='903',quote='903',start=0,end=3)
        self.providers.local_first=True;self.providers.route='retrieve'
        tid=await self.submit('测试甲属性乙')
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual(len(self.providers.lookups),1)
        from persona_runtime.knowledge_service import KnowledgeService
        private_lookup.edit_source(KnowledgeService(self.store),self.pid,source['id'],'withdrawn',0,'pkg2-withdraw',None)
        self.assertNotEqual(self.find('测试甲属性乙')[0],'hit')

    async def test_refresh_uses_notice_before_web_and_then_completes(self):
        with self.store.connection(write=True) as db:self.add(db)
        tid=await self.submit('帮我查一下测试甲属性乙，请联网核实')
        self.assertFalse(self.providers.lookups)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',(tid,)).fetchone()[0],'refresh_requested')
        self.show(tid,'search_notice');self.reopen();self.chat.recover()
        await self.continue_turn(tid)
        self.assertIn('903',self.show(tid,'main'))
        self.assertEqual(len(self.providers.lookups),1)

    def test_legacy_source_is_checked_again_on_use(self):
        from persona_runtime.knowledge_store import key
        page,doc,batch,claim='page-1','doc-1','batch-1','claim-1'
        body='701 903'
        from time import time
        now=time()
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO knowledge_pages VALUES(?,?,?,?,?,NULL,1,'active',?)",
                       (page,'wikidata','Q1','701','https://example.org/1',now))
            db.execute("INSERT INTO knowledge_documents VALUES(?,?,1,?,?,'{}','{}',NULL,?)",
                       (doc,page,hashlib.sha256(body.encode()).hexdigest(),body,now))
            db.execute('UPDATE knowledge_pages SET head_document_id=? WHERE id=?',(doc,page))
            db.execute("INSERT INTO knowledge_batches VALUES(?,?,?,?,'committed',NULL,NULL,'{}',?)",(batch,doc,self.pid,'w',now))
            db.execute("INSERT INTO knowledge_claims VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?)",
                (claim,page,doc,batch,key('测试甲','属性乙'),'测试甲','属性乙','903','fact','active',now))
            db.execute('INSERT INTO knowledge_evidence VALUES(?,?,4,7,?)',(claim,doc,'903'))
            library.admit_legacy(db,self.pid,claim)
        self.assertEqual(self.find('测试甲的属性乙')[0],'hit')
        with self.store.connection(write=True) as db:db.execute("UPDATE knowledge_pages SET state='withdrawn' WHERE id=?",(page,))
        self.assertNotEqual(self.find('测试甲属性乙')[0],'hit')
