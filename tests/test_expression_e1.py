"""E1原文映射、投影、单次传输及未知位置恢复验证；无收费调用。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web

from experiments.expression_e1.core import (ExpressionPayloadError, bubble_plan, build_messages,
    decode_output, local_baseline_plan, map_sentences, offered_candidates, protected_in_expression)
from experiments.expression_e1.maibot_components import normalize_selected_ids
from experiments.expression_e1.runner import (fingerprint, public_model, report, run, sha)
from experiments.expression_e1.transport import ExpressionCallFailure, ExpressionHTTPClient
from experiments.expression_e1.review import view, candidate
from persona_runtime.config import atomic_json
from persona_runtime.provider_compat import build_request


REPO = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((REPO / 'experiments/expression_e0/public_fixtures.json').read_text(encoding='utf-8'))


def material(index=-1):
    fixture = FIXTURES['cases'][index]
    spans = []
    for item in fixture['protect']:
        start = fixture['draft'].index(item['text'])
        spans.append({'span_id': item['id'], 'start': start, 'end': start + len(item['text']), 'kind': item['kind']})
    return {'draft': fixture['draft'], 'persona': fixture['persona'], 'role_projection': FIXTURES['personas'][fixture['persona']],
            'user': fixture['user'], 'already_spoken': fixture.get('already_spoken', []), 'history': [],
            'protected': spans, 'candidate_ids': fixture['candidate_ids']}


class ExpressionE1CoreTests(unittest.TestCase):
    def test_maibot_mapping_keeps_all_whitespace_and_original_punctuation(self):
        body = '先说，  \t\n再说。\n\n最后🫖。'
        pieces = map_sentences(body, ['先说,', '再说.', '最后🫖.'])
        self.assertEqual(pieces, ['先说，  \t\n', '再说。\n\n', '最后🫖。'])
        self.assertEqual(''.join(pieces), body)

    def test_no_split_is_complete_and_changed_content_is_rejected(self):
        self.assertEqual(map_sentences('好。', '"no_split"'), ['好。'])
        with self.assertRaisesRegex(ExpressionPayloadError, 'segments_changed_body'):
            map_sentences('别覆盖文件。', ['覆盖文件。'])

    def test_protected_code_and_quotes_survive_rewrite(self):
        for index in (0, 1):
            item = material(index)
            body = '接着说。' + item['draft']
            payload = json.dumps({'body': body, 'segments': 'no_split', 'selected_expression_ids': []}, ensure_ascii=False)
            result = decode_output(payload, item, [])
            self.assertEqual(result['body'], body)
            self.assertEqual(len(result['protected']), len(item['protected']))
            literal = item['draft'][item['protected'][0]['start']:item['protected'][0]['end']]
            changed = body.replace(literal, '丢失了原文')
            with self.assertRaisesRegex(ExpressionPayloadError, 'protected_text_changed'):
                decode_output(json.dumps({'body': changed}), item, [])

    def test_invalid_plan_falls_back_to_complete_expression_body(self):
        item = material()
        result = decode_output('{"body":"好，明天接着聊。","segments":["好。"],"selected_expression_ids":[]}', item, [])
        self.assertEqual(result['body'], '好，明天接着聊。')
        self.assertTrue(result['plan_fallback'])
        self.assertEqual(result['segments'][0]['text'], result['body'])

    def test_protected_block_cut_uses_whole_expression(self):
        item = material(0)
        body = item['draft']
        cut = item['protected'][0]['start'] + 5
        result = decode_output(json.dumps({'body': body, 'segments': [body[:cut], body[cut:]], 'selected_expression_ids': []}), item, [])
        self.assertEqual(result['notes'], ['protected_text_split'])
        self.assertEqual(''.join(s['text'] for s in result['segments']), body)

    def test_transport_json_and_duplicate_keys_never_become_body(self):
        for raw in ('```json\n{}\n```', '{"body":"好","body":"再说"}', '{"analysis":"过程","body":"好"}', '[]', '   '):
            with self.assertRaises(ExpressionPayloadError):
                decode_output(raw, material(), [])

    def test_role_scope_and_empty_selection_follow_upstream_rules(self):
        item = material(5)
        offered = offered_candidates(item, FIXTURES, with_assets=True)
        self.assertEqual(len(offered), 1)
        invalid = dict(item, candidate_ids=['qn-casual-v1'])
        with self.assertRaisesRegex(ExpressionPayloadError, 'candidate_scope_changed'):
            offered_candidates(invalid, FIXTURES, with_assets=True)
        self.assertEqual(normalize_selected_ids({'selected_expression_ids': ['missing', offered[0]['id'], offered[0]['id'], True]}, offered), [offered[0]['id']])
        self.assertEqual(normalize_selected_ids({'selected_expression_ids': []}, offered), [])

    def test_C0_changes_only_expression_candidates_and_keeps_context(self):
        item = material(4)
        item['history'] = [{'speaker': 'user', 'text': '前面那个话题。'}, {'speaker': 'assistant', 'text': '前面实际说过的话。'}]
        c, candidates = build_messages(item, FIXTURES, with_assets=True)
        c0, empty = build_messages(item, FIXTURES, with_assets=False)
        self.assertTrue(candidates)
        self.assertEqual(empty, [])
        self.assertEqual(c[1:], c0[1:])
        self.assertIn(item['role_projection']['name'], c0[0]['content'])
        for prompt in (c, c0):
            text = json.dumps(prompt, ensure_ascii=False)
            for hidden in ('不固定台词', '不要求必须给初步判断', 'must_preserve_for_review', 'source_kind', 'E0-21'):
                self.assertNotIn(hidden, text)

    def test_local_B_preserves_code_blank_lines_and_all_characters(self):
        for index in range(len(FIXTURES['cases'])):
            item = material(index)
            plan = local_baseline_plan(item)
            self.assertEqual(''.join(s['text'] for s in plan), item['draft'])
            self.assertEqual(plan[0]['delay_ms'], 0)
            self.assertLessEqual(sum(s['delay_ms'] for s in plan), 6000)


class ExpressionE1TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.count = 0
        self.responses = []

        async def handler(request):
            self.count += 1
            self.payload = await request.json()
            return web.json_response({'model': 'deepseek-flash', 'choices': [{'message': {'content': '  bad JSON  '}, 'finish_reason': 'stop'}],
                                      'usage': {'prompt_tokens': 100, 'completion_tokens': 4, 'total_tokens': 104}})
        app = web.Application()
        app.router.add_post('/chat/completions', handler)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.server.cleanup()

    async def test_one_http_call_keeps_invalid_first_content_and_usage(self):
        cfg = json.loads((REPO / 'experiments/expression_e0/call_plan.json').read_text(encoding='utf-8'))
        model = public_model({'call_plan': cfg}, {'id': 'fixture'}) | {'base_url': f'http://127.0.0.1:{self.port}'}
        received = await ExpressionHTTPClient(allow_loopback_fixture=True).complete(model, [{'role': 'user', 'content': 'return json'}], save_raw=self.responses.append)
        self.assertEqual(self.count, 1)
        self.assertEqual(received['content'], '  bad JSON  ')
        self.assertEqual(json.loads(self.responses[0])['choices'][0]['message']['content'], received['content'])
        self.assertEqual(self.payload['temperature'], 1.0)
        self.assertEqual(self.payload['thinking'], {'type': 'disabled'})
        self.assertEqual(self.payload['max_tokens'], 8192)
        self.assertNotIn('tools', self.payload)


class ExpressionE1ReviewTests(unittest.TestCase):
    def test_model_text_is_escaped_in_review_html(self):
        body = '<script>alert("example")</script>\n代码与原话。'
        rendered = view(body)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        from html import unescape
        self.assertEqual(unescape(rendered.split('>', 1)[1].rsplit('</pre>', 1)[0]), body)

    def test_first_blank_is_named_without_inventing_a_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            atomic_json(root / 'positions/fixture/result.json',
                        {'candidate_status': 'unusable', 'raw_content': ' ' * 23, 'error_code': 'empty_expression_transport'})
            value, note = candidate(root, {}, 'fixture')
            self.assertIsNone(value)
            self.assertIn('23', note)
            self.assertIn('全空白', note)


class FakeExpressionClient:
    def __init__(self, unknown=False, invalid=False):
        self.calls = 0
        self.unknown = unknown
        self.invalid = invalid

    async def complete(self, model, messages, *, save_raw):
        self.calls += 1
        if self.unknown:
            raise ExpressionCallFailure('provider_transport_interrupted', {'request_headers_sent': True})
        body = json.loads(messages[-1]['content'])['主稿']
        content = 'bad JSON' if self.invalid else json.dumps({'body': body, 'segments': 'no_split', 'selected_expression_ids': []})
        receipt = {'choices': [{'message': {'content': content}}]}
        save_raw(json.dumps(receipt).encode('utf-8'))
        return {'content': content, 'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
                'diagnostics': {'request_headers_sent': True, 'finish_reason': 'stop', 'tool_calls_present': False}}


class ExpressionE1RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_run(self, root, token_limit=800000):
        item = material()
        cfg = json.loads((REPO / 'experiments/expression_e0/call_plan.json').read_text(encoding='utf-8'))
        cfg['stop_between_requests']['known_tokens'] = token_limit
        plan = {'version': 'expression-e1-run-v1', 'call_plan': cfg, 'fixtures': FIXTURES,
                'positions': [], 'source_hashes': {}}
        (root / 'inputs').mkdir()
        atomic_json(root / 'inputs/fixture.json', item)
        data = (root / 'inputs/fixture.json').read_bytes()
        for i in range(32):
            p = {'id': f'position-{i}', 'case_id': 'fixture', 'arm': 'C', 'input_sha256': sha(data)}
            messages, _ = build_messages(item, FIXTURES)
            payload, _ = build_request(public_model(plan, p), messages)
            p['request_sha256'] = fingerprint(payload)
            plan['positions'].append(p)
            atomic_json(root / 'positions' / p['id'] / 'request.json', payload)
        atomic_json(root / 'plan.json', plan)
        atomic_json(root / 'plan-hash.json', {'sha256': sha((root / 'plan.json').read_bytes())})
        return plan

    async def test_unknown_attempt_pauses_and_resume_never_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_run(root)
            client = FakeExpressionClient(unknown=True)
            first = await run(root, client=client)
            second = await run(root, client=client)
            self.assertEqual(client.calls, 1)
            self.assertEqual(first['stop_reason'], 'unknown_dispatch_no_replay')
            self.assertEqual(second['unknown_call_positions'], ['position-0'])
            self.assertIsNone(second['estimated_cost_cny'])

    async def test_success_receipt_survives_restart_without_another_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_run(root, token_limit=1)
            client = FakeExpressionClient()
            first = await run(root, client=client)
            second = await run(root, client=client)
            self.assertEqual(client.calls, 1)
            self.assertEqual(first['usable_candidates'], 1)
            self.assertEqual(second['known_tokens'], 120)
            self.assertIsNone(second['actual_cost_cny'])

    async def test_started_marker_without_result_stops_before_any_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_run(root)
            atomic_json(root / 'positions/position-0/dispatch.json', {'attempt_id': 'interrupted'})
            client = FakeExpressionClient()
            result = await run(root, client=client)
            self.assertEqual(client.calls, 0)
            self.assertEqual(result['unresolved_dispatches'], ['position-0'])

    async def test_invalid_success_is_saved_billed_and_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = self.make_run(root, token_limit=1)
            client = FakeExpressionClient(invalid=True)
            result = await run(root, client=client)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result['known_tokens'], 120)
            self.assertEqual(result['rows'][0]['raw_content'], 'bad JSON')
            self.assertEqual(result['rows'][0]['error_code'], 'invalid_expression_json')
            (root / 'positions/position-0/response.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'expression_response_receipt_changed'):
                report(root, plan)


if __name__ == '__main__':
    unittest.main()
