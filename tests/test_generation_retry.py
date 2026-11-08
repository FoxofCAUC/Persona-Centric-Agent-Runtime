import asyncio
from copy import deepcopy
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock,patch
from persona_runtime import generation_retry as retry
from persona_runtime.chat import ModelError
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.memory_policy import project_calls
from tests import test_m754r2 as fixture


class AuditedRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model={'name':'grok-4.7','base_url':'http://127.0.0.1:1/v1','max_tokens':512,'timeout':1}
        self.messages=[{'role':'user','content':'人工材料'}];self.saved=[]
        self.client=type('Client',(),{})();self.client.complete=AsyncMock()
    async def run_case(self,**kw):
        return await retry.audited_complete(self.client,self.model,self.messages,validate=lambda _:None,
            save=lambda value:self.saved.append(deepcopy(value)),before_attempt=lambda _:None,sleep=AsyncMock(),**kw)
    async def test_initial_plus_three_retries_same_request_success_last(self):
        self.client.complete.side_effect=[ModelError('provider_timeout')]*3+[('有效回复',{'total_tokens':7})]
        result=await self.run_case()
        self.assertEqual(self.client.complete.await_count,4)
        rows=result[1]['generation_retry']['attempts']
        self.assertEqual([r['index'] for r in rows],[0,1,2,3])
        self.assertEqual([r['status'] for r in rows],['failed']*3+['completed'])
        self.assertEqual(len({r['request_hash'] for r in rows}),1)
        self.assertEqual(result[1]['total_tokens'],7)
        self.assertEqual(len(self.saved),8)
    async def test_four_failures_abort_without_fifth_dispatch(self):
        self.client.complete.side_effect=ModelError('provider_empty_response')
        with self.assertRaises(retry.GenerationRetriesExhausted):await self.run_case()
        self.assertEqual(self.client.complete.await_count,4)
        self.assertEqual(self.saved[-1]['generation_retry']['attempts'][-1]['status'],'failed')
    async def test_old_failed_attempt_leaves_three_retry_slots(self):
        self.client.complete.side_effect=ModelError('provider_timeout')
        with self.assertRaises(retry.GenerationRetriesExhausted):await self.run_case(prior_failures=1)
        self.assertEqual(self.client.complete.await_count,3)
        self.assertEqual([r['index'] for r in self.saved[-1]['generation_retry']['attempts']],[1,2,3])
    async def test_authentication_and_non_provider_errors_never_retry(self):
        for code in ('provider_http_401','provider_http_400','provider_content_filtered','character_source_changed'):
            self.client.complete.reset_mock();self.client.complete.side_effect=ModelError(code)
            with self.assertRaisesRegex(ModelError,code):await self.run_case()
            self.assertEqual(self.client.complete.await_count,1)
    async def test_audit_write_failure_prevents_dispatch(self):
        with self.assertRaises(OSError):
            await retry.audited_complete(self.client,self.model,self.messages,validate=lambda _:None,
                save=lambda _:(_ for _ in ()).throw(OSError()),before_attempt=lambda _:None)
        self.client.complete.assert_not_awaited()
    async def test_cancellation_has_terminal_record_and_no_retry(self):
        entered=asyncio.Event()
        async def wait(*_):entered.set();await asyncio.Event().wait()
        self.client.complete.side_effect=wait
        task=asyncio.create_task(self.run_case());await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.client.complete.await_count,1)
        self.assertEqual(self.saved[-1]['generation_retry']['attempts'][0]['status'],'interrupted')
    async def test_timeout_cancels_old_task_before_retry(self):
        self.model['timeout']=.01;cancelled=[]
        async def wait(*_):
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:cancelled.append(True);raise
        self.client.complete.side_effect=wait
        with self.assertRaises(retry.GenerationRetriesExhausted):await self.run_case(timeout_code='selector_timeout')
        self.assertEqual(len(cancelled),4)
        self.assertEqual(self.saved[-1]['generation_retry']['attempts'][0]['error_code'],'selector_timeout')
    def test_quota_counts_retry_dispatches_at_their_own_times(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE attempts(started_epoch REAL,usage_json TEXT)')
            db.execute('INSERT INTO attempts VALUES(?,?)',(1,json.dumps({'generation_retry':{'attempts':[{'started_epoch':1},{'started_epoch':20}]}})))
            db.execute('INSERT INTO attempts VALUES(?,?)',(30,None))
            self.assertEqual(retry.call_count(db,'attempts',10),2)
            self.assertEqual(retry.call_count(db,'attempts',0),3)


class RuntimeRetry(fixture.SelectorTests):
    def setUp(self):
        super().setUp();p=patch.object(retry,'retry_delay',new=AsyncMock());p.start();self.addCleanup(p.stop)
    async def test_main_retry_is_audited_and_counts_against_quota(self):
        turn=await self.turn('书店')
        with patch.object(self.model,'complete',new=AsyncMock(side_effect=[ModelError('provider_timeout'),('主回复',{'total_tokens':5})])) as call:
            await self.engine._run(turn['id'])
        self.assertEqual(call.await_count,2);self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        with self.store.connection() as db:
            row=db.execute('SELECT usage_json FROM attempts WHERE turn_id=?',(turn['id'],)).fetchone()
            self.assertEqual(len(json.loads(row[0])['generation_retry']['attempts']),2)
            self.assertEqual(project_calls(db,0),2)
    async def test_selector_retries_then_main_runs_once(self):
        self.enable();turn=await self.turn('书店');counts={'selector':0,'main':0}
        async def complete(model,messages):
            kind='selector' if model.get('_character_selection') else 'main';counts[kind]+=1
            if kind=='selector':
                if counts[kind]<4:raise ModelError('provider_timeout')
                return '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}',{'total_tokens':3}
            return '主回复',{'total_tokens':5}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=complete):
            await self.engine._run(turn['id'])
        self.assertEqual(counts,{'selector':4,'main':1});self.assertEqual(self.engine.get(turn['id'])['status'],'completed')
        with self.store.connection() as db:self.assertEqual(project_calls(db,0),5)
    async def test_selector_exhaustion_aborts_without_lexical_main(self):
        self.enable();turn=await self.turn('书店')
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',new=AsyncMock(side_effect=ModelError('provider_timeout'))) as call:
            await self.engine._run(turn['id'])
        self.assertEqual(call.await_count,4);self.assertEqual(self.engine.get(turn['id'])['error_code'],'provider_retries_exhausted')
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
    async def test_manual_main_retries_ignore_automatic_call_quota(self):
        turn=await self.turn('书店');self.config.update({'model':{'hourly_limit':1}})
        with patch.object(self.model,'complete',new=AsyncMock(side_effect=ModelError('provider_timeout'))) as call:await self.engine._run(turn['id'])
        self.assertEqual(call.await_count,4);self.assertEqual(self.engine.get(turn['id'])['error_code'],'provider_retries_exhausted')

    async def test_exhausted_automatic_selector_retry_does_not_quota_block_manual_main(self):
        self.enable();self.config.update({'model':{'hourly_limit':1}});turn=await self.turn('书店');counts={'selector':0,'main':0}
        async def complete(model,messages):
            kind='selector' if model.get('_character_selection') else 'main';counts[kind]+=1
            if kind=='selector':raise ModelError('provider_timeout')
            return '主回复',{'total_tokens':1}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=complete):await self.engine._run(turn['id'])
        self.assertEqual(counts,{'selector':1,'main':1});self.assertEqual(self.engine.get(turn['id'])['status'],'completed')


for name in dir(fixture.SelectorTests):
    if name.startswith('test_') and name not in RuntimeRetry.__dict__:setattr(RuntimeRetry,name,None)


if __name__=='__main__':unittest.main()
