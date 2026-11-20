import json
import unittest
from unittest.mock import patch

from persona_runtime import character_embedding, knowledge_coverage, knowledge_study, local_knowledge, semantic_knowledge
from persona_runtime.models import RuntimeErrorBase
from tests import test_knowledge_study as study_tests, test_knowledge_revisions as revision_tests


class SemanticRoute(unittest.IsolatedAsyncioTestCase):
    reopen=study_tests.Study.reopen
    submit=study_tests.Study.submit
    show=study_tests.Study.show
    paint=study_tests.Study.paint
    cold=study_tests.Study.cold
    continue_turn=study_tests.Study.continue_turn

    def setUp(self):
        study_tests.Study.setUp(self)
        self.providers.semantic_lookup=True
        self.providers.knowledge_revisions=True
        self.providers.consume=False
        available=patch.object(semantic_knowledge,'require_encoder')
        available.start();self.addCleanup(available.stop)
        continuity=patch.object(semantic_knowledge,'related_questions',return_value=False)
        continuity.start();self.addCleanup(continuity.stop)
        for owner, name in ((local_knowledge,'search'),(knowledge_coverage,'select'),(knowledge_coverage,'requirements'),
                            (knowledge_study,'_bound_read')):
            guard=patch.object(owner,name,side_effect=AssertionError('legacy lexical path'))
            guard.start()
            self.addCleanup(guard.stop)

    async def test_saved_sources_reach_the_desktop_without_word_alignment(self):
        ranking=patch.object(semantic_knowledge,'rank',side_effect=lambda paths,q,rows,**options:
                             [semantic_knowledge.public_material(r) for r in rows[:2]])
        ranking.start();self.addCleanup(ranking.stop)
        async def organize(aid,packet,model):
            self.organizations.append(aid)
            return {'raw':'{}'},{}
        self.providers.organize=organize
        tid=await self.cold()
        self.show(tid,'main')
        first=self.providers.bodies[-1][1]['research']
        self.assertEqual(first['status'],'local_hit')
        self.assertEqual(first['facts'][0]['text'],self.sources[0]['summary'])
        self.reopen()
        again=await self.submit('测试甲的属性乙是什么？')
        self.assertEqual(self.providers.bodies[-1][1]['research']['facts'],first['facts'])
        self.assertEqual((len(self.organizations),len(self.providers.lookups)),(1,2))  # search and supplemental page
        unit=next(u for u in self.out.list(self.pid,self.sid,again) if u['phase']=='main')
        plan=self.out.plan(self.pid,self.sid,unit['id'])
        with self.store.connection(write=True) as db:
            self.assertFalse(db.execute("SELECT 1 FROM private_reply_nodes WHERE node LIKE '%J5%'").fetchone())
            local_knowledge.withdraw_document(db,first['facts'][0]['source_ids'][0])
        self.assertEqual(self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'902')['state'],'stopped')

    async def test_checklist_presets_are_not_used_on_new_vector_turns(self):
        from persona_runtime.private_reply_output import FACT_REVIEW_VERSION
        del self.providers.semantic_lookup  # The normal local-first entry defaults to semantic recall.
        self.providers.structured_study=True
        self.providers.output_contract_version=FACT_REVIEW_VERSION
        with patch.object(semantic_knowledge,'rank',side_effect=lambda paths,q,rows,**options:
                          [semantic_knowledge.public_material(r) for r in rows[:1]]):
            first=await self.cold();self.show(first,'main')
            self.assertFalse(self.organizations)
            with self.store.connection() as db:
                self.assertFalse(db.execute("SELECT 1 FROM private_reply_nodes WHERE node LIKE '%J5%' OR node='J7'").fetchone())
                self.assertTrue(db.execute("SELECT 1 FROM private_reply_reviews WHERE reason='local_publication_ready'").fetchone())

    async def test_continuation_uses_acquisition_provenance_through_cold_and_warm_reads(self):
        self.providers.structured_study=True
        self.sources=[
            {'url':'https://example.org/701','title':'701','content':'701 711。','kind':'page_content'},
            {'url':'https://example.org/702','title':'702','content':'702 722。','kind':'page_content'},
        ]
        j4=[]
        choose=self.providers.choose

        async def observe(node,attempt,state,options):
            if node=='J4':
                j4.append(state)
                if state['question'] in ('900','903'):
                    return 'none',{}
            return await choose(node,attempt,state,options)

        def rank(paths,question,rows,*,limit=6):
            wanted='702' if question in ('901','902','903') else None if question=='900' else '701'
            return [semantic_knowledge.public_material(f) for f in rows if f['title']==wanted][:limit]

        self.providers.choose=observe
        with patch.object(semantic_knowledge,'rank',side_effect=rank):
            first=await self.cold();self.show(first,'main')
            self.assertEqual([f['title'] for f in self.providers.bodies[-1][1]['research']['facts']],['701'])
            unrelated=await self.submit('900');self.show(unrelated,'main')
            self.assertNotIn('previous_reply',j4[-1])
            # Recall the research topic once more to create a warm answer from one document.
            warm=await self.submit('701');self.show(warm,'main')
            self.assertEqual([f['title'] for f in self.providers.bodies[-1][1]['research']['facts']],['701'])
            for q in ('901','902'):
                tid=await self.submit(q);self.show(tid,'main')
                self.assertIn('previous_reply',j4[-1])
                research=self.providers.bodies[-1][1]['research']
                self.assertEqual(research['status'],'local_hit')
                self.assertEqual([f['title'] for f in research['facts']],['702','701'])
                with self.store.connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM private_lookup_calls WHERE turn_id=?',(tid,)).fetchone()[0],0)
            with self.store.connection(write=True) as db:
                doc=db.execute("SELECT id FROM local_knowledge_documents WHERE title='702'").fetchone()[0]
                local_knowledge.withdraw_document(db,doc)
            after=await self.submit('903');self.show(after,'main')
            self.assertNotIn('previous_reply',j4[-1])

    async def test_local_continuation_keeps_evidence_when_j4_declines_new_lookup(self):
        self.providers.structured_study=True
        self.sources=[
            {'url':'https://example.org/701','title':'701','content':'701 711。','kind':'page_content'},
            {'url':'https://example.org/702','title':'702','content':'702 722。','kind':'page_content'},
        ]
        choose=self.providers.choose

        async def decline(node,attempt,state,options):
            if node=='J4' and state['question'] in ('901','902'):
                self.assertIn('previous_reply',state)
                return 'none',{}
            return await choose(node,attempt,state,options)

        def rank(paths,question,rows,*,limit=6):
            wanted='702' if question in ('901','902') else '701'
            return [semantic_knowledge.public_material(f) for f in rows if f['title']==wanted][:limit]

        self.providers.choose=decline
        with patch.object(semantic_knowledge,'rank',side_effect=rank):
            first=await self.cold();self.show(first,'main')
            mid=await self.submit('901');self.show(mid,'main')
            tip=await self.submit('902')
        for tid in (mid,tip):
            with self.store.connection() as db:
                self.assertEqual(db.execute('SELECT choice FROM private_lookup_intents WHERE turn_id=?',(tid,)).fetchone()[0],'none')
                self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',(tid,)).fetchone()[0],'continued')
                self.assertFalse(db.execute('SELECT 1 FROM private_lookup_calls WHERE turn_id=?',(tid,)).fetchone())
        research=self.providers.bodies[-1][1]['research']
        self.assertEqual(research['status'],'local_hit')
        self.assertEqual([f['title'] for f in research['facts']],['702'])
        unit=next(u for u in self.out.list(self.pid,self.sid,tip) if u['phase']=='main')
        plan=self.out.plan(self.pid,self.sid,unit['id'])
        with self.store.connection(write=True) as db:
            doc=db.execute("SELECT id FROM local_knowledge_documents WHERE title='702'").fetchone()[0]
            local_knowledge.withdraw_document(db,doc)
        self.assertEqual(self.out.offer(self.pid,self.sid,unit['id'],plan['id'],plan['revision'],0,'902')['state'],'stopped')

    async def test_a_specific_link_cannot_be_satisfied_by_carrying_old_context(self):
        link=self.sources[0]['url']
        choose=self.providers.choose

        async def decide(node,attempt,state,options):
            if node=='J4' and state['question']==link:
                self.assertIn('previous_reply',state)
                return 'none',{}
            return await choose(node,attempt,state,options)

        self.providers.choose=decide
        with patch.object(semantic_knowledge,'rank',side_effect=lambda paths,q,rows:
                          [semantic_knowledge.public_material(r) for r in rows[:1]]):
            self.show(await self.cold(),'main')
            tid=await self.submit(link);self.show(tid,'main')
        self.assertFalse(self.providers.bodies[-1][1]['research']['facts'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT outcome FROM local_knowledge_reads WHERE turn_id=?',(tid,)).fetchone()[0],
                             'not_requested')


class SemanticEligibility(unittest.TestCase):
    admit=revision_tests.RevisionTests.admit

    def setUp(self):
        # Use the same public store and persona fixture as the revision reader.
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.config import ConfigStore
        from persona_runtime.storage import Store
        from persona_runtime.service import RuntimeService
        from persona_runtime.models import PersonaSpec
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup)
        paths=RuntimePaths(Path(directory.name));config=ConfigStore(paths);config.initialize()
        self.store=Store(paths);self.store.initialize()
        svc=RuntimeService(self.store)
        self.pid=svc.create_persona(PersonaSpec(1,'700','701',['702'],'703',['704'],'705'))['id']
        svc.use_persona(self.pid);self.sid=svc.create_session()['id']
        self.base={'instance':config.load()['instance_id'],'consent':{'enabled':False,'revision':0}}
        self.turn={'id':'','persona_id':self.pid,'session_id':self.sid}

    def test_unavailable_local_encoder_is_reported_separately_from_a_miss(self):
        with patch.object(character_embedding,'numpy_module',side_effect=character_embedding.SemanticFailure('embedding_dependency_missing')):
            self.assertEqual(semantic_knowledge.rank(self.store.paths,'900',[]),[])
            with self.assertRaisesRegex(RuntimeErrorBase,'local_knowledge_embedding_unavailable'):
                semantic_knowledge.rank(self.store.paths,'900',[{'text':'801'}])

    def test_independent_observations_survive_but_a_withdrawal_does_not(self):
        with self.store.connection(write=True) as db:
            db.execute('INSERT OR REPLACE INTO knowledge_control VALUES(?,?)',('knowledge.policy.'+self.pid,
                       json.dumps({'revision':1,'data':{'enabled':True,'channels':['desktop']}})))
            a=self.admit(db,'801','https://example.org/a','receipt-a','2026-01-01',expires='2026-12-01')
            b=self.admit(db,'802','https://example.org/b','receipt-b','2026-01-01',expires='2026-12-01')
            query=lambda:semantic_knowledge.materials(db,self.turn,self.base,'900',now='2026-02-01')
            self.assertEqual({r['id'] for r in query()},{a,b})
            local_knowledge.withdraw(db,b)
            self.assertEqual([r['id'] for r in query()],[a])
            self.assertEqual(semantic_knowledge.materials(db,self.turn,self.base,'900',now='2027-02-01'),[])
