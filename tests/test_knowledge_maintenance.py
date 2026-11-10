"""Public-source changes run through product coordination and shared managed work."""

import copy
from datetime import datetime, timedelta, timezone
import json
import time
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ModelError
from persona_runtime.knowledge_fetch import FetchError, WikimediaFetcher, public_identifier
from persona_runtime.knowledge_output import parse
from persona_runtime.knowledge_store import eligible, search
from persona_runtime.models import RuntimeErrorBase, identifier
from tests import test_m4 as fixtures


class Source:
    def __init__(self):
        self.values = {}
        self.calls = []

    def fetch(self, source, public_id):
        self.calls.append((source['id'], public_id))
        self.last_interactive = source.get('interactive',False)
        value = self.values[(source['id'], public_id)]
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    def put(self, public_id='测试行星', *, value='蓝色', revision=1, source='wikipedia_zh', body=None):
        text = body or public_id + '的颜色是' + value + '。'
        self.values[(source, public_id)] = {'upstream_id': public_id, 'revision': revision,
            'title': '测试行星', 'body': text, 'upstream_at': '2026-11-10T00:00:00+00:00',
            'url': ('https://zh.wikipedia.org/wiki/' if source == 'wikipedia_zh'
                    else 'https://www.wikidata.org/wiki/') + public_id,
            'metadata': {'parser': 'artificial-v1'}, 'raw': {'artificial': text}}


