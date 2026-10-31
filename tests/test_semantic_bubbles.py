"""Artificial fixtures; no private source, cloud calls or formal runtime writes."""
import json
import sqlite3
import unittest
from dataclasses import asdict
from unittest.mock import patch

from tests import test_m61 as fixtures
from persona_runtime.reply_batch import expression_snapshot,validate_reply,ReplySchemaError,prompt_contract
from persona_runtime.reply_protocol import response_format,descriptor,validation_failure
from persona_runtime.chat_display import plan
from persona_runtime.models import identifier,RuntimeErrorBase
from persona_runtime.delivery import DeliveryService

EXPRESSION=expression_snapshot('natural_structured',version=3)


def raw(texts, **changes):
    value={'schema_version':3,'behavior_intent':'respond','messages':[
        {'content':text,'content_kind':'chat','cancel_if_new_input':False} for text in texts]}
    value.update(changes)
    return json.dumps(value,ensure_ascii=False)


# Frozen 32 independent presentation cases; no model scores are involved.
CASES=[
 ('comma','雨还在下，等小一点再走','chat','雨还在下，等小一点再走'),
 ('period','那就明天见。','chat','那就明天见'),
 ('ascii_period','okay.','chat','okay'),
 ('question','你到家了吗？','chat','你到家了吗？'),
 ('exclamation','终于修好了！','chat','终于修好了！'),
 ('ellipsis','让我想想……','chat','让我想想……'),
 ('ascii_ellipsis','嗯...','chat','嗯...'),
 ('wave','好呀～','chat','好呀～'),
 ('list_comma','杯子、书和灯就够了','chat','杯子、书和灯就够了'),
 ('short','嗯','chat','嗯'),
 ('single_question','？','chat','？'),
 ('decimal','价格3.14','chat','价格3.14'),
 ('version','现在是v1.2.3','chat','现在是v1.2.3'),
 ('url','https://example.org/a?q=1,2','chat','https://example.org/a?q=1,2'),
 ('email','a.b@example.org','chat','a.b@example.org'),
 ('path',r'D:\folder\main.py','chat',r'D:\folder\main.py'),
 ('quote','他说“别着急。”','chat','他说“别着急。”'),
 ('ascii_quote','他说"等一下."','chat','他说"等一下."'),
 ('parentheses','可以（如果你方便）','chat','可以（如果你方便）'),
 ('emoticon','(T_T)','chat','(T_T)'),
 ('code','def f(x):\n    return x**2 + 3.14','code','def f(x):\n    return x**2 + 3.14'),
 ('misclassified_code','def f(x):\n    return x**2','chat','def f(x):\n    return x**2'),
 ('technical','参数是1.2，不能写成12。','technical','参数是1.2，不能写成12。'),
 ('literal','原话：今天很好。','quote','原话：今天很好。'),
 ('newline','收到\n\n稍后联系','chat','收到\n\n稍后联系'),
 ('inner_period','我到了。你在哪？','chat','我到了。你在哪？'),
 ('long','长'*80,'chat','长'*80),
 ('date','约在2026-10-30','chat','约在2026-10-30'),
 ('markdown','**收到了**','chat','收到了'),
 ('link','[资料](https://example.org)','chat','资料（https://example.org）'),
 ('mixed','代码如下\nx = 1.2','chat','代码如下\nx = 1.2'),
 ('colon','带上这些：水和钥匙','chat','带上这些：水和钥匙'),
]


