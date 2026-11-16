"""人物出口故障和呈现顺序；全部为离线人工回执。"""
import asyncio
import json
import sqlite3
import unittest

from persona_runtime.expression_persona_draft import PROFILE
from persona_runtime.expression_utterance_publication import SpeechPublication
from persona_runtime.models import identifier
from tests import test_m9r_native_retrieval as fixture


class Gate:
    def __init__(self):self.calls=[];self.failure=False;self.callback=None;self.started=asyncio.Event();self.block=None;self.content=None
    def raw(self,body='我去翻翻，等下跟你说'):
        return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'body':body,'segments':[body]},ensure_ascii=False)}}],
                           'usage':{'prompt_tokens':17,'completion_tokens':9,'total_tokens':26}},ensure_ascii=False).encode()
    async def complete(self,model,messages,*,save_raw):
        self.calls.append(messages);self.callback=save_raw;self.started.set()
        if self.block:await self.block.wait()
        if self.failure:
            from persona_runtime.chat import ModelError
            raise ModelError('provider_transport_interrupted',diagnostics={'request_headers_sent':True,'response_headers_received':False})
        save_raw(self.content or self.raw())
        return {'diagnostics':{}}


class Outlet(unittest.IsolatedAsyncioTestCase):
    decision_transport=fixture.Integration.decision_transport
    transport=fixture.Integration.transport

    async def asyncSetUp(self):
        fixture.Integration.setUp(self);fixture.Integration.enable(self)
        self.runtime=self.chat.expression;self.outlet=self.runtime.outlet;self.gate=Gate();self.runtime.client=self.gate
        self.pid=self.persona['id'];self.runtime.configure(self.pid,{'enabled':True,'protocol':PROFILE},0,identifier())
        self.pub=SpeechPublication(self.outlet)

    async def asyncTearDown(self):
        if self.gate.block:self.gate.block.set()
        for task in list(self.chat.tasks.values()):
            if not task.done():task.cancel()
        if getattr(self,'worker',None):
            self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        await fixture.Integration.asyncTearDown(self)

    async def start(self):
        turn=await self.chat.submit('desktop','USER_SENTINEL 查询公开攻略',identifier(),public_task={'question':'PUBLIC_SENTINEL公开攻略'})
        await asyncio.wait_for(self.gate.started.wait(),8)
        for _ in range(100):
            units=self.pub.list(self.pid,turn['session_id'],turn['id'])
            notices=[u for u in units if u['phase']=='search_notice']
            if notices and self.pub.plan(self.pid,turn['session_id'],notices[0]['id']):return turn,notices[0]['id']
            await asyncio.sleep(.02)
        self.fail('no notice plan')

    def present(self,turn,uid,state='presented'):
        plan=self.pub.plan(self.pid,turn['session_id'],uid);offers=[]
        for item in plan['items']:
            offered=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],plan['revision'],item['ordinal'],'fixture')
            self.pub.acknowledge(self.pid,turn['session_id'],uid,offered['id'],offered['lease_id'],'fixture',identifier(),offered['body_hash'],state)
            offers.append(offered)
        return offers

    async def test_notice_expression_ack_precedes_search_and_only_actual_body_flows_back(self):
        turn,uid=await self.start()
        self.assertFalse(self.provider.calls);self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        payload=json.dumps(self.gate.calls[0],ensure_ascii=False)
        self.assertEqual(len(self.gate.calls[0]),2)
        self.assertNotIn('USER_SENTINEL',payload);self.assertNotIn('PUBLIC_SENTINEL',payload)
        self.assertEqual(self.gate.calls[0][1]['content'],'等我翻翻看，这个我想查清楚再跟你说。')
        self.assertNotIn('body',json.dumps(self.pub.status(self.pid,turn['session_id'],uid)).replace('body_hash',''))
        self.assertTrue(all(i['delay_ms']==0 for i in self.pub.plan(self.pid,turn['session_id'],uid)['items']))
        self.present(turn,uid);await asyncio.wait_for(self.chat.wait(),10)
        result=self.chat.get(turn['id']);self.assertEqual(result['search_notice']['text'],'我去翻翻，等下跟你说')
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(len(self.main.notice_calls),1)
        self.assertIn('我去翻翻，等下跟你说',json.dumps(self.main.calls,ensure_ascii=False))
        self.assertNotIn('等我翻翻看，这个我想查清楚再跟你说。',json.dumps(self.main.calls,ensure_ascii=False))
        units=self.pub.list(self.pid,turn['session_id'],turn['id']);self.assertEqual({u['phase'] for u in units},{'search_notice','main'})
        main=next(u for u in units if u['phase']=='main');self.present(turn,main['id'])
        self.assertEqual(len(self.gate.calls),2)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM expression_speech_runs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM expression_runs').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],2)

    async def test_unknown_expression_is_not_replayed_and_late_result_cannot_replace_fallback(self):
        self.gate.failure=True;turn,uid=await self.start()
        plan=self.pub.plan(self.pid,turn['session_id'],uid);self.assertEqual(plan['producer_kind'],'draft')
        self.present(turn,uid);await asyncio.wait_for(self.chat.wait(),10)
        first=self.chat.get(turn['id'])['search_notice']['text']
        with self.store.connection() as db:run=dict(db.execute('SELECT * FROM expression_speech_runs WHERE utterance_id=?',(uid,)).fetchone())
        self.assertIsNone(run['usage_json'])
        with self.store.connection(write=True) as db:
            from hashlib import sha256
            raw=self.gate.raw('迟到新说法')
            db.execute('UPDATE expression_speech_runs SET raw_response=?,raw_hash=? WHERE id=?',(raw,sha256(raw).hexdigest(),run['id']))
        value=self.outlet.finalize(run['id']);self.assertTrue(value['late_evidence_only'])
        await self.outlet.execute(uid,self.cfg.load()['model'])
        self.assertEqual(self.chat.get(turn['id'])['search_notice']['text'],first)
        self.assertEqual(len(self.gate.calls),2)

    async def test_unknown_presentation_reuses_lease_without_reoffering_text_or_starting_search(self):
        turn,uid=await self.start();offered=self.present(turn,uid,'unknown')[0]
        plan=self.pub.plan(self.pid,turn['session_id'],uid)
        again=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],plan['revision'],0,'fixture')
        self.assertNotIn('body',again);self.assertFalse(self.provider.calls)
        pending=self.pub.pending_receipts(self.pid,turn['session_id'],uid,'fixture')
        self.assertEqual(pending[0]['lease_id'],offered['lease_id']);self.assertNotIn('text',pending[0])
        self.pub.acknowledge(self.pid,turn['session_id'],uid,offered['id'],offered['lease_id'],'fixture',identifier(),offered['body_hash'])
        await asyncio.wait_for(self.chat.wait(),10);self.assertEqual(len(self.provider.calls),1)

    async def test_cancel_before_offer_does_not_start_search_or_promote_hidden_source(self):
        turn,uid=await self.start();self.pub.cancel(uid)
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertFalse(self.provider.calls);self.assertIsNone(self.chat.get(turn['id'])['search_notice'])
        self.assertEqual(self.pub.visible(self.pid,turn['session_id'],uid),[])
        await self.outlet.execute(uid,self.cfg.load()['model']);self.assertEqual(len(self.main.notice_calls),1)

    async def test_receipt_conflict_scope_and_source_are_immutable(self):
        turn,uid=await self.start()
        with self.assertRaisesRegex(ValueError,'scope_mismatch'):self.pub.plan('other',turn['session_id'],uid)
        with self.store.connection(write=True) as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE expression_speech_units SET body='tamper' WHERE id=?",(uid,))
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE expression_speech_runs SET request_json='tamper' WHERE utterance_id=?",(uid,))
        plan=self.pub.plan(self.pid,turn['session_id'],uid);offer=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],1,0,'fixture')
        rid=identifier();args=(self.pid,turn['session_id'],uid,offer['id'],offer['lease_id'],'fixture',rid,offer['body_hash'])
        self.pub.acknowledge(*args);self.assertTrue(self.pub.acknowledge(*args)['already_applied'])
        with self.assertRaisesRegex(ValueError,'conflict'):self.pub.acknowledge(*args,state='unknown')
        await asyncio.wait_for(self.chat.wait(),10)

    async def test_disable_before_first_offer_creates_fallback_revision_without_another_call(self):
        turn,uid=await self.start();plan=self.pub.plan(self.pid,turn['session_id'],uid)
        self.runtime.configure(self.pid,{'enabled':False},self.runtime.policy(self.pid)['revision'],identifier())
        result=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],plan['revision'],0,'fixture')
        self.assertEqual(result['state'],'plan_changed')
        new=self.pub.plan(self.pid,turn['session_id'],uid);self.assertEqual(new['revision'],2);self.assertEqual(new['producer_kind'],'draft')
        self.present(turn,uid);await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(len(self.gate.calls),1)
        self.assertEqual(self.chat.get(turn['id'])['search_notice']['text'],'等我翻翻看，这个我想查清楚再跟你说。')

    async def test_withdrawn_human_source_blocks_presentation_without_fallback(self):
        turn,uid=await self.start()
        with self.store.connection(write=True) as db:
            event=db.execute('SELECT user_event_id FROM turns WHERE id=?',(turn['id'],)).fetchone()[0]
            from persona_runtime.models import timestamp,LOCAL_USER_ID
            db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(event,self.pid,LOCAL_USER_ID,'author withdrawal',timestamp()))
        plan=self.pub.plan(self.pid,turn['session_id'],uid)
        result=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],plan['revision'],0,'fixture')
        self.assertEqual(result['state'],'stopped');self.assertNotIn('body',result)
        await asyncio.wait_for(self.chat.wait(),10);self.assertFalse(self.provider.calls)

    async def test_partial_cancel_keeps_only_presented_prefix_and_no_search(self):
        self.gate.content=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'body':'先放着，我去查查','segments':['先放着，','我去查查']},ensure_ascii=False)}}]}).encode()
        turn,uid=await self.start();plan=self.pub.plan(self.pid,turn['session_id'],uid)
        offer=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],1,0,'fixture')
        self.pub.acknowledge(self.pid,turn['session_id'],uid,offer['id'],offer['lease_id'],'fixture',identifier(),offer['body_hash'])
        self.pub.cancel(uid);await asyncio.wait_for(self.chat.wait(),10)
        self.assertFalse(self.provider.calls);self.assertEqual(self.chat.get(turn['id'])['search_notice']['text'],'先放着，')
        self.assertEqual(self.pub.plan(self.pid,turn['session_id'],uid)['state'],'closed')

    async def test_restart_does_not_reoffer_outstanding_lease_or_call_model(self):
        turn,uid=await self.start();plan=self.pub.plan(self.pid,turn['session_id'],uid)
        offer=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],1,0,'fixture')
        self.outlet.recover()
        self.assertEqual(self.pub.pending_receipts(self.pid,turn['session_id'],uid,'fixture')[0]['state'],'unknown')
        again=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],1,0,'fixture');self.assertNotIn('body',again)
        self.assertEqual(len(self.gate.calls),1);self.assertFalse(self.provider.calls)
        self.pub.acknowledge(self.pid,turn['session_id'],uid,offer['id'],offer['lease_id'],'fixture',identifier(),offer['body_hash'])
        await asyncio.wait_for(self.chat.wait(),10)

    async def test_migration_projects_only_confirmed_prefix_and_keeps_raw_and_unknown_receipt(self):
        self.gate.content=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'body':'先放着，我去查查','segments':['先放着，','我去查查']},ensure_ascii=False)}}]}).encode()
        turn,uid=await self.start();plan=self.pub.plan(self.pid,turn['session_id'],uid)
        one=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],1,0,'fixture')
        self.pub.acknowledge(self.pid,turn['session_id'],uid,one['id'],one['lease_id'],'fixture',identifier(),one['body_hash'])
        two=self.pub.offer(self.pid,turn['session_id'],uid,plan['id'],1,1,'fixture')
        with self.store.connection(write=True) as db:
            before=db.execute('SELECT raw_response,raw_hash,usage_json FROM expression_speech_runs WHERE utterance_id=?',(uid,)).fetchone()
            from persona_runtime.expression_compat import pause
            pause(db)
            after=db.execute('SELECT raw_response,raw_hash,usage_json FROM expression_speech_runs WHERE utterance_id=?',(uid,)).fetchone()
            self.assertEqual(tuple(before),tuple(after))
            self.assertEqual(db.execute('SELECT state FROM expression_speech_items WHERE id=?',(two['id'],)).fetchone()[0],'unknown')
        await asyncio.wait_for(self.chat.wait(),10)
        self.assertEqual(self.chat.get(turn['id'])['search_notice']['text'],'先放着，');self.assertFalse(self.provider.calls)
        self.assertEqual(self.pub.plan(self.pid,turn['session_id'],uid)['state'],'closed')

    async def test_code_omission_uses_complete_source_and_never_publishes_commentary(self):
        original=self.main.complete
        async def source(model,messages):
            if '_search_notice_profile' in model:
                from persona_runtime.chat import ModelCompletion
                return ModelCompletion('先保留这段：\n```python\nprint(1)\n```\n我去查查',{'total_tokens':3},{'finish_reason':'stop'})
            return await original(model,messages)
        self.main.complete=source
        turn,uid=await self.start()
        plan=self.pub.plan(self.pid,turn['session_id'],uid);self.assertEqual(plan['producer_kind'],'draft')
        self.assertEqual(plan['reason'],'protected_text_changed')
        self.present(turn,uid);await asyncio.wait_for(self.chat.wait(),10)
        self.assertIn('```python\nprint(1)\n```',self.chat.get(turn['id'])['search_notice']['text'])

    async def test_minimal_profile_cannot_silently_use_unadapted_character_channel(self):
        from persona_runtime.expression_utterance import require_supported_channel
        with self.store.connection() as db:
            require_supported_channel(db,self.pid,'desktop')
            with self.assertRaisesRegex(ValueError,'channel_not_enabled'):require_supported_channel(db,self.pid,'qq')

    async def test_v2_management_is_admin_only_and_status_does_not_expose_source(self):
        from aiohttp import ClientSession
        from aiohttp.test_utils import TestServer
        from persona_runtime.server import create_app
        turn,uid=await self.start();app=create_app(self.paths,self.main);app['engine'].expression=self.runtime
        async with TestServer(app) as server,ClientSession() as client:
            args={'persona_id':self.pid,'session_id':turn['session_id'],'utterance_id':uid}
            for role,expected in (('bridge',403),('admin',200)):
                async with client.post(server.make_url('/v1/manage'),headers={'Authorization':'Bearer '+self.cfg.token(role)},json={'action':'expression.v2.status','args':args}) as response:
                    value=await response.json();self.assertEqual(response.status,expected,value)
                    if role=='admin':self.assertNotIn('等我翻翻',json.dumps(value,ensure_ascii=False))
        self.pub.cancel(uid);await asyncio.wait_for(self.chat.wait(),10)

    async def test_root_cancel_during_notice_translation_preserves_unknown_call_and_source(self):
        self.gate.block=asyncio.Event()
        turn=await self.chat.submit('desktop','查询',identifier(),public_task={'question':'公开攻略'})
        await asyncio.wait_for(self.gate.started.wait(),8)
        await self.chat.cancel(turn['id'])
        for _ in range(100):
            with self.store.connection() as db:
                run=dict(db.execute('SELECT * FROM expression_speech_runs').fetchone())
            if run['transport_json']:break
            await asyncio.sleep(.02)
        self.assertFalse(self.provider.calls);self.assertEqual(len(self.gate.calls),1)
        self.assertEqual(json.loads(run['transport_json'])['physical_outcome'],'unknown');self.assertIsNone(run['usage_json'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],2) # Jev与预告各有源回执。
            unit=dict(db.execute('SELECT * FROM expression_speech_units').fetchone());self.assertIsNotNone(unit['closed_at'])
        self.assertIsNone(self.chat.get(turn['id'])['search_notice'])

    async def test_full_migration_keeps_both_stages_receipts_and_pauses_new_units(self):
        from persona_runtime.migration import export_instance,import_instance
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        turn,uid=await self.start();self.present(turn,uid);await asyncio.wait_for(self.chat.wait(),10)
        main=next(u for u in self.pub.list(self.pid,turn['session_id'],turn['id']) if u['phase']=='main');self.present(turn,main['id'])
        self.org.draining=True;self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
        with self.store.connection() as db:
            units=[tuple(r) for r in db.execute('SELECT id,phase,body,source_id,body_hash FROM expression_speech_units ORDER BY rowid')]
            raw=[tuple(r) for r in db.execute('SELECT id,raw_response,raw_hash,usage_json FROM expression_speech_runs')]
        package=export_instance(self.paths,'r2-public-fixture.zip')
        target=RuntimePaths(self.paths.home/'new-instance');import_instance(target,package['path'],apply=True)
        copy=Store(target)
        with copy.connection() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,phase,body,source_id,body_hash FROM expression_speech_units ORDER BY rowid')],units)
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,raw_response,raw_hash,usage_json FROM expression_speech_runs')],raw)
            self.assertFalse(json.loads(db.execute('SELECT data_json FROM expression_runtime_policies').fetchone()[0])['enabled'])
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE role='persona'").fetchone()[0],2)


if __name__=='__main__':unittest.main()
