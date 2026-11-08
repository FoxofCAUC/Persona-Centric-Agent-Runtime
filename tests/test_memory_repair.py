"""Memory-format regression, known billing, gaps and reviewed corrections."""
import json
import unittest
from copy import deepcopy

from tests import test_m4 as fixtures
from persona_runtime.models import RuntimeErrorBase, identifier
from persona_runtime.organization import OrganizationEngine, encoded
from persona_runtime.organization_output import parse, receipt, pending_conflicts
from persona_runtime.storage import Store


class MemoryRepairTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.M4Tests.setUp
    asyncTearDown = fixtures.M4Tests.asyncTearDown
    seed = fixtures.M4Tests.seed
    organize = fixtures.M4Tests.organize

    async def fixture(self):
        first = (await self.seed())[0]
        await self.organize(first['session_id'])
        old = self.org.candidates.accept(self.org.candidates.list()[0]['id'], identifier())['memory_id']
        turn = await self.chat.submit('desktop', '更正虚构偏好：现在喜欢红茶，不再喜欢乌龙茶。', identifier())
        await self.chat.wait()
        preview = self.org.preview(first['session_id'])
        def conflict(data):
            data['candidates'][0]['conflicts'] = [{'id': old, 'quote': '喜欢乌龙茶'}]
            return data
        self.model.transform = conflict
        return old, preview, self.chat.get(turn['id'])

    async def test_verified_object_both_source_formats_and_negative_controls(self):
        old, p, turn = await self.fixture()
        response, _ = await self.model.complete({}, self.org.messages(p))
        raw = json.loads(response)
        with self.store.connection() as db:
            audit = receipt(response)
            _, cs = self.org.validate_output(db, p, response, audit)
            self.assertEqual(cs[0]['conflicts'], [old])
            self.assertEqual(len(audit['normalizations']), 1)
            self.assertIsInstance(json.loads(response)['candidates'][0]['conflicts'][0], dict)
            p2 = dict(p, source_format=2, previous=None)
            p2['events'] = [dict(e, kind='event') for e in p['events']]
            raw2 = deepcopy(raw);raw2['format_version'] = 2;raw2['summary'] = raw2['summary'][-1:]
            for item in raw2['summary'] + raw2['candidates']:
                for s in item['sources']:s['source_id'] = s.pop('event_id')
            self.assertEqual(self.org.validate_output(db,p2,encoded(raw2))[1][0]['conflicts'],[old])
            for bad in ({'id':old,'quote':'编造引文'}, {'id':old,'quote':'喜欢乌龙茶','extra':1},
                        {'id':'other-persona','quote':'喜欢乌龙茶'}, 123, {'id':old,'quote':''}):
                data=deepcopy(raw);data['candidates'][0]['conflicts']=[bad]
                with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeErrorBase,'organization_invalid_output'):
                    self.org.validate_output(db,p,encoded(data))
            canonical=deepcopy(raw);canonical['candidates'][0]['conflicts']=[old]
            self.assertEqual(self.org.validate_output(db,p,encoded(canonical))[1][0]['conflicts'],[old])
        with self.store.connection(write=True) as db:
            db.execute('UPDATE memories SET revision=revision+1 WHERE id=?',(old,))
        with self.store.connection() as db, self.assertRaisesRegex(RuntimeErrorBase,'organization_invalid_output'):
            self.org.validate_output(db,p,response)

    async def test_invalid_output_keeps_usage_raw_receipt_and_atomicity(self):
        old,p,_=await self.fixture()
        self.model.transform=lambda data: dict(data,candidates=[dict(data['candidates'][0],conflicts=[{'id':old,'quote':'假引文'}])])
        before=(len(self.org.summaries.list()),len(self.org.candidates.list()))
        task=self.org.submit(p,identifier());await self.org.run_next();result=self.org.show(task['id'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(json.loads(result['attempts'][0]['usage_json'])['total_tokens'],10)
        self.assertEqual(result['attempts'][0]['output_diagnostic']['error']['path'],'candidates[0].conflicts[0].quote')
        self.assertEqual(before,(len(self.org.summaries.list()),len(self.org.candidates.list())))
        with self.store.connection() as db:
            row=db.execute('SELECT response_text,diagnostic_json FROM organization_receipts WHERE attempt_id=?',(result['attempts'][0]['id'],)).fetchone()
            self.assertEqual(receipt(row[0])['response_sha256'],json.loads(row[1])['response_sha256'])
        self.assertFalse(await self.org.run_next())

    async def test_pending_conflict_and_explicit_revision_survive_restart(self):
        old,p,turn=await self.fixture()
        task=self.org.submit(p,identifier());await self.org.run_next()
        candidate=self.org.candidates.list('pending')[0]
        with self.store.connection() as db:
            self.assertEqual(pending_conflicts(db,turn,{old})[0]['id'],candidate['id'])
        # Unapproved conflicts cannot silently append a second active fact.
        with self.assertRaisesRegex(RuntimeErrorBase,'conflict_revision_required'):
            self.org.candidates.accept(candidate['id'],identifier())
        with self.assertRaisesRegex(RuntimeErrorBase,'memory_version_conflict'):
            self.org.candidates.accept(candidate['id'],identifier(),target_memory_id=old,expected_revision=2)
        query=await self.chat.submit('desktop','虚构偏好现在喜欢什么茶？',identifier());await self.chat.wait()
        details=self.org.explain(query['id'])[0]['details']
        self.assertEqual(details['pending_conflicts'][0]['id'],candidate['id'])
        self.assertIsNone(details['summary_id'])
        result=self.org.candidates.accept(candidate['id'],identifier(),target_memory_id=old,expected_revision=1)
        self.assertTrue(self.org.candidates.accept(candidate['id'],identifier(),target_memory_id=old,expected_revision=1)['already_accepted'])
        Store(self.paths).initialize()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM memories WHERE id=?',(old,)).fetchone()[0],'superseded')
            self.assertEqual(db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],1)
            self.assertEqual(pending_conflicts(db,turn,{old}),[])
            self.assertEqual(db.execute("SELECT count(*) FROM summaries WHERE status='active'").fetchone()[0],0)
        query=await self.chat.submit('desktop','虚构偏好现在喜欢什么茶？',identifier());await self.chat.wait()
        self.assertEqual([m['id'] for m in self.org.explain(query['id'])[0]['details']['memories']],[result['memory_id']])

    async def test_gap_is_visible_after_restart_and_requires_explicit_rebuild(self):
        turns=await self.seed(21);sid=turns[0]['session_id']
        await self.organize(sid,end_row=10)
        self.model.transform=lambda data: dict(data,format_version=999)
        failed=await self.organize(sid,start_row=11,end_row=20)
        self.model.transform=lambda data:data
        p=self.org.preview(sid,start_row=21,end_row=21)
        self.assertEqual(p['coverage_gaps'],list(range(11,21)))
        task=self.org.submit(p,identifier());await self.org.run_next()
        active=next(s for s in self.org.summaries.list() if s['status']=='active')
        self.assertEqual(self.org.summaries.show(active['id'])['coverage_status'],'degraded')
        Store(self.paths).initialize()
        with self.assertRaisesRegex(RuntimeErrorBase,'coverage_gap_requires_rebuild'):self.org.preview(sid)
        self.assertEqual(self.org.show(failed['id'])['status'],'failed')
        self.assertEqual(self.org.preview(sid,rebuild=True)['rows'],list(range(1,11)))
        calls=len(self.model.calls);self.org.retry(task['id']);await self.org.run_next()
        self.assertEqual(len(self.model.calls),calls)
        await self.organize(sid,rebuild=True)
        await self.organize(sid)
        await self.organize(sid)
        active=next(s for s in self.org.summaries.list() if s['status']=='active')
        self.assertEqual(self.org.summaries.show(active['id'])['coverage_gaps'],[])
        self.assertEqual(self.org.summaries.show(active['id'])['covered'],list(range(1,22)))
        self.assertEqual(self.org.show(failed['id'])['status'],'failed')

    async def test_scope_revocation_and_rejection_never_promote_correction(self):
        old,p,turn=await self.fixture()
        response,_=await self.model.complete({},self.org.messages(p))
        with self.store.connection() as db:
            foreign=dict(p,persona_id=identifier())
            with self.assertRaisesRegex(RuntimeErrorBase,'organization_invalid_output'):self.org.validate_output(db,foreign,response)
        task=self.org.submit(p,identifier());await self.org.run_next()
        candidate=self.org.candidates.list('pending')[0]
        self.org.candidates.reject(candidate['id'],identifier())
        with self.store.connection() as db:
            self.assertEqual(pending_conflicts(db,turn,{old}),[])
            self.assertEqual(db.execute('SELECT status FROM memories WHERE id=?',(old,)).fetchone()[0],'active')
        with self.assertRaisesRegex(RuntimeErrorBase,'candidate_not_pending'):
            self.org.candidates.accept(candidate['id'],identifier(),target_memory_id=old,expected_revision=1)
        with self.store.connection(write=True) as db:
            from persona_runtime.memory_policy import exclude_memory
            memory=dict(db.execute('SELECT * FROM memories WHERE id=?',(old,)).fetchone())
            exclude_memory(db,memory,'user_withdrawn')
        with self.store.connection() as db,self.assertRaisesRegex(RuntimeErrorBase,'organization_invalid_output'):
            self.org.validate_output(db,p,response)

    async def test_sealed_session_uses_existing_memories_and_rebuilds_invalidated_coverage(self):
        from persona_runtime.sessions import SessionService
        from persona_runtime.session_organization import SessionOrganization
        old,_,turn=await self.fixture()
        sessions=SessionService(self.store)
        sid=turn['session_id'];pid=self.persona['id']
        current=sessions.show(sid,pid)
        sessions.seal(sid,pid,current['revision'],identifier())
        sealed=SessionOrganization(self.org)
        p=sealed.preview(sid,pid)
        self.assertIn(old,[m['id'] for m in p['existing_memories']])
        event=next(e for e in p['events'] if e['role']=='user')
        source={'source_id':event['id'],'quote':event['text']}
        data={'format_version':2,'summary':[{'text':event['text'],'sources':[source]}],
              'candidates':[{'category':'preference','text':event['text'],'sources':[source],'conflicts':[old]}]}
        async def complete(*args):return encoded(data),{'total_tokens':12}
        self.org.client.complete=complete
        task=sealed.submit(p,pid,sessions.show(sid,pid)['revision'],identifier())
        await self.org.run_next()
        self.assertEqual(self.org.show(task['id'])['status'],'completed')
        candidate=self.org.candidates.list('pending')[0]
        self.org.candidates.accept(candidate['id'],identifier(),target_memory_id=old,expected_revision=1)
        fresh=sealed.preview(sid,pid)
        self.assertEqual(fresh['status'],'ready')
        self.assertIn(turn['ordinal'] if 'ordinal' in turn else 2,fresh['rows'])

    async def test_upgrade_twenty_preserves_receipts_on_repeat_and_cascades_delete(self):
        import sqlite3
        db=sqlite3.connect(self.store.path)
        from persona_runtime.schema_m8d import TABLES as automatic_tables
        for name in reversed(automatic_tables):db.execute('DROP TABLE '+name)
        from persona_runtime.schema_m8a import TABLES as managed_tables
        for name in reversed(managed_tables):db.execute('DROP TABLE '+name)
        db.execute('DROP TABLE organization_receipts');db.execute('PRAGMA user_version=20');db.commit();db.close()
        self.store.initialize();self.store.initialize()
        self.assertTrue(list((self.paths.home/'backups').glob('before-memory-repair-*.sqlite3')))
        first=(await self.seed())[0];task=await self.organize(first['session_id'])
        with self.store.connection(write=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],1)
            # A receipt must not outlive a removable attempt; published manifests
            # intentionally protect real attempts with an append-only trigger.
            attempt=identifier()
            db.execute("INSERT INTO organization_attempts SELECT ?,task_id,started_epoch,status,error_code,usage_json,model_snapshot FROM organization_attempts WHERE task_id=?",(attempt,task['id']))
            db.execute("INSERT INTO organization_receipts VALUES (?,'artificial response','{}')",(attempt,))
            db.execute('DELETE FROM organization_attempts WHERE id=?',(attempt,))
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],1)


if __name__ == '__main__':unittest.main()
