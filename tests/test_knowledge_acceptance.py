"""Acceptance regressions for retained facts and accurately named test origins."""

import copy
import json
import unittest

from persona_runtime.knowledge_context import attach
from persona_runtime.knowledge_store import evidence_usable
from tests import test_knowledge_maintenance as maintenance


class KnowledgeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    setUp = maintenance.MaintenanceTests.setUp
    asyncTearDown = maintenance.MaintenanceTests.asyncTearDown
    enable = maintenance.MaintenanceTests.enable
    submit = maintenance.MaintenanceTests.submit
    process = maintenance.MaintenanceTests.process

    def proposals(self, color, include_diameter):
        rows = [{'subject': '测试行星', 'predicate': '颜色', 'value': color, 'kind': 'fact',
                 'quote': '测试行星的颜色是' + color + '。', 'valid_from': None, 'valid_until': None}]
        if include_diameter:
            rows.append({'subject': '测试行星', 'predicate': '直径', 'value': '20千米', 'kind': 'fact',
                'quote': '测试行星的直径是20千米。', 'valid_from': None, 'valid_until': None})
        self.processor.proposals = rows

    async def initial(self):
        self.enable()
        self.source.put(body='测试行星的颜色是蓝色。测试行星的直径是20千米。')
        self.proposals('蓝色', True); self.submit(); await self.process()

    async def test_correction_rechecks_unchanged_fact_outside_initial_excerpt(self):
        await self.initial()
        body = '测试行星的颜色是红色。' + ('公开补充资料。' * 1500) + '测试行星的直径是20千米。'
        self.source.put(revision=2, body=body)
        self.proposals('红色', False); self.now += 2; self.submit(); await self.process()
        result = self.chat.knowledge.search(self.pid, '测试行星直径是多少')
        self.assertTrue(any(item['section_title'] == '直径' for item in result['evidence']))
        self.assertFalse(any('蓝色' in item['claim_text'] for item in result['evidence']))
        self.assertEqual(len(self.processor.calls), 4)
        reviewed = json.loads(self.processor.calls[-1][-1]['content'])
        self.assertTrue(any('直径是20千米' in item['text'] for item in reviewed['document']['supporting_fragments']))
        self.assertEqual(len(reviewed['proposals']['claims']), 2)

    async def test_retained_fact_still_requires_independent_review(self):
        await self.initial()
        self.source.put(revision=2, body='测试行星的颜色是红色。测试行星的直径是20千米。')
        self.proposals('红色', False); self.processor.supported = False
        self.now += 2; self.submit(); await self.process()
        self.assertFalse(self.chat.knowledge.search(self.pid, '测试行星直径')['evidence'])
        self.assertEqual(len(self.processor.calls), 4)

    async def test_deleted_original_quote_is_not_retained(self):
        await self.initial()
        self.source.put(revision=2, body='测试行星的颜色是红色。')
        self.proposals('红色', False); self.now += 2; self.submit(); await self.process()
        result = self.chat.knowledge.search(self.pid, '测试行星直径是多少')
        self.assertFalse(any(item['section_title'] == '直径' for item in result['evidence']))

    async def test_claim_components_are_bound_and_legacy_refs_still_work(self):
        await self.initial()
        result = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        item = next(row for row in result['evidence'] if row['section_title'] == '颜色')
        self.assertEqual(item['claim'], {'subject': '测试行星', 'predicate': '颜色', 'value': '蓝色'})
        legacy = copy.deepcopy(item)
        del legacy['claim']
        with self.store.connection() as db:
            self.assertTrue(evidence_usable(db, self.pid, 'desktop', legacy))
            for field in ('subject', 'predicate', 'value'):
                changed = copy.deepcopy(item)
                changed['claim'][field] = '篡改后的内容'
                self.assertFalse(evidence_usable(db, self.pid, 'desktop', changed))

    async def test_current_structured_claim_is_rendered_alongside_exact_history(self):
        await self.initial()
        result = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        original = [{'role': 'system', 'content': '验收上下文。'},
                    {'role': 'assistant', 'content': '资料没有写明测试行星的颜色。'},
                    {'role': 'user', 'content': '测试行星的颜色是什么？'}]
        messages, details = attach(original, {'prompt_assembly': {'blocks': []}}, result,
            {'model_snapshot': json.dumps({'max_input_chars': 8000})})
        self.assertEqual(messages[1:], original[1:])
        self.assertEqual(original[0]['content'], '验收上下文。')
        self.assertIn('历史回答与引文有出入时，依据引文更正回答', messages[0]['content'])
        self.assertIn('"subject": "测试行星"', messages[0]['content'])
        self.assertIn('"value": "蓝色"', messages[0]['content'])
        self.assertTrue(all('claim' in row for row in details['knowledge']['evidence']))
        block = details['prompt_assembly']['blocks'][0]
        self.assertEqual(block['version'], 'encyclopedia-evidence-v2')

    async def test_old_claim_ref_is_rejected_after_value_correction(self):
        await self.initial()
        old = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        self.source.put(revision=2, body='测试行星的颜色是红色。测试行星的直径是20千米。')
        self.proposals('红色', False)
        self.now += 2; self.submit(); await self.process()
        with self.store.connection() as db:
            row = next(item for item in old['evidence'] if item['section_title'] == '颜色')
            self.assertFalse(evidence_usable(db, self.pid, 'desktop', row))
        current = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        self.assertTrue(any(item['claim']['value'] == '红色' for item in current['evidence']))

    async def test_controlled_http_origin_and_revision_link_are_explicit(self):
        self.enable(); self.source.put()
        value = self.source.values[('wikipedia_zh', '测试行星')]
        value['url'] = 'http://127.0.0.1:38911/documents/lighthouse'
        value['metadata']['pcar_provenance'] = {'kind': 'controlled_http_fixture',
            'name': '受控HTTP验收公告', 'group': 'pcar-controlled-primary',
            'revision_url': value['url'] + '/revisions/1'}
        self.submit(); await self.process()
        result = self.chat.knowledge.search(self.pid, '测试行星颜色是什么')
        item = result['evidence'][0]
        self.assertEqual(item['source_name'], '受控HTTP验收公告')
        self.assertEqual(item['source_group'], 'pcar-controlled-primary')
        self.assertEqual(item['source_origin'], 'controlled_http_fixture')
        self.assertEqual(item['version_url'], value['metadata']['pcar_provenance']['revision_url'])
        messages, _ = attach([{'role': 'system', 'content': '验收上下文。'}], {}, result,
                              {'model_snapshot': json.dumps({'max_input_chars': 8000})})
        self.assertIn('受控HTTP验收公告', messages[0]['content'])
        self.assertIn(value['metadata']['pcar_provenance']['revision_url'], messages[0]['content'])
        changed = copy.deepcopy(item); changed['version_url'] = 'https://example.invalid/forged'
        with self.store.connection() as db:
            self.assertFalse(evidence_usable(db, self.pid, 'desktop', changed))


if __name__ == '__main__':
    unittest.main()
