"""QQ plain-text contract and durable ledger consistency, without platform calls."""
import hashlib
import unittest
from persona_runtime.qq_text import plain_qq_text
from persona_runtime.models import identifier
from tests import test_m63 as m63


class QQTextTests(unittest.TestCase):
    def test_conversational_emphasis_headings_lists_and_quotes(self):
        raw = '# 阅读角\n\n**建议一**：蓝色坐垫。\n- *预算*：三百元\n> 不用买大件\n~~旧方案~~'
        self.assertEqual(plain_qq_text(raw), '阅读角\n\n建议一：蓝色坐垫。\n预算：三百元\n不用买大件\n旧方案')

    def test_code_keeps_operators_indentation_and_fences_disappear(self):
        raw = '完整代码：\n```python\ndef square(a_b):\n    return a_b ** 2\nassert square(3) == 9\n```\n用 `square(3)` 调用。'
        self.assertEqual(plain_qq_text(raw), '完整代码：\ndef square(a_b):\n    return a_b ** 2\nassert square(3) == 9\n用 square(3) 调用。')

    def test_links_numbers_tables_and_plain_text_survive(self):
        raw = '| 类别 | 金额 |\n| --- | ---: |\n| 坐垫 | 80 |\n\n[资料](https://example.com/a_b?q=1)'
        value = plain_qq_text(raw)
        self.assertNotIn('|', value)
        self.assertIn('坐垫；80', value)
        self.assertIn('资料（https://example.com/a_b?q=1）', value)
        self.assertEqual(plain_qq_text('身高162厘米，预算300，蓝色。'), '身高162厘米，预算300，蓝色。')
        url = 'https://example.com/a/**key**?q=__value__'
        self.assertIn(url, plain_qq_text(f'[资料]({url})'))
        self.assertEqual(plain_qq_text(r'\*\*建议\*\*'), '建议')

    def test_conversion_is_idempotent_for_chat(self):
        text = '**标题**\n- 蓝色\n[来源](https://example.com)'
        self.assertEqual(plain_qq_text(plain_qq_text(text)), plain_qq_text(text))


class QQLedgerTextTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = m63.M63Tests.asyncSetUp
    async def test_plain_text_is_persisted_before_split_and_hash(self):
        self.fake.text = '**蓝色**' * 100
        turn = await self.engine.submit('qq', 'plain QQ check', identifier(), origin={'sender': '123', 'bot': '456'})
        await self.engine.wait()
        turn = self.engine.get(turn['id'])
        batch = self.delivery.by_batch(turn['reply_batch']['id'])
        parts = [p for p in batch['parts'] if p['turn_id'] == turn['id']]
        self.assertEqual(''.join(p['text'] for p in parts), '蓝色'*100)
        self.assertTrue(all(p['text_hash'] == hashlib.sha256(p['text'].encode()).hexdigest() for p in parts))
        self.assertEqual(turn['reply'], '**蓝色**'*100)
        self.assertEqual(turn['attempt_count'], 1)

    async def test_format_only_output_fails_atomically_without_empty_stream(self):
        self.fake.text = '---'
        turn = await self.engine.submit('qq', 'empty QQ check', identifier(), origin={'sender': '123', 'bot': '456'})
        await self.engine.wait(); turn = self.engine.get(turn['id'])
        self.assertEqual(turn['status'], 'failed')
        self.assertEqual(turn['error_code'], 'qq_plain_text_empty')
        self.assertIsNone(turn['reply_batch'])
        self.assertEqual(turn['attempt_count'], 1)


if __name__ == '__main__': unittest.main()
