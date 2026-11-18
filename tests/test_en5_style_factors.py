"""Isolated marker tests for the two projections and the frozen admission route."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime import style_study as study
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier, timestamp, RuntimeErrorBase, LOCAL_USER_ID
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import encode
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests.test_en4_style_baseline import Capture


def span(text):
    return {'field':study.FIELD,'source_sha256':digest(text.encode()),'span':[1,3],
            'removed_sha256':digest(text[1:3].encode())}


class Factors(unittest.TestCase):
    def test_only_exact_reviewed_span_and_attribute_are_changed(self):
        original='A乙丙Z'
        val={'id':study.FIELD,'kind':'anchor','data':original}
        part={'kind':'anchor','id':study.FIELD,'text':encode(val)}
        other=dict(part,id='identity.aliases')
        parts, applied=study.project_parts([other,part],span(original))
        self.assertTrue(applied)
        self.assertIs(parts[0],other)
        self.assertEqual(json.loads(parts[1]['text']),val|{'data':'AZ'})
        self.assertEqual(part['text'],encode(val))
        with self.assertRaisesRegex(RuntimeErrorBase,'field_changed'):
            study.project_parts([part],span(original+'X'))
        with self.assertRaisesRegex(RuntimeErrorBase,'span_changed'):
            study.project_parts([part],span(original)|{'span':[2,4]})

    def test_single_persona_term_only_after_ranking(self):
        g={'turn_id':'T','members':[{'id':'U','text':'乙丙'}],'replies':[{'id':'P','text':'甲乙 丁丙 M42'}]}
        p={'kind':'event','id':'P'}
        def match(words,recent=(),ids=(),refs=(),literals=()):
            return study.weak_persona_match(g,ids or [p],set(words),list(literals),set(recent),set(refs))
        self.assertEqual(match(['甲乙']),'甲乙')
        for result in (match(['甲乙'],recent=['甲乙']),match(['甲乙'],refs=['T']),match(['甲乙','丁丙']),
                       match(['m42']),match(['甲乙'],ids=[p,{'kind':'event','id':'U'}]),
                       match(['甲乙'],literals=['m42']),match(['甲乙'],ids=[{'kind':'event','id':'X'}])):
            self.assertIsNone(result)
        current=[{'id':'M1','text':'甲乙'},{'id':'M2','text':'丁丙'}]
        item={'kind':'retrieval','group':g}
        ev={'items':[item],'retrieval':{'matched_targets':{'T':[p]}}}
        with patch.object(study,'load_turn_input',return_value=current):
            self.assertEqual(study.filter_history(None,{},[item],ev,{'turn_ids':[]}),([item],[]))
        with patch.object(study,'load_turn_input',return_value=current[:1]):
            kept, why=study.filter_history(None,{},[item],ev,{'turn_ids':[]})
            self.assertEqual((kept,why[0]['term']),([], '甲乙'))
            # A match that is already part of recent history is never removed.
            self.assertEqual(study.filter_history(None,{},[dict(item,kind='recent')],ev,{'turn_ids':[]})[1],[])


class NormalAdmission(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scratch=tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        paths=RuntimePaths(Path(scratch.name))
        cfg=ConfigStore(paths);cfg.initialize();cfg.update({'model':{'base_url':'https://api.deepseek.com'}})
        self.store=Store(paths);self.store.initialize()
        service=RuntimeService(self.store);self.pid=service.create_persona(generic_persona())['id'];service.use_persona(self.pid)
        self.client=Capture()
        self.chat=ChatEngine(self.store,cfg,self.client)
        self.addCleanup(self.chat.knowledge.close)
        self.ingress=IngressService(self.chat)
        self.sid=self.chat.session()['session_id']
        chars=CharacterService(self.store)
        head=chars.current(self.pid)
        data=head['data']; data['social']['context_rules']='A乙丙Z'
        chars.update(self.pid,data,head['id'],identifier())
        self.policy={'version':study.VERSION,'arm':'PH','study_id':'ID-TEST','persona_id':self.pid,'sessions':[self.sid],
                     'character_version':chars.current(self.pid)['id'],'projection':span('A乙丙Z')}
        self.install(self.policy)

    def install(self,policy):
        with self.store.connection(write=True) as db:
            prev=self.ingress.policy(db,self.pid,'desktop')
            data=json.loads(prev['data_json'])
            if policy is None:data.pop(study.KEY,None)
            else:data[study.KEY]=policy
            vid=identifier()
            db.execute('INSERT INTO interaction_policies (id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES(?,?,?,?,?,?,?,?,?)',
                       (vid,self.pid,prev['user_id'],'desktop',prev['revision']+1,prev['mode'],encode(data),timestamp(),prev['reply_mode']))
            db.execute('UPDATE interaction_heads SET version_id=? WHERE persona_id=? AND channel=?',(vid,self.pid,'desktop'))

    async def test_unapproved_route_and_rollback_do_not_generate_or_reinterpret(self):
        t=await self.chat.submit('desktop','ID-1',identifier(),self.sid)
        await self.chat.wait()
        self.assertEqual(self.chat.get(t['id'])['error_code'],'style_study_specific_input_approval_required')
        self.assertFalse(self.client.requests)
        self.install(None)
        with self.assertRaisesRegex(RuntimeErrorBase,'revision_changed'):
            await self.chat.retry(t['id'])
        after=await self.chat.submit('desktop','ID-2',identifier(),self.sid)
        await self.chat.wait()
        self.assertEqual(self.chat.get(after['id'])['status'],'completed')
        self.assertIn('A乙丙Z',self.client.requests[0]['messages'][0]['content'])

    async def test_normal_renderer_freezes_candidate_and_reopen_reads_output(self):
        self.chat.style_study_dispatch_guard=lambda db,turn,config,request_hash:None  # local marker transport only
        t=await self.chat.submit('desktop','ID-1',identifier(),self.sid)
        await self.chat.wait()
        outcome=self.chat.get(t['id'])
        self.assertEqual(outcome['status'],'completed',outcome.get('error_code'))
        sy=self.client.requests[0]['messages'][0]['content']
        self.assertIn('AZ',sy); self.assertNotIn('A乙丙Z',sy); self.assertNotIn(study.VERSION,sy)
        self.assertEqual(outcome['context']['style_study']['arm'],'PH')
        self.install(None)
        reopened=ChatEngine(self.store,self.chat.config,self.client)
        self.addCleanup(reopened.knowledge.close)
        reopened.recover()
        self.assertEqual(reopened.history(self.sid)[0]['reply'],outcome['reply'])
        self.assertEqual(len(self.client.requests),1)

    async def test_batched_acceptance_keeps_its_version_after_policy_rollback(self):
        self.chat.style_study_dispatch_guard=lambda *_: None
        batch=self.ingress.accept({'channel':'desktop','text':'ID-BATCH','request_id':identifier(),
                                   'persona_id':self.pid,'session_id':self.sid})
        self.install(None)
        linked=self.ingress.action(batch['batch_id'],'seal',batch['batch_revision'],identifier(),self.pid)
        await self.chat.wait()
        self.assertEqual(self.chat.get(linked['turn_id'])['error_code'],'style_study_policy_withdrawn')
        with self.store.connection() as db:
            saved=db.execute('SELECT context_policy_snapshot FROM turns WHERE id=?',(linked['turn_id'],)).fetchone()
            self.assertEqual(json.loads(saved[0])[study.KEY],self.policy)
        self.assertFalse(self.client.requests)

    async def test_role_change_and_withdrawal_reject_at_physical_boundary(self):
        called=0
        def withdraw(db,turn,cfg,request_hash):
            nonlocal called
            called+=1
            if called==1:
                db.execute("INSERT INTO source_exclusions VALUES(?,?,?,'test',?)",(turn['user_event_id'],self.pid,LOCAL_USER_ID,timestamp()))
        self.chat.style_study_dispatch_guard=withdraw
        t=await self.chat.submit('desktop','ID-X',identifier(),self.sid)
        await self.chat.wait()
        self.assertEqual(self.chat.get(t['id'])['error_code'],'style_study_input_withdrawn')
        self.assertFalse(self.client.requests)
        with self.store.connection() as db:
            raw=dict(db.execute('SELECT * FROM turns WHERE id=?',(t['id'],)).fetchone())
        with self.assertRaisesRegex(RuntimeErrorBase,'scope_changed'):
            study.policy_for(dict(raw,persona_id='ID-OTHER'))
