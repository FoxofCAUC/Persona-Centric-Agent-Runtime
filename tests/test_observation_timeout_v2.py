"""Versioned understanding deadlines, compatibility and safe error codes."""
import asyncio
import json
from unittest.mock import patch
import time
import unittest
from persona_runtime.chat import ModelError
from persona_runtime.situation_observation import (VERSION,CONFIGURED_VERSION,LEGACY_VERSION,timeout_for,policy_for,provider_error_code,prompt_for,LEGACY_PROMPT)
from tests import test_m763_repair as fixtures


class VersionedTimeout(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.Repair.asyncSetUp;asyncTearDown=fixtures.Repair.asyncTearDown
    source=fixtures.Repair.source;document=fixtures.Repair.document;publish=fixtures.Repair.publish
    publish_document=fixtures.Repair.publish_document;observe_setup=fixtures.Repair.observe_setup
    admit=fixtures.Repair.admit;mode=fixtures.Repair.mode;queued=fixtures.Repair.queued

    def configure(self,tid,version,seconds):
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            policy=json.loads(turn['context_policy_snapshot']);policy['situation_observation']['version']=version
            model=json.loads(turn['observation_model_snapshot']);model['timeout']=seconds
            db.execute('UPDATE turns SET context_policy_snapshot=?,observation_model_snapshot=? WHERE id=?',
                       (json.dumps(policy),json.dumps(model),tid))

    async def test_v2_request_uses_configured_sixty_and_records_it(self):
        self.observe_setup();tid=await self.queued('给我建议');self.configure(tid,CONFIGURED_VERSION,60)
        observed=await self.engine.observer.prepare(tid)
        self.assertEqual(self.fake.calls[-1]['timeout'],60)
        self.assertGreater(self.fake.calls[-1]['_request_deadline']-time.monotonic(),58)
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            self.assertEqual(policy_for(turn)['version'],CONFIGURED_VERSION)
            manifest=json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE task_type='situation_observation'").fetchone()[0])
            self.assertEqual(manifest['timeout_seconds'],60);self.assertEqual(manifest['contract'],CONFIGURED_VERSION)
            self.assertEqual(self.engine.observer.recheck(db,turn,observed),observed)

    async def test_legacy_snapshot_and_receipt_keep_fifteen(self):
        self.observe_setup();tid=await self.queued('给我建议');self.configure(tid,LEGACY_VERSION,60)
        observed=await self.engine.observer.prepare(tid)
        self.assertEqual(observed['version'],LEGACY_VERSION);self.assertEqual(self.fake.calls[-1]['timeout'],15)
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            self.assertEqual(self.engine.observer.recheck(db,turn,observed),observed)
        self.assertEqual(len(self.fake.calls),1)

    async def test_lower_configured_timeout_is_respected(self):
        self.assertEqual(timeout_for({'timeout':30},{'version':CONFIGURED_VERSION}),30)
        self.assertEqual(timeout_for({'timeout':60},{'version':LEGACY_VERSION}),15)

    async def test_v3_request_freezes_240_profile_and_new_task_contract(self):
        self.observe_setup();tid=await self.queued('给我建议');observed=await self.engine.observer.prepare(tid)
        self.assertEqual(self.fake.calls[-1]['timeout'],240)
        self.assertGreater(self.fake.calls[-1]['_request_deadline']-time.monotonic(),238)
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            self.assertEqual(policy_for(turn)['version'],VERSION)
            self.assertEqual(observed['version'],VERSION)
        self.assertIn('先识别用户要求完成的具体目标',self.fake.messages[-1][0]['content'])
        self.assertEqual(prompt_for({'version':CONFIGURED_VERSION}),LEGACY_PROMPT)
        self.assertEqual(prompt_for({'version':LEGACY_VERSION}),LEGACY_PROMPT)

    async def test_explicit_stream_profile_has_no_outer_observation_deadline(self):
        self.observe_setup();tid=await self.queued('给我建议')
        with self.store.connection(write=True) as db:
            model=json.loads(db.execute('SELECT observation_model_snapshot FROM turns WHERE id=?',(tid,)).fetchone()[0])
            model['_generation_profile']={'version':'chat-stream-unlimited-v1'}
            db.execute('UPDATE turns SET observation_model_snapshot=? WHERE id=?',(json.dumps(model),tid))
        original=asyncio.wait
        with patch('persona_runtime.situation_observation.asyncio.wait',wraps=original) as waiting:
            result=await self.engine.observer.prepare(tid)
        self.assertEqual(result['status'],'completed')
        self.assertIsNone(waiting.call_args.kwargs['timeout'])
        self.assertIsNone(self.fake.calls[-1]['_request_deadline'])
        with self.store.connection() as db:
            manifest=json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE task_type='situation_observation'").fetchone()[0])
        self.assertIsNone(manifest['timeout_seconds'])
        self.assertEqual(manifest['provider_request_contract']['timeout_policy'],'none')

    async def test_provider_code_kept_without_raw_exception_or_replay(self):
        self.observe_setup();tid=await self.queued('给我建议')
        async def failed(model,messages):
            self.fake.calls.append(model);raise ModelError('provider_timeout',diagnostics={'phase':'awaiting_headers'})
        self.fake.complete=failed
        observed=await self.engine.observer.prepare(tid)
        self.assertEqual(observed['error'],'provider_timeout')
        self.assertEqual(await self.engine.observer.prepare(tid),observed);self.assertEqual(len(self.fake.calls),1)

    async def test_unknown_policy_never_calls(self):
        self.observe_setup();tid=await self.queued('给我建议');self.configure(tid,'unknown-v99',60)
        with self.assertRaisesRegex(Exception,'observation_snapshot_invalid'):await self.engine.observer.prepare(tid)
        self.assertEqual(self.fake.calls,[])


class ErrorCodes(unittest.TestCase):
    def test_unknown_sensitive_messages_are_not_recorded(self):
        self.assertEqual(provider_error_code(ValueError('private input and token')),'observation_provider_failed')
        self.assertEqual(provider_error_code(ModelError('provider_http_429')),'provider_http_429')
        self.assertEqual(provider_error_code(ModelError('provider_http_429?private')),'observation_provider_failed')