class Processor:
    def __init__(self):
        self.calls = []
        self.proposals = None
        self.supported = True
        self.failure = None
        self.gate = None

    async def complete(self, model, messages):
        self.calls.append(copy.deepcopy(messages))
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        data = json.loads(messages[-1]['content'])
        if 'proposals' not in data:
            body = data['document']['excerpt']
            claims = self.proposals or [{'subject': '测试行星', 'predicate': '颜色',
                'value': '红色' if '红色' in body else '蓝色', 'kind': 'fact', 'quote': body,
                'valid_from': None, 'valid_until': None}]
            output = {'format_version': 1, 'claims': claims}
        else:
            output = {'format_version': 1, 'decisions': [{'index': index,
                'supported': self.supported, 'temporal_ok': True, 'version_ok': True,
                'kind': item['kind'], 'note': '核对了本份原文中的逐字引文。'}
                for index, item in enumerate(data['proposals']['claims'])]}
        return json.dumps(output, ensure_ascii=False), {'total_tokens': 11}


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.M4Tests.setUp

    async def asyncTearDown(self):
        if getattr(self,'processor',None) and self.processor.gate:
            self.processor.gate.set()
        self.chat.knowledge.close()
        await self.chat.wait()

    def enable(self):
        self.pid = self.persona['id']
        self.maintenance = self.org.knowledge
        self.source = Source()
        self.maintenance.fetcher = self.source
        self.processor = Processor()
        self.org.client = self.processor
        self.now = time.time()
        self.maintenance.clock = lambda: self.now
        self.chat.knowledge.update_policy(self.pid, {'enabled': True, 'channels': ['desktop','qq']}, 0, 'read')
        policy = self.maintenance.policy(self.pid)['data']
        policy['enabled'] = True
        self.maintenance.update_policy(self.pid, policy, 0, 'learn')

    def submit(self, public_id='测试行星', source='wikipedia_zh'):
        with self.store.connection() as db:
            from persona_runtime.knowledge_service import read
            revision = read(db, 'refresh', {})['revision']
        return self.maintenance.submit(self.pid, [{'source_id': source, 'public_id': public_id}],
                                       revision, identifier())

    async def process(self, count=8):
        for _ in range(count):
            await self.maintenance.tick()
            await self.org.run_next()
            self.now += 2

    async def test_two_independent_stages_no_fake_chat_and_content_reuse(self):
        self.enable();self.source.put();self.submit()
        await self.process()
        self.assertEqual(len(self.processor.calls), 2)
        result = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['candidates'][0]['title'], '测试行星')
        self.assertEqual(result['evidence'][0]['claim_text'], '测试行星 颜色 蓝色')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0], 2)
            purposes = [json.loads(row[0])['purpose'] for row in db.execute('SELECT manifest_json FROM context_manifests')]
            self.assertEqual(purposes, ['knowledge_extract', 'knowledge_review'])
        self.now += 86401;self.source.put(revision=2)
        await self.process()
        self.assertEqual(len(self.processor.calls), 2)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_documents').fetchone()[0], 2)
        self.assertTrue(self.chat.knowledge.search(self.pid, '测试行星颜色')['evidence'])

    async def test_correction_filters_saved_results_and_old_versions(self):
        self.enable();self.source.put();self.submit();await self.process()
        saved = self.chat.knowledge.search(self.pid, '测试行星颜色')
        self.now += 86401;self.source.put(value='红色', revision=2)
        await self.process()
        current = self.chat.knowledge.search(self.pid, '测试行星颜色')
        self.assertIn('红色', current['evidence'][0]['claim_text'])
        with self.store.connection() as db:
            checked = self.chat.knowledge.recheck(db, {'persona_id': self.pid, 'channel': 'desktop'}, saved)
            self.assertEqual(checked['evidence'], [])
            self.assertEqual(len(list(db.execute('SELECT * FROM knowledge_claims'))), 2)

    async def test_future_and_expired_claims_obey_effective_intervals(self):
        self.enable()
        now = datetime.now(timezone.utc)
        future = (now + timedelta(days=1)).isoformat()
        body = '当前颜色蓝色。明天起颜色红色。'
        self.source.put(body=body)
        self.processor.proposals = [
            {'subject':'测试行星','predicate':'颜色','value':'蓝色','kind':'fact','quote':'当前颜色蓝色。',
             'valid_from':None,'valid_until':future},
            {'subject':'测试行星','predicate':'颜色','value':'红色','kind':'fact','quote':'明天起颜色红色。',
             'valid_from':future,'valid_until':None}]
        self.submit();await self.process()
        with self.store.connection() as db:
            before = eligible(db, now=now.timestamp())
            after = eligible(db, now=now.timestamp()+2*86400)
        self.assertEqual([row['value'] for row in before], ['蓝色'])
        self.assertEqual([row['value'] for row in after], ['红色'])

    async def test_conflicts_and_retraction_recompute_remaining_support(self):
        self.enable();self.source.put();self.submit();await self.process()
        self.source.put('Q123', source='wikidata', value='红色')
        self.submit('Q123', 'wikidata');await self.process()
        result = self.chat.knowledge.search(self.pid, '测试行星颜色')
        self.assertTrue(all(row['claim_state']=='conflict' for row in result['evidence']))
        self.assertEqual({row['source_group'] for row in result['evidence']}, {'wikimedia'})
        with self.store.connection() as db:
            target = db.execute("SELECT id FROM knowledge_targets WHERE source_id='wikidata'").fetchone()[0]
        self.maintenance.withdraw(self.pid, target, '明确来源撤回', 0, 'withdraw')
        current = self.chat.knowledge.search(self.pid, '测试行星颜色')
        self.assertEqual(len(current['evidence']), 1)
        self.assertEqual(current['evidence'][0]['claim_state'], 'source_checked')
        with self.store.connection() as db:
            self.assertEqual(self.chat.knowledge.recheck(db, {'persona_id':self.pid,'channel':'desktop'}, result)['evidence'], [])

    async def test_upstream_suppression_is_processed_by_product_fetch(self):
        self.enable();self.source.put();self.submit();await self.process()
        self.source.values[('wikipedia_zh','测试行星')] = FetchError('knowledge_upstream_withdrawn', withdrawn=True)
        self.now += 86401
        await self.process()
        self.assertEqual(self.chat.knowledge.search(self.pid, '测试行星颜色')['evidence'], [])
        self.assertEqual(len(self.processor.calls), 2)

    async def test_missing_and_rate_limit_do_not_retract_a_fact(self):
        self.enable();self.source.put();self.submit();await self.process()
        self.source.values[('wikipedia_zh','测试行星')] = FetchError('knowledge_http_429', retry_after=900)
        self.now += 86401;await self.process(1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM knowledge_pages').fetchone()[0], 'active')
            job = db.execute('SELECT * FROM knowledge_fetch_jobs ORDER BY created_epoch DESC').fetchone()
            self.assertGreaterEqual(job['ready_after'], self.now+890)
        self.assertTrue(self.chat.knowledge.search(self.pid, '测试行星颜色')['evidence'])

    async def test_shared_quota_waits_then_resumes_without_reextracting(self):
        self.enable();self.source.put();self.submit();await self.process()
        self.cfg.update({'organization': {'hourly_limit': 1}})
        self.now += 86401;self.source.put(value='红色', revision=2)
        await self.process()
        with self.store.connection() as db:
            task = db.execute("SELECT * FROM organization_tasks WHERE status='waiting_quota'").fetchone()
            self.assertIsNotNone(task)
            self.assertEqual(task['automatic'], 1)
        self.assertEqual(len(self.processor.calls), 3)
        self.cfg.update({'organization': {'hourly_limit': 60}})
        await self.process()
        self.assertEqual(len(self.processor.calls), 4)

    async def test_saved_receipt_recovers_publication_without_new_model_call(self):
        self.enable();self.source.put();self.submit()
        await self.maintenance.tick();await self.maintenance.tick()
        original = self.maintenance.published
        with patch.object(self.maintenance, 'published', side_effect=RuntimeErrorBase('injected_before_commit')):
            self.assertTrue(await self.org.run_next())
        with self.store.connection() as db:
            task_id = db.execute('SELECT id FROM organization_tasks').fetchone()[0]
        self.org.retry(task_id)
        self.assertTrue(await self.org.run_next())
        await self.process()
        self.assertEqual(len(self.processor.calls), 2)
        self.assertTrue(self.chat.knowledge.search(self.pid, '测试行星颜色')['evidence'])

    async def test_unknown_call_is_not_replayed_by_duplicate_discovery(self):
        self.enable();self.source.put();self.submit()
        self.processor.failure = ModelError('provider_network_error', diagnostics={'request_headers_sent':True})
        await self.process()
        self.now += 86401;await self.process()
        self.assertEqual(len(self.processor.calls), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM organization_tasks').fetchone()[0], 'blocked_unknown')

    async def test_stop_learning_and_bad_original_quote_prevent_publication(self):
        self.enable();self.source.put();self.submit()
        await self.maintenance.tick();await self.maintenance.tick()
        policy = self.maintenance.policy(self.pid)
        data = policy['data']|{'enabled':False}
        self.maintenance.update_policy(self.pid, data, policy['revision'], 'stop')
        await self.org.run_next()
        self.assertEqual(len(self.processor.calls), 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM organization_tasks').fetchone()[0], 'blocked_authorization')

    async def test_invalid_proposal_is_isolated_while_valid_proposal_is_reviewed(self):
        self.enable();self.source.put()
        good={'subject':'测试行星','predicate':'颜色','value':'蓝色','kind':'source_claim',
              'quote':'测试行星的颜色是蓝色。','valid_from':None,'valid_until':None}
        bad=dict(good,quote='没有这句话')
        self.processor.proposals=[good,bad]
        self.submit();await self.process()
        result=self.chat.knowledge.search(self.pid,'测试行星颜色')
        self.assertEqual(len(result['evidence']),1)
        self.assertEqual(result['evidence'][0]['claim_kind'],'source_claim')
        with self.store.connection() as db:
            stored=json.loads(db.execute('SELECT data_json FROM knowledge_batches').fetchone()[0])
            self.assertEqual(stored['proposals']['quarantined'][0]['reason'],'quote_not_exact')
        self.assertEqual(len(self.processor.calls),2)

    async def test_reflection_preview_uses_public_document_guard(self):
        self.enable();self.source.put();self.submit();await self.process()
        with self.store.connection() as db:
            task_id=db.execute('SELECT extract_task_id FROM knowledge_batches').fetchone()[0]
        preview=self.chat.cognitive.preview(self.pid,'desktop','reflection',task_id=task_id)
        self.assertEqual(preview['status'],'ok')
        self.assertEqual(preview['manifest']['purpose'],'knowledge_extract')
        self.assertEqual(preview['manifest']['evidence_refs'][0]['kind'],'knowledge_document')

    async def test_source_rate_limit_applies_to_other_targets(self):
        self.enable();self.source.put();self.source.put('另一行星')
        self.source.values[('wikipedia_zh','测试行星')]=FetchError('knowledge_http_429',retry_after=172800)
        self.submit();await self.maintenance.tick()
        self.submit('另一行星');self.now+=2;await self.maintenance.tick()
        self.assertEqual(len(self.source.calls),1)

    async def test_latest_query_waits_for_review_without_claiming_checked_results(self):
        from persona_runtime.knowledge_context import attach
        self.enable();self.source.put();self.submit();await self.process()
        with self.store.connection() as db:
            before=len(list(db.execute('SELECT * FROM organization_attempts')))
        result=await self.chat.submit('desktop','测试行星最新颜色是什么','current-source')
        await self.chat.wait()
        view=self.chat.get(result['id'])
        self.assertEqual(view['status'],'completed')
        self.assertTrue(view['context']['knowledge']['evidence'])
        self.source.put(value='红色',revision=2);self.now+=2
        result=await self.chat.submit('desktop','测试行星最新颜色是什么','new-current-source')
        await self.chat.wait();view=self.chat.get(result['id'])
        self.assertEqual(view['context']['knowledge']['state'],'refresh_pending')
        self.assertEqual(view['context']['knowledge']['evidence'],[])

    async def test_dated_queries_keep_nonoverlapping_versions_and_source_followup(self):
        self.enable();self.source.put(body='1950年到1955年颜色是蓝色。1955年起颜色是红色。')
        self.processor.proposals=[
            {'subject':'测试行星','predicate':'颜色','value':'蓝色','kind':'fact','quote':'1950年到1955年颜色是蓝色。',
             'valid_from':'1950-01-01T00:00:00Z','valid_until':'1955-01-01T00:00:00Z'},
            {'subject':'测试行星','predicate':'颜色','value':'红色','kind':'fact','quote':'1955年起颜色是红色。',
             'valid_from':'1955-01-01T00:00:00Z','valid_until':None}]
        self.submit();await self.process()
        self.assertTrue(self.source.last_interactive)
        result=self.chat.knowledge.search(self.pid,'1952年测试行星颜色')
        self.assertEqual(len(result['evidence']),1)
        self.assertIn('蓝色',result['evidence'][0]['claim_text'])
        self.assertEqual(result['evidence'][0]['time_state'],'dated')
        result=self.chat.knowledge.search(self.pid,'1955年测试行星颜色')
        self.assertEqual([item['claim_state'] for item in result['evidence']],['source_checked'])
        reply=await self.chat.submit('desktop','1952年测试行星颜色是什么','dated-world')
        await self.chat.wait();view=self.chat.get(reply['id']);source_calls=len(self.source.calls)
        followup=await self.chat.submit('desktop','给我出处','dated-source',session_id=view['session_id'])
        await self.chat.wait();shown=self.chat.get(followup['id'])
        self.assertEqual(shown['context']['knowledge']['evidence'][0]['claim_id'],view['context']['knowledge']['evidence'][0]['claim_id'])
        self.assertEqual(len(self.source.calls),source_calls)

    async def test_changed_document_during_model_call_blocks_old_publication(self):
        import asyncio
        self.enable();self.source.put();self.submit()
        await self.maintenance.tick();await self.maintenance.tick()
        self.processor.gate=asyncio.Event()
        running=asyncio.create_task(self.org.run_next())
        for _ in range(100):
            if self.processor.calls:break
            await asyncio.sleep(.01)
        self.assertTrue(self.processor.calls)
        self.now+=2;self.source.put(value='红色',revision=2);self.submit()
        await self.maintenance.tick()
        self.processor.gate.set();await running;self.processor.gate=None
        await self.process()
        with self.store.connection() as db:
            original=db.execute('SELECT status FROM organization_tasks ORDER BY rowid LIMIT 1').fetchone()[0]
            self.assertEqual(original,'stale')
        result=self.chat.knowledge.search(self.pid,'测试行星颜色')
        self.assertTrue(result['evidence'])
        self.assertTrue(all('红色' in row['claim_text'] for row in result['evidence']))


class FetchAndOutputTests(unittest.TestCase):
    def test_only_public_identifiers_and_structured_suppression(self):
        with self.assertRaises(RuntimeErrorBase):public_identifier('wikidata','我的朋友')
        with self.assertRaises(RuntimeErrorBase):public_identifier('wikipedia_zh','User:private')
        self.assertEqual(public_identifier('wikidata','Q2'), 'Q2')
        source = {'adapter':'mediawiki','license_url':'public'}
        with self.assertRaises(FetchError) as raised:
            WikimediaFetcher.parse(source, {'query':{'pages':[{'ns':0,'pageid':1,'title':'测试',
                'revisions':[{'slots':{'main':{'texthidden':True}}}]}]}})
        self.assertTrue(raised.exception.withdrawn)

    def test_extraction_requires_exact_original_quote_and_zoned_dates(self):
        payload = {'external_knowledge':{'stage':'extract','document':{'excerpt':'原始资料。'}}}
        item = {'subject':'对象','predicate':'属性','value':'值','kind':'fact','quote':'不存在',
                'valid_from':None,'valid_until':None}
        with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_output_invalid'):
            parse(payload, json.dumps({'format_version':1,'claims':[item]}))
        item.update(quote='原始资料。',valid_from='2026-11-10')
        with self.assertRaises(RuntimeErrorBase):parse(payload,json.dumps({'format_version':1,'claims':[item]}))

    def test_relative_dates_require_an_anchor_and_an_effective_interval(self):
        payload={'external_knowledge':{'stage':'extract','document':{'excerpt':'明天起颜色是红色。','upstream_at':None}}}
        item={'subject':'对象','predicate':'颜色','value':'红色','kind':'fact','quote':'明天起颜色是红色。',
              'valid_from':'2026-11-11T00:00:00Z','valid_until':None}
        output=parse(payload,json.dumps({'format_version':1,'claims':[item]}),isolate_items=True)
        self.assertEqual(output['claims'],[])
        self.assertEqual(output['quarantined'][0]['reason'],'relative_time_anchor_unknown')


if __name__ == '__main__':
    unittest.main()