class BubbleTests(unittest.TestCase):
    def test_corrupt_or_wrong_mode_snapshot_is_not_legacy(self):
        for expression in (EXPRESSION|{'version':4},EXPRESSION|{'max_messages':3},
                           expression_snapshot('legacy_full',version=3)):
            for action in (lambda:validate_reply(raw(['好']), 'natural_structured',expression),
                           lambda:response_format('natural_structured',expression),
                           lambda:prompt_contract('natural_structured',expression)):
                with self.assertRaises(ReplySchemaError):action()
        self.assertEqual(prompt_contract('natural_structured',json.dumps(EXPRESSION)),
                         prompt_contract('natural_structured',EXPRESSION))

    def test_32_frozen_cases(self):
        self.assertEqual(len(CASES),32)
        for name,text,kind,expected in CASES:
            with self.subTest(name=name):
                rows=plan([{'text':text,'content_kind':kind}],EXPRESSION)
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['text'],expected)
                self.assertEqual(plan([{'text':rows[0]['text'],'content_kind':kind}],EXPRESSION)[0]['text'],expected)

    def test_six_bubbles_no_resegmentation(self):
        texts=['椅子朝侧面放，光就不会直晃眼','桌面留空一点','杯子放右侧','电线从后面绕','先用现成的垫子','如果还不舒服，再换椅子']
        batch=validate_reply(raw(texts),'natural_structured',EXPRESSION)
        rows=plan([asdict(s) for s in batch.segments],EXPRESSION)
        self.assertEqual([r['text'] for r in rows],texts)
        self.assertEqual([r['semantic_index'] for r in rows],list(range(1,7)))

    def test_counts_old_new_and_wire(self):
        for n in (1,3,6,12):self.assertEqual(len(validate_reply(raw(['好']*n),'natural_structured',EXPRESSION).segments),n)
        with self.assertRaises(ReplySchemaError):validate_reply(raw(['好']*13),'natural_structured',EXPRESSION)
        with self.assertRaises(ReplySchemaError):validate_reply(raw(['好']*4).replace('"schema_version": 3','"schema_version": 2'),'natural_structured')
        self.assertEqual(response_format('natural_structured',EXPRESSION)['json_schema']['schema']['properties']['messages']['maxItems'],12)
        self.assertEqual(response_format('context_structured',expression_snapshot('context_structured',version=3))['json_schema']['schema']['properties']['segments']['maxItems'],3)
        self.assertNotEqual(descriptor('natural_structured'),descriptor('natural_structured',EXPRESSION))
        with self.assertRaises(ReplySchemaError):response_format('natural_structured',{'version':3})

    def test_fallback_preserves_boundaries_and_disables_cancel(self):
        value=json.loads(raw(['答案','补充','结束']))
        value['messages'][1]['cancel_if_new_input']='yes'
        batch=validate_reply(json.dumps(value),'natural_structured',EXPRESSION)
        self.assertEqual([s.text for s in batch.segments],['答案','补充','结束'])
        self.assertTrue(batch.fallback_reason);self.assertTrue(all(not s.cancel_if_new_input for s in batch.segments))

    def test_bad_contract_no_partial_output(self):
        for value in [raw(['好']*13),raw(['好','']),raw(['好'])[:-1],raw(['好']).replace('"schema_version": 3','"schema_version": 3, "schema_version":3'),raw(['好'],secret='x')]:
            with self.subTest(value=value):
                with self.assertRaises(ReplySchemaError):validate_reply(value,'natural_structured',EXPRESSION)
        self.assertEqual(validation_failure(raw(['好']*13),'natural_structured',EXPRESSION)['reason'],'message_count')

    def test_diagnostics_no_semantic_claim_and_prompt(self):
        row=plan([{'text':'字'*60}],EXPRESSION)[0]
        self.assertEqual(row['diagnostics']['quality_findings'],['long_bubble_review'])
        self.assertIn('合理逗号',prompt_contract('natural_structured',EXPRESSION))
        self.assertNotIn('不写逗号',prompt_contract('natural_structured',EXPRESSION))
        self.assertIn('不写逗号',prompt_contract('natural_structured',expression_snapshot('natural_structured',version=2)))


class BubbleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.IngressTests.asyncSetUp
    finish=fixtures.IngressTests.finish
    accept=fixtures.IngressTests.accept
    batch=fixtures.IngressTests.batch
    seal=fixtures.IngressTests.seal

    def policy(self,channel='desktop'):
        p=self.ingress.current_policy(self.pid,channel)
        return self.ingress.update_policy(self.pid,channel,'natural_batch',p['id'],identifier(),
            reply_mode='natural_structured',expression_policy_version=3)

    def fake_reply(self,texts):
        self.models=[]
        async def complete(model,messages):
            self.models.append(model);self.fake.messages.append(messages)
            return raw(texts),{'total_tokens':1}
        self.fake.complete=complete

    async def test_six_persist_manifest_preview_export(self):
        self.policy();self.fake_reply(['你好，收到了']*6)
        receipt=self.accept('周末怎么安排');self.seal(receipt);await self.engine.wait()
        t=self.engine.get(self.batch(receipt)['turn_id']);self.assertEqual(t['status'],'completed',t['error_code'])
        self.assertEqual(len(t['display_messages']),6);self.assertEqual(t['attempt_count'],1)
        with self.store.connection() as c:
            manifest=json.loads(c.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(t['id'],)).fetchone()[0])
            self.assertEqual(manifest['reply_output_contract'],descriptor('natural_structured',self.models[0]['_expression_snapshot']))
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],19)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
        from persona_runtime.reply_service import ReplyService
        preview=ReplyService(self.store).preview(self.pid,batch_id=t['reply_batch']['id'])
        self.assertEqual([r['text'] for r in preview['display_messages']],['你好，收到了']*6)
        from pathlib import Path
        exported=json.loads(Path(self.service.export_persona(self.pid)['path']).read_text(encoding='utf-8'))
        self.assertEqual(exported['format_version'],16)

    async def test_policy_preserved_pending_and_retry(self):
        p=self.policy();a=self.accept('收到吗')
        kept=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),conversation_policy_version=2)
        self.assertEqual(kept['id'],p['id']);self.assertEqual(kept['expression_policy']['version'],3)
        self.fake_reply(['收到']);self.seal(a);await self.engine.wait()
        self.assertEqual(json.loads(self.models[0]['_expression_snapshot'])['version'],3)

    async def test_qq_capability_and_no_duplicate_claim(self):
        self.config.update({'bot_qq':'10001','owner_qq':'10002'});self.policy('qq');self.fake_reply(['好，明白']*6)
        a=self.accept('开始吧',channel='qq',bot='10001',sender='10002',platform_message_id=identifier())
        self.seal(a);await self.engine.wait();t=self.engine.get(self.batch(a)['turn_id'])
        self.assertEqual(t['status'],'completed',t['error_code'])
        d=DeliveryService(self.store,lambda:self.now);v=d.list(self.pid)[0];part=v['parts'][0]
        caps=['reply_batch_v2','reply_rhythm_v1','reply_display_messages_v1']
        with self.assertRaises(RuntimeErrorBase):d.claim(v['id'],part['id'],v['revision'],identifier(),capabilities=caps)
        caps+=['reply_bubbles_v3'];claimed=d.claim(v['id'],part['id'],v['revision'],identifier(),capabilities=caps)
        self.assertTrue(claimed['claimed'])
        v=d.list(self.pid)[0]
        again=d.claim(v['id'],part['id'],v['revision'],identifier(),capabilities=caps)
        self.assertFalse(again['claimed'])

    async def test_manifest_failure_zero_call(self):
        self.policy();self.fake_reply(['好'])
        with patch.object(self.engine.cognitive,'persist',side_effect=sqlite3.OperationalError('simulated disk')):
            a=self.accept('开始');self.seal(a);await self.engine.wait()
        self.assertEqual(self.fake.messages,[])

    async def test_old_batch_frozen_and_new_policy_requires_resolution(self):
        with self.store.connection(write=True) as c:
            for row in c.execute('SELECT id,reply_mode,data_json FROM interaction_policies').fetchall():
                value=json.loads(row['data_json']);value['expression']=expression_snapshot(row['reply_mode'],row['id'],version=2)
                c.execute('UPDATE interaction_policies SET data_json=? WHERE id=?',(json.dumps(value),row['id']))
        p=self.ingress.current_policy(self.pid,'desktop')
        p=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),reply_mode='natural_structured')
        a=self.accept('先收到这条')
        with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):
            self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),expression_policy_version=3)
        async def complete(model,messages):
            version=json.loads(model['_expression_snapshot'])['version']
            return raw(['收到']).replace('"schema_version": 3','"schema_version": '+str(version)),{}
        self.fake.complete=complete
        new=self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),expression_policy_version=3,pending_action='seal')
        await self.engine.wait()
        self.assertEqual(self.batch(a)['policy']['expression']['version'],2)
        self.assertEqual(new['expression_policy']['version'],3)
        old=self.engine.get(self.batch(a)['turn_id']);self.assertEqual(old['reply_batch']['schema_version'],2)

    async def test_new_optional_bubble_cancel_keeps_required_and_history(self):
        self.config.update({'bot_qq':'10001','owner_qq':'10002'});self.policy('qq')
        value=json.loads(raw(['必需的第一项','必需的第二项','下次聊']))
        value['messages'][2]['cancel_if_new_input']=True
        async def complete(model,messages):return json.dumps(value),{}
        self.fake.complete=complete
        kw={'channel':'qq','bot':'10001','sender':'10002'}
        a=self.accept('先给建议',platform_message_id=identifier(),**kw);self.seal(a);await self.engine.wait()
        t=self.engine.get(self.batch(a)['turn_id'])
        self.accept('结束语不用了',platform_message_id=identifier(),**kw)
        d=DeliveryService(self.store);v=d.list(self.pid)[0]
        self.assertEqual([p['state'] for p in v['parts']],['pending','pending','skipped'])
        self.assertEqual(self.engine.get(t['id'])['reply'],t['reply'])


from tests import test_reply_protocol as wire_fixtures


class BubbleWireTests(unittest.IsolatedAsyncioTestCase):
    cleanup_engine=wire_fixtures.ProtocolIntegrationTests.cleanup_engine
    submit=wire_fixtures.ProtocolIntegrationTests.submit
    manifest=wire_fixtures.ProtocolIntegrationTests.manifest

    async def asyncSetUp(self):
        await wire_fixtures.ProtocolIntegrationTests.asyncSetUp(self)
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),expression_policy_version=3)
        self.raw=raw(['下雨了，带把伞','晚点再出门','杯子放桌边','文件已经收好','不用着急','到了再说'])

    async def test_actual_http_schema_prompt_manifest_agree(self):
        t=await self.submit();self.assertEqual(t['status'],'completed',t['error_code'])
        self.assertEqual(len(self.requests),1);self.assertEqual(len(t['display_messages']),6)
        with self.store.connection() as c:
            expression=c.execute('SELECT expression_snapshot FROM turns WHERE id=?',(t['id'],)).fetchone()[0]
        self.assertEqual(self.requests[0]['response_format'],response_format('natural_structured',expression))
        self.assertEqual(self.manifest(t['id'])['reply_output_contract'],descriptor('natural_structured',expression))
        self.assertIn('合理逗号',self.requests[0]['messages'][0]['content'])


if __name__=='__main__':unittest.main()
