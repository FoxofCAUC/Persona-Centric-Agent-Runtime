"""Isolated rolling deadline and source-preserving display contracts."""
import asyncio
import json
import unittest
from unittest.mock import patch

from tests import test_m61 as fixtures
from persona_runtime.chat_display import plan, split_chat, enabled
from persona_runtime.delivery import DeliveryService
from persona_runtime.models import identifier, RuntimeErrorBase
from persona_runtime.reply_batch import ReplySchemaError


class DisplayTests(unittest.TestCase):
    def test_sample(self):
        text='晚上好。看得到，消息很清楚。\n\n你今晚还忙着吗？'
        rows=plan([{'text':text,'content_kind':'chat'}])
        self.assertEqual([r['text'] for r in rows],['晚上好','看得到','消息很清楚','你今晚还忙着吗'])
        self.assertEqual([r['wait_ms'] for r in rows],[0,645,645,675])
        self.assertTrue(all(r['source']['semantic_sha256']==rows[0]['source']['semantic_sha256'] for r in rows))

    def test_protected_data_and_idempotence(self):
        fixtures=['价格3.14，版本v1.2.3。','网址https://example.org/a?q=1,2。','邮箱a.b@example.com。',
            '文件D:\\folder\\a.b.py。','他说“你好，世界。”，我收到了。','(T_T)，好吧。', '日期2026-10-30，好的。']
        protected=['3.14','v1.2.3','https://example.org/a?q=1,2','a.b@example.com','D:\\folder\\a.b.py','“你好，世界。”','(T_T)','2026-10-30']
        outputs=[]
        for text in fixtures:
            rows=plan([{'text':text}]);outputs.extend(r['text'] for r in rows)
            for row in rows:self.assertEqual([p[0] for p in split_chat(row['text'])],[row['text']])
        for value in protected:self.assertIn(value,'\n'.join(outputs))

    def test_code_quote_technical_unchanged(self):
        for kind,text in [('code','def f(x):\n    return x**2 + 3.14'),('quote','原话是“你好，世界。”'),('technical','参数x=1.2，不要改成12。'),('chat','代码如下\ndef f(x):\n    return x**2')]:
            self.assertEqual([r['text'] for r in plan([{'text':text,'content_kind':kind}])],[text])

    def test_limit_atomic_empty_and_long(self):
        with self.assertRaisesRegex(ReplySchemaError,'message_limit'):plan([{'text':'好。'*13}])
        with self.assertRaisesRegex(ReplySchemaError,'empty'):plan([{'text':'？！'}])
        row=plan([{'text':'长'*100}])[0]
        self.assertEqual(row['text'],'长'*100);self.assertTrue(row['diagnostics']['not_further_split'])

    def test_old_contract_and_invalid(self):
        self.assertFalse(enabled(None,'natural_structured'))
        self.assertFalse(enabled({'version':1},'natural_structured'))
        self.assertTrue(enabled({'version':2},'natural_structured'))
        with self.assertRaises(ReplySchemaError):enabled({'version':999},'natural_structured')


class RollingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.IngressTests.asyncSetUp(self)
        from persona_runtime.reply_batch import expression_snapshot
        with self.store.connection(write=True) as conn:
            for r in conn.execute('SELECT id,reply_mode,data_json FROM interaction_policies').fetchall():
                data=json.loads(r['data_json']);data['expression']=expression_snapshot(r['reply_mode'],r['id'],version=2)
                conn.execute('UPDATE interaction_policies SET data_json=? WHERE id=?',(json.dumps(data),r['id']))
    finish=fixtures.IngressTests.finish
    accept=fixtures.IngressTests.accept
    batch=fixtures.IngressTests.batch
    seal=fixtures.IngressTests.seal

    def rolling(self,channel='desktop',reply='legacy_full'):
        p=self.ingress.current_policy(self.pid,channel)
        return self.ingress.update_policy(self.pid,channel,'natural_batch',p['id'],identifier(),reply_mode=reply,conversation_policy_version=2)

    async def test_archived_upgrade_keeps_trash_and_canon(self):
        from persona_runtime.models import LOCAL_USER_ID
        p=self.ingress.current_policy(self.pid,'desktop')
        with self.assertRaisesRegex(RuntimeErrorBase,'archived_policy_maintenance_only'):
            self.ingress.upgrade_archived_policy(self.pid,'desktop',p['id'],identifier())
        with self.store.connection(write=True) as c:
            before=dict(c.execute('SELECT * FROM personas WHERE id=?',(self.pid,)).fetchone())
            c.execute('INSERT INTO persona_lifecycle(persona_id,user_id,deleted_at) VALUES (?,?,?) ON CONFLICT(persona_id) DO UPDATE SET deleted_at=excluded.deleted_at',(self.pid,LOCAL_USER_ID,'2026-01-01'))
            from persona_runtime.ingress import PARAMS
            data=json.loads(p['data_json']);data.update(PARAMS);data['expression']['version']=1
            c.execute("UPDATE interaction_policies SET mode='natural_batch',data_json=? WHERE id=?",(json.dumps(data),p['id']))
        op=identifier();new=self.ingress.upgrade_archived_policy(self.pid,'desktop',p['id'],op)
        self.assertEqual(new['buffer_policy']['schema_version'],2)
        self.assertEqual(new,self.ingress.upgrade_archived_policy(self.pid,'desktop',p['id'],op))
        with self.store.connection() as c:
            self.assertEqual(dict(c.execute('SELECT * FROM personas WHERE id=?',(self.pid,)).fetchone()),before)
            self.assertEqual(c.execute('SELECT deleted_at FROM persona_lifecycle WHERE persona_id=?',(self.pid,)).fetchone()[0],'2026-01-01')
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_in_trash'):self.accept('blocked')

    async def test_rolling_21_seconds_once(self):
        self.rolling();receipts=[]
        for t in (0,4,8,12,16):
            self.now=t;receipts.append(self.accept(str(t)));self.ingress.tick()
            self.assertEqual(self.fake.messages,[])
        self.assertEqual(len({r['batch_id'] for r in receipts}),1)
        self.now=20.999;self.ingress.tick();self.assertEqual(self.fake.messages,[])
        self.now=21;self.ingress.tick();await self.engine.wait()
        self.assertEqual(len(self.fake.messages),1)
        self.assertEqual([m['text'] for m in self.batch(receipts[0])['members']],['0','4','8','12','16'])

    async def test_minute_duplicates_and_wall_clock(self):
        self.rolling();first=None
        for t in range(0,61,4):
            self.now=t;self.epoch-=1000;r=self.accept(str(t),rid=str(t));first=first or r
            self.ingress.tick();self.assertEqual(self.fake.messages,[])
        self.now=64;self.accept('60',rid='60')
        self.assertEqual(self.batch(first)['remaining_ms'],1000)
        self.now=65;self.ingress.tick();await self.engine.wait();self.assertEqual(len(self.fake.messages),1)

    async def test_boundary_and_policy_change(self):
        self.rolling();a=self.accept('a');self.now=4.999;b=self.accept('b')
        self.assertEqual(a['batch_id'],b['batch_id'])
        self.now=9.999;c=self.accept('c');self.assertNotEqual(a['batch_id'],c['batch_id'])
        self.seal(c);await self.engine.wait();self.assertEqual(len(self.fake.messages),2)

    async def test_upgrade_same_mode_requires_resolution_and_new_version(self):
        p=self.ingress.current_policy(self.pid,'desktop')
        with self.store.connection(write=True) as conn:
            from persona_runtime.ingress import PARAMS
            data=json.loads(p['data_json']);data.update(PARAMS);data['expression']['version']=1
            conn.execute('UPDATE interaction_policies SET data_json=? WHERE id=?',(json.dumps(data),p['id']))
        p=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
        a=self.accept('a')
        with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):
            self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),conversation_policy_version=2)
        new=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),conversation_policy_version=2,pending_action='seal')
        self.assertNotEqual(new['id'],p['id'])
        self.assertEqual(self.batch(a)['policy']['max_wait_ms'],15000)
        await self.engine.wait()

    async def natural_reply(self,channel='desktop',body=None):
        self.rolling(channel,'natural_structured')
        raw=json.dumps({'schema_version':2,'behavior_intent':'respond','messages':body or [
            {'content':'晚上好。看得到，消息很清楚。\n\n你今晚还忙着吗？','content_kind':'chat','cancel_if_new_input':False}]},ensure_ascii=False)
        async def complete(model,messages):
            self.fake.messages.append(messages);return raw,{'total_tokens':1}
        self.fake.complete=complete
        metadata={}
        if channel=='qq':
            self.config.update({'bot_qq':'10001','owner_qq':'10002'})
            metadata={'channel':'qq','bot':'10001','sender':'10002','platform_message_id':identifier()}
        r=self.accept('晚上好，能看到我的消息吗',**metadata)
        self.seal(r);await self.engine.wait()
        return self.engine.get(self.batch(r)['turn_id'])

    async def test_desktop_saved_display_and_unchanged_original(self):
        t=await self.natural_reply();self.assertEqual(t['status'],'completed',t['error_code'])
        self.assertEqual(len(t['display_messages']),4);self.assertIn('。',t['reply'])
        self.assertNotIn('。',t['display_reply']);self.assertEqual(t['attempt_count'],1)
        with self.store.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM reply_delivery_parts').fetchone()[0],0)
        from pathlib import Path
        export=json.loads(Path(self.service.export_persona(self.pid)['path']).read_text(encoding='utf-8'))
        self.assertEqual(export['format_version'],15);self.assertEqual(len(export['reply_display_messages']),4)

    async def test_qq_display_claim_wait_and_restart(self):
        t=await self.natural_reply('qq');self.assertEqual(t['status'],'completed',t['error_code'])
        delivery=DeliveryService(self.store,lambda:self.now)
        view=delivery.list(self.pid)[0];self.assertEqual(len(view['parts']),4)
        capabilities=['reply_batch_v2','reply_rhythm_v1','reply_display_messages_v1']
        first=view['parts'][0]
        with self.assertRaisesRegex(RuntimeErrorBase,'display_capability'):
            delivery.claim(view['id'],first['id'],view['revision'],identifier(),capabilities=capabilities[:2])
        a=delivery.claim(view['id'],first['id'],view['revision'],identifier(),capabilities=capabilities)
        duplicate=delivery.claim(view['id'],first['id'],a['revision'],identifier(),capabilities=capabilities)
        self.assertFalse(duplicate['claimed'])
        delivery.result(view['id'],first['id'],a['claim_token'],'sent',a['part_revision'],identifier(),platform_message_id='1')
        view=delivery.list(self.pid)[0];second=view['parts'][1]
        b=delivery.claim(view['id'],second['id'],view['revision'],identifier(),capabilities=capabilities)
        self.assertEqual(b['reason'],'rhythm_wait');self.assertEqual(b['remaining_ms'],645)
        self.now+=.645;view=delivery.list(self.pid)[0]
        b=delivery.claim(view['id'],second['id'],view['revision'],identifier(),capabilities=capabilities)
        self.assertTrue(b['claimed']);delivery.recover()
        view=delivery.list(self.pid)[0]
        self.assertEqual([p['state'] for p in view['parts']],['sent','uncertain','pending','pending'])

    async def test_optional_semantic_group_cancellation(self):
        t=await self.natural_reply('qq',[
            {'content':'答案在这里','content_kind':'chat','cancel_if_new_input':False},
            {'content':'聊得开心。下次再聊。','content_kind':'chat','cancel_if_new_input':True}])
        d=DeliveryService(self.store);v=d.list(self.pid)[0]
        self.assertEqual(len(v['parts']),3)
        result=d.cancel_optional(self.pid,t['reply_batch']['id'],v['revision'],identifier())
        self.assertEqual([p['state'] for p in d.list(self.pid)[0]['parts']],['pending','skipped','skipped'])

    async def test_overflow_sends_nothing_and_charges_once(self):
        t=await self.natural_reply('qq',[{'content':'好。'*13,'content_kind':'chat','cancel_if_new_input':False}])
        self.assertEqual(t['error_code'],'chat_display_message_limit')
        self.assertEqual(t['attempt_count'],1)
        with self.store.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM reply_delivery_parts').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM reply_batches').fetchone()[0],0)

    async def test_started_optional_group_cannot_be_partly_cancelled(self):
        t=await self.natural_reply('qq',[
            {'content':'答案在这里','content_kind':'chat','cancel_if_new_input':False},
            {'content':'聊得开心。下次再聊。','content_kind':'chat','cancel_if_new_input':True}])
        d=DeliveryService(self.store,lambda:self.now)
        caps=['reply_batch_v2','reply_rhythm_v1','reply_display_messages_v1']
        v=d.list(self.pid)[0]
        a=d.claim(v['id'],v['parts'][0]['id'],v['revision'],identifier(),capabilities=caps)
        d.result(v['id'],v['parts'][0]['id'],a['claim_token'],'sent',a['part_revision'],identifier(),platform_message_id='one')
        self.now+=2;v=d.list(self.pid)[0]
        a=d.claim(v['id'],v['parts'][1]['id'],v['revision'],identifier(),capabilities=caps)
        self.assertTrue(a['claimed'])
        v=d.list(self.pid)[0];d.cancel_optional(self.pid,t['reply_batch']['id'],v['revision'],identifier())
        self.assertEqual([p['state'] for p in d.list(self.pid)[0]['parts']],['sent','sending','pending'])

    async def test_display_persist_failure_no_partial_send(self):
        with patch('persona_runtime.chat_display.persist',side_effect=OSError('disk failure')):
            t=await self.natural_reply('qq')
        self.assertEqual(t['status'],'failed');self.assertEqual(t['attempt_count'],1)
        with self.store.connection() as c:
            for table in ('reply_display_messages','reply_delivery_parts','reply_batches'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    async def test_preview_no_side_effect(self):
        from persona_runtime.reply_service import ReplyService
        raw=json.dumps({'schema_version':2,'behavior_intent':'respond','messages':[
            {'content':'晚上好。看到了。','content_kind':'chat','cancel_if_new_input':False}]})
        with self.store.connection() as c:before=c.total_changes
        result=ReplyService(self.store).preview(self.pid,raw=raw,presentation_version='chat-sentences-v1')
        self.assertEqual([r['text'] for r in result['display_messages']],['晚上好','看到了'])
        with self.store.connection() as c:
            for table in ('events','turns','reply_display_messages','attempts'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    async def test_bridge_receipt_returns_before_generation(self):
        from persona_runtime.qq_bridge import QQBridge
        from unittest.mock import AsyncMock
        bridge=QQBridge(self.paths); gate=asyncio.Event()
        payload={'channel':'qq','text':'a','request_id':'test-only','sender':'10002','bot':'10001','platform_message_id':'m'}
        bridge.request=AsyncMock(side_effect=[{'capabilities':['durable_ingress_v2']},
            {'receipt_id':'r','batch_id':'b','turn_id':None}])
        async def follow(turn,send):await gate.wait()
        bridge._follow_submission=follow
        with patch('persona_runtime.qq_bridge.allowed_message',return_value=payload):
            self.assertTrue(await asyncio.wait_for(bridge.handle(object(),AsyncMock(),background=True),.5))
        self.assertEqual(len(bridge._followers),1)
        await bridge.close();self.assertEqual(bridge._followers,{})


if __name__=='__main__':unittest.main()
