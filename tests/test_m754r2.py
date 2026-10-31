"""R2 isolated selector, accounting and protocol regression tests."""
import asyncio
from copy import deepcopy
import json
import sqlite3
import time
import unittest
from unittest.mock import patch
from tests import test_m753 as prior
lore=prior.lore
from persona_runtime.character_embedding import SemanticFailure
from persona_runtime.character_retrieval import load_config
from persona_runtime.character_selector_protocol import candidates,parse_output,messages,PROMPT_VERSION
from persona_runtime.models import identifier
from persona_runtime.quotas import QuotaService

class SelectorTests(prior.IntegrationTests):
    # Reuse setup/helpers without inheriting historical test collection below.
    def enable(self):
        self.config.update({'character_selector':{'enabled':True,'base_url':'http://127.0.0.1:1/v1','name':'selector'}})
        self.policy(character_retrieval_mode='hybrid_llm')

    def pool(self,*args):
        return candidates(args[0],args[2],{'lore':[],'example':[]},load_config())

    async def test_success_separate_attempts_and_manifest(self):
        self.enable();t=await self.turn('书店')
        async def model(cfg,msg):
            self.model.calls.append(msg)
            if cfg['name']=='selector':return '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}',{'total_tokens':3}
            return '主回复',{'total_tokens':5}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=model):await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['status'],'completed')
        self.assertEqual(len(self.model.calls),2)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT state FROM quota_reservations').fetchone()[0],'consumed')
            self.assertEqual(db.execute('SELECT count(*) FROM context_manifests').fetchone()[0],2)
        used=QuotaService(self.store,self.config).current()['used'];self.assertEqual(used['project_hourly'],2)
        await self.engine._run(t['id']);self.assertEqual(len(self.model.calls),2)

    async def test_last_quota_kept_for_main(self):
        self.enable();self.config.update({'model':{'hourly_limit':1}});t=await self.turn('书店')
        with patch.object(self.rt,'local_pool',side_effect=self.pool):await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['status'],'completed');self.assertEqual(len(self.model.calls),1)
        self.assertEqual(self.engine.selector.list(self.pid),[])

    async def test_invalid_output_fallback_and_usage(self):
        self.enable();t=await self.turn('书店')
        with patch.object(self.rt,'local_pool',side_effect=self.pool):await self.engine._run(t['id'])
        self.assertEqual(self.engine.get(t['id'])['status'],'completed')
        rows=self.engine.selector.list(self.pid);self.assertEqual(rows[0]['error_code'],'selector_invalid_output')
        self.assertIsNotNone(rows[0]['usage_json']);self.assertEqual(len(self.model.calls),2)

    async def test_empty_success_not_degradation(self):
        self.enable();t=await self.turn('书店')
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',return_value=('{"schema_version":1,"lore_ids":[],"example_ids":[]}',{})):
            result=await self.engine.selector.prepare(t['id'])
        self.assertEqual(result['effective_mode'],'hybrid_llm');self.assertIsNone(result['degrade_reason'])
        self.engine.selector.release(t['id'])

    async def test_ledger_failure_zero_calls(self):
        self.enable();t=await self.turn('书店')
        with self.store.connection(write=True) as db:db.execute("CREATE TRIGGER fail_manifest BEFORE INSERT ON context_manifests BEGIN SELECT RAISE(ABORT,'injected'); END")
        with patch.object(self.rt,'local_pool',side_effect=self.pool):await self.engine._run(t['id'])
        self.assertFalse(self.model.calls);self.assertEqual(self.engine.selector.list(self.pid),[])

    async def test_model_snapshot_and_retry(self):
        self.enable();t=await self.turn('书店');self.config.update({'character_selector':{'name':'changed'}})
        self.assertEqual(json.loads(self.raw(t['id'])['selector_model_snapshot'])['name'],'selector')
        with patch.object(self.rt,'local_pool',side_effect=self.pool):await self.engine._run(t['id'])
        with self.store.connection(write=True) as db:db.execute("UPDATE turns SET status='failed' WHERE id=?",(t['id'],))
        with patch.object(self.engine,'_schedule'):await self.engine.retry(t['id'])
        self.assertEqual(self.raw(t['id'])['execution_revision'],2)
        self.assertEqual(json.loads(self.raw(t['id'])['selector_model_snapshot'])['name'],'changed')

    async def test_readonly_preview_and_export(self):
        self.enable()
        with patch.object(self.rt,'local_pool',side_effect=self.pool):result=self.rt.preview(self.pid,'书店',channel='desktop',mode='hybrid_llm')
        self.assertEqual(result['effective_mode'],'lexical');self.assertFalse(self.model.calls)
        path=self.service.export_persona()['path'];value=json.loads(__import__('pathlib').Path(path).read_text(encoding='utf-8'))
        self.assertEqual(value['format_version'],16);self.assertNotIn('encrypted_key',json.dumps(value))

    async def test_probe_durable_no_turn_and_idempotent(self):
        self.enable();op=identifier()
        with patch.object(self.model,'complete',return_value=('{"schema_version":1,"lore_ids":["probe"],"example_ids":[]}',{})) as call:
            await self.engine.selector.probe(self.pid,op);await self.engine.selector.probe(self.pid,op)
        self.assertEqual(call.call_count,1)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM turns').fetchone()[0],0)

    async def test_timeout_late_response_and_busy_slot(self):
        self.enable();t=await self.turn('书店');pool=self.pool(self.version,{},['书店'])
        model=self.config.load()['character_selector']|{'timeout':0.03};gate=asyncio.Event()
        async def stubborn(*args):
            try:await gate.wait()
            except asyncio.CancelledError:await gate.wait()
            return '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}',{'total_tokens':7}
        with patch.object(self.model,'complete',side_effect=stubborn):
            start=time.monotonic();answer,error,aid=await self.engine.selector._call(self.pid,self.raw(t['id']),model,messages(pool,['书店'],'',2,48000,prompt_version=PROMPT_VERSION),pool,2)
            self.assertLess(time.monotonic()-start,0.5);self.assertEqual(error,'selector_timeout');self.assertIsNone(answer)
            self.assertTrue(self.engine.selector.busy)
            with self.assertRaisesRegex(SemanticFailure,'selector_busy'):await self.engine.selector._call(self.pid,None,model,[],pool,2,identifier())
            gate.set();await asyncio.sleep(.02)
        row=self.engine.selector.list(self.pid)[0];self.assertEqual(row['status'],'failed');self.assertIsNone(row['selected_json'])
        self.assertFalse(self.engine.selector.busy);self.engine.selector.release(t['id'])

    async def test_reservation_blocks_other_calls_and_restart_releases(self):
        self.enable();self.config.update({'model':{'hourly_limit':2}});t=await self.turn('书店');pool=self.pool(self.version,{},['书店']);model=self.config.load()['character_selector']
        with self.store.connection(write=True) as db:
            aid=self.engine.selector._start(db,self.pid,self.raw(t['id']),model,messages(pool,['书店'],'',2,48000,prompt_version=PROMPT_VERSION),pool)
        with self.store.connection(write=True) as db:
            with self.assertRaisesRegex(SemanticFailure,'selector_project_quota'):self.engine.selector._start(db,self.pid,None,model,messages(pool,['书店'],'',2,48000,prompt_version=PROMPT_VERSION),pool,identifier())
        with self.store.connection(write=True) as db:self.engine.selector.recover(db)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM quota_reservations').fetchone()[0],'released')
            self.assertEqual(db.execute('SELECT status FROM character_selection_attempts').fetchone()[0],'interrupted')
        self.assertFalse(self.model.calls)

    async def test_cancel_during_selector_no_primary(self):
        self.enable();t=await self.turn('书店');started=asyncio.Event();gate=asyncio.Event()
        async def blocked(*args):started.set();await gate.wait();return '{"schema_version":1,"lore_ids":[],"example_ids":[]}',{}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=blocked) as call:
            task=asyncio.create_task(self.engine._run(t['id']));await started.wait()
            self.engine.cancel_queued(t['id']);gate.set();await task
        self.assertEqual(call.call_count,1);self.assertEqual(self.engine.get(t['id'])['attempt_count'],0)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT state FROM quota_reservations').fetchone()[0],'released')

    async def test_config_copy_independent_and_old_quota_journal(self):
        self.config.copy_selector_model();name=self.config.load()['character_selector']['name']
        self.config.update({'model':{'name':'new-primary'}})
        self.assertEqual(self.config.load()['character_selector']['name'],name)
        quota=QuotaService(self.store,self.config);before=quota.current()['limits'];quota.begin_acceptance();quota.end_acceptance()
        self.assertEqual(quota.current()['limits'],before)
        from persona_runtime.config import atomic_json
        atomic_json(quota.journal,{'status':'active','restore_limits':{'project_hourly':7,'background_hourly':2,'background_daily':3}})
        quota.end_acceptance();self.assertEqual(quota.current()['limits']['project_hourly'],7)
        self.assertEqual(quota.current()['limits']['selector_daily'],200)

    async def test_cancel_retry_while_cloud_inflight_does_not_reply_old_run(self):
        self.enable();t=await self.turn('书店');started=asyncio.Event();gate=asyncio.Event()
        async def blocked(*args):started.set();await gate.wait();return '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}',{}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=blocked) as call:
            old=asyncio.create_task(self.engine._run(t['id']));await started.wait()
            self.engine.cancel_queued(t['id'])
            with patch.object(self.engine,'_schedule'):await self.engine.retry(t['id'])
            gate.set();await old
        self.assertEqual(call.call_count,1);self.assertEqual(self.engine.get(t['id'])['status'],'queued')
        self.assertEqual(self.engine.get(t['id'])['attempt_count'],0)

    async def test_api_permissions_health_and_probe_record_lookup(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        self.enable();ready=asyncio.Event();gate=asyncio.Event()
        async def slow(*args):ready.set();await gate.wait();return '{"schema_version":1,"lore_ids":["probe"],"example_ids":[]}',{}
        with patch.object(self.model,'complete',side_effect=slow):
            server=TestServer(create_app(self.paths,self.model));await server.start_server()
            try:
                async with ClientSession() as client:
                    for action in ('character.selector-status','character.selector-records','character.selector-test','character.selector-copy-main'):
                        async with client.post(server.make_url('/v1/manage'),headers={'Authorization':'Bearer '+self.config.token('bridge')},json={'action':action,'args':{}}) as response:self.assertEqual(response.status,403)
                    pending=asyncio.create_task(client.post(server.make_url('/v1/manage'),headers={'Authorization':'Bearer '+self.config.token()},json={'action':'character.selector-test','args':{'persona_id':self.pid,'operation_id':identifier()}}))
                    await ready.wait()
                    async with client.get(server.make_url('/v1/health'),headers={'Authorization':'Bearer '+self.config.token()}) as response:
                        self.assertEqual(response.status,200);health=await response.json();self.assertIn('character_selection_v1',health['capabilities'])
                    gate.set();response=await pending;self.assertEqual(response.status,200);await response.read();response.release()
                    async with client.post(server.make_url('/v1/manage'),headers={'Authorization':'Bearer '+self.config.token()},json={'action':'character.selector-records','args':{'persona_id':self.pid}}) as response:
                        body=await response.text();self.assertNotIn('encrypted_key',body);self.assertNotIn('model_snapshot',body)
            finally:gate.set();await server.close()

    async def test_first_member_policy_and_seal_model_snapshot(self):
        self.enable();p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        with patch.object(self.engine,'_schedule'):
            first=self.ingress.accept({'text':'书店','channel':'desktop','request_id':identifier()})
            self.config.update({'character_selector':{'name':'at-seal'}})
            b=self.ingress.get(first['batch_id']);self.ingress.action(b['id'],'seal',b['revision'],identifier(),self.pid)
        tid=self.ingress.get(first['batch_id'])['turn_id'];self.config.update({'character_selector':{'name':'after-seal'}})
        self.assertEqual(json.loads(self.raw(tid)['selector_model_snapshot'])['name'],'at-seal')
        self.assertEqual(json.loads(self.raw(tid)['context_policy_snapshot'])['character_retrieval']['mode'],'hybrid_llm')

    async def test_reference_failed_before_and_after_selection(self):
        from persona_runtime.references import ReferenceError
        for after in (False,True):
            self.enable();t=await self.turn('书店'+str(after));seen=0
            def references(conn,turn):
                nonlocal seen
                seen+=1
                if not after or seen>=3:raise ReferenceError('reference_unavailable',{'references':[]})
                return '',{'references':[]}
            with patch.object(self.rt,'local_pool',side_effect=self.pool),patch('persona_runtime.references.prepare',side_effect=references),patch.object(self.model,'complete',return_value=('{"schema_version":1,"lore_ids":[],"example_ids":[]}',{})) as call:
                await self.engine._run(t['id'])
            self.assertEqual(call.call_count,1 if after else 0)
            self.assertEqual(self.engine.get(t['id'])['attempt_count'],0)
            self.assertEqual(self.engine.get(t['id'])['error_code'],'reference_unavailable')

    async def test_selector_success_primary_manifest_failure_keeps_selector(self):
        self.enable();t=await self.turn('书店')
        with self.store.connection(write=True) as db:db.execute("CREATE TRIGGER fail_primary BEFORE INSERT ON context_manifests WHEN NEW.task_type='dialogue' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',return_value=('{"schema_version":1,"lore_ids":[],"example_ids":[]}',{'total_tokens':7})) as call:await self.engine._run(t['id'])
        self.assertEqual(call.call_count,1);self.assertEqual(self.engine.selector.list(self.pid)[0]['status'],'completed')
        self.assertEqual(self.engine.get(t['id'])['attempt_count'],0)
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT state FROM quota_reservations').fetchone()[0],'released')

    async def test_local_timeout_is_degradation_not_storage_failure(self):
        self.enable();t=await self.turn('书店')
        def slow(*args):time.sleep(1.1);return self.pool(*args)
        with patch.object(self.rt,'local_pool',side_effect=slow):result=await self.engine.selector.prepare(t['id'])
        self.assertEqual(result['degrade_reason'],'character_semantic_timeout');self.assertFalse(self.engine.selector.list(self.pid));self.assertFalse(self.model.calls)

# Only explicitly defined new tests run in SelectorTests; historical suite runs separately.
for name in dir(prior.IntegrationTests):
    if name.startswith('test_') and name not in SelectorTests.__dict__:setattr(SelectorTests,name,None)

class ProtocolTests(unittest.TestCase):
    def test_strict_output(self):
        pool={'payload':{'roots':{'lore':['a'],'example':['e']}}}
        valid='{"schema_version":1,"lore_ids":[],"example_ids":[]}'
        self.assertEqual(parse_output(valid,pool,2)['lore_ids'],[])
        for text in (valid+' trailing',valid.replace('1,','true,'),valid.replace('[],','["wrong"],'),valid.replace('[],','["a","a"],'),valid.replace('1,','1,"schema_version":1,'),valid.replace('1,','1,"other":0,')):
            with self.subTest(text=text),self.assertRaises(SemanticFailure):parse_output(text,pool,2)
