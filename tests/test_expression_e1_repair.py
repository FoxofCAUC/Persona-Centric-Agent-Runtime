"""协议修复、原始回执恢复及不可重放边界；只使用人工夹具。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from experiments.expression_e1 import repair
from experiments.expression_e1 import repair_continue
from experiments.expression_e1 import current
from experiments.expression_e1.repair_review import eligible
from experiments.expression_e1.core import ExpressionPayloadError
from experiments.expression_e1.repair_protocol import (
    JSON_PROTOCOL, TEXT_PROTOCOL, build_messages, decode_output_for, model_for,
)
from experiments.expression_e1.runner import fingerprint, load, sha
from experiments.expression_e1.transport import ExpressionCallFailure
from persona_runtime.config import atomic_json
from persona_runtime.provider_compat import build_request
from tests.test_expression_e1 import FIXTURES, REPO, material


class RepairProtocolTests(unittest.TestCase):
    def test_current_default_uses_verified_json_scoped_protocol(self):
        self.assertEqual(current.DEFAULT_PROTOCOL, JSON_PROTOCOL)
        messages, _ = current.build_messages(material(), FIXTURES)
        self.assertTrue(messages[0]['content'].startswith('返回一个JSON对象'))
        value = current.decode_output('{"body":"好。","segments":"no_split","selected_expression_ids":[]}', material(), [])
        self.assertEqual(value['body'], '好。')

    def test_current_text_alternative_keeps_body_and_does_not_claim_asset_adoption(self):
        value = current.decode_output('好。', material(), [], protocol=TEXT_PROTOCOL)
        self.assertEqual(value['selection_declaration'], 'unreported')
        self.assertEqual(value['body'], '好。')

    def test_json_instruction_is_scoped_and_reinforced_at_current_task(self):
        item = material(4)
        messages, candidates = build_messages(item, FIXTURES, JSON_PROTOCOL)
        self.assertTrue(messages[0]['content'].startswith('返回一个JSON对象'))
        self.assertIn('要求只作用于body', messages[0]['content'])
        self.assertTrue(messages[-1]['content'].endswith('整个JSON对象。'))
        self.assertTrue(candidates)

    def test_text_protocol_omits_json_mode_on_actual_wire(self):
        cfg = load(REPO / 'experiments/expression_e0/call_plan.json')
        for protocol in (JSON_PROTOCOL, TEXT_PROTOCOL):
            position = {'id': 'fixture', 'protocol': protocol}
            messages, _ = build_messages(material(), FIXTURES, protocol)
            payload, _ = build_request(model_for({'call_plan': cfg}, position), messages)
            self.assertEqual(payload.get('response_format'), {'type': 'json_object'} if protocol == JSON_PROTOCOL else None)
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertEqual(payload['temperature'], 1.0)
            self.assertEqual(payload['max_tokens'], 8192)

    def test_variants_keep_current_material_and_real_history_unchanged(self):
        item = material(4)
        item['history'] = [{'speaker': 'assistant', 'text': '已经说过的上一句。'}]
        for protocol in (JSON_PROTOCOL, TEXT_PROTOCOL):
            messages, _ = build_messages(item, FIXTURES, protocol)
            self.assertEqual(messages[1], {'role': 'assistant', 'content': item['history'][0]['text']})
            current, _ = json.JSONDecoder().raw_decode(messages[-1]['content'])
            self.assertEqual(current['主稿'], item['draft'])
            self.assertEqual(current['当前消息'], item['user'])
            for text in ('不固定台词', '不要求必须给初步判断', 'source_kind', 'must_preserve_for_review'):
                self.assertNotIn(text, json.dumps(messages, ensure_ascii=False))

    def test_text_keeps_exact_code_and_whitespace(self):
        item = material(0)
        body = '\n先试这个。\n\n' + item['draft'] + '\n '
        result = decode_output_for(TEXT_PROTOCOL, body, item, [])
        self.assertEqual(result['body'], body)
        self.assertEqual(''.join(s['text'] for s in result['segments']), body)
        self.assertEqual(result['selection_declaration'], 'unreported')
        with self.assertRaisesRegex(ExpressionPayloadError, 'protected_text_changed'):
            decode_output_for(TEXT_PROTOCOL, body.replace('greet(', 'GREET('), item, [])

    def test_empty_is_rejected_without_replacing_body(self):
        for protocol in (JSON_PROTOCOL, TEXT_PROTOCOL):
            with self.assertRaisesRegex(ExpressionPayloadError, 'empty_expression_transport'):
                decode_output_for(protocol, ' ' * 63, material(), [])

    def test_internal_envelope_is_not_rendered_but_explicit_verbatim_json_is_kept(self):
        raw = '{"body":"好。","segments":["好。"],"selected_expression_ids":[]}'
        with self.assertRaisesRegex(ExpressionPayloadError, 'plain_expression_received_internal_envelope'):
            decode_output_for(TEXT_PROTOCOL, raw, material(), [])
        item = dict(material(), draft=raw, protected=[{'span_id': 'json-literal', 'start': 0, 'end': len(raw), 'kind': 'verbatim'}])
        self.assertEqual(decode_output_for(TEXT_PROTOCOL, raw, item, [])['body'], raw)


class FakeClient:
    def __init__(self, *, unknown=False, after_receipt_error=False, blank_json=False):
        self.calls = 0
        self.unknown = unknown
        self.after_receipt_error = after_receipt_error
        self.blank_json = blank_json

    async def complete(self, model, messages, *, save_raw):
        self.calls += 1
        if self.unknown:
            raise ExpressionCallFailure('provider_transport_interrupted', {'request_headers_sent': True})
        data, _ = json.JSONDecoder().raw_decode(messages[-1]['content'])
        body = data['主稿']
        content = ((' ' * 50 if self.blank_json else json.dumps({'body': body, 'segments': 'no_split', 'selected_expression_ids': []}))
                   if model.get('_json_output') else body)
        raw = {'model': 'deepseek-flash', 'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
               'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
        save_raw(json.dumps(raw, ensure_ascii=False).encode('utf-8'))
        if self.after_receipt_error:
            raise RuntimeError('simulated_post_receipt_interruption')
        return {'diagnostics': {'request_headers_sent': True, 'response_headers_received': True, 'elapsed_ms': 10}}


def make_run(root, protocols=(JSON_PROTOCOL, TEXT_PROTOCOL)):
    item = material()
    cfg = load(REPO / 'experiments/expression_e0/call_plan.json')
    plan = {'version': repair.VERSION, 'max_physical_calls': 16, 'original_root': str(root / 'original'),
            'original_files': {}, 'source_hashes': {}, 'call_plan': cfg, 'fixtures': FIXTURES, 'positions': []}
    (root / 'original').mkdir()
    atomic_json(root / 'inputs/fixture.json', item)
    for index, protocol in enumerate(protocols):
        p = {'id': f'slot-{index}', 'case_id': 'fixture', 'original_position': 'old-fixture', 'arm': 'C',
             'protocol': protocol, 'input_sha256': sha((root / 'inputs/fixture.json').read_bytes())}
        messages, _ = build_messages(item, FIXTURES, protocol)
        payload, _ = build_request(model_for(plan, p), messages)
        p['request_sha256'] = fingerprint(payload)
        atomic_json(root / 'positions' / p['id'] / 'request.json', payload)
        plan['positions'].append(p)
    atomic_json(root / 'plan.json', plan)
    atomic_json(root / 'plan-hash.json', {'sha256': sha((root / 'plan.json').read_bytes())})
    return plan


def saved_receipt(root, plan):
    position = plan['positions'][0]
    directory = root / 'positions' / position['id']
    marker = {'position': position['id'], 'attempt_id': 'received-attempt', 'request_sha256': position['request_sha256'],
              'input_sha256': position['input_sha256'], 'retry_index': 0}
    atomic_json(directory / 'dispatch.json', marker)
    raw = {'model': 'deepseek-flash', 'choices': [{'message': {'content': '{"body":"好。","segments":"no_split","selected_expression_ids":[]}'}, 'finish_reason': 'stop'}],
           'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}
    (directory / 'response.json').write_text(json.dumps(raw), encoding='utf-8')
    atomic_json(directory / 'response-receipt.json', {'attempt_id': marker['attempt_id'],
                'request_sha256': position['request_sha256'], 'dispatch_sha256': sha((directory / 'dispatch.json').read_bytes()),
                'response_sha256': sha((directory / 'response.json').read_bytes()), 'http_complete': True})
    return directory


class RepairRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_variant_is_one_call_and_success_resume_needs_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_run(root)
            client = FakeClient()
            first = await repair.run(root, client=client)
            second = await repair.run(root)
            self.assertEqual(client.calls, 2)
            self.assertEqual(first['usable_candidates'], 2)
            self.assertEqual(second['physical_dispatches_recorded'], 2)
            self.assertIsNone(second['actual_cost_cny'])

    async def test_unknown_stops_and_resume_does_not_read_credentials_or_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_run(root)
            client = FakeClient(unknown=True)
            first = await repair.run(root, client=client)
            second = await repair.run(root)
            self.assertEqual(client.calls, 1)
            self.assertEqual(first['stop_reason'], 'unknown_dispatch_no_replay')
            self.assertEqual(second['unknown_usage_records'], 1)
            self.assertIsNone(second['estimated_cost_cny'])

    async def test_raw_receipt_can_recover_without_any_network_or_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = make_run(root, (JSON_PROTOCOL,))
            directory = saved_receipt(root, plan)
            result = await repair.run(root)
            self.assertEqual(result['usable_candidates'], 1)
            self.assertEqual(result['known_tokens'], 120)
            self.assertIsNone(result['rows'][0]['diagnostics']['elapsed_ms'])
            self.assertEqual(load(directory / 'recovery.json')['new_physical_calls'], 0)

    async def test_exception_after_complete_raw_keeps_paid_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_run(root, (JSON_PROTOCOL,))
            client = FakeClient(after_receipt_error=True)
            result = await repair.run(root, client=client)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result['usable_candidates'], 1)
            self.assertEqual(result['unknown_usage_records'], 0)

    async def test_raw_without_completion_checkpoint_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = make_run(root, (JSON_PROTOCOL,))
            directory = saved_receipt(root, plan)
            (directory / 'response-receipt.json').unlink()
            client = FakeClient()
            result = await repair.run(root, client=client)
            self.assertEqual(client.calls, 0)
            self.assertEqual(result['unknown_usage_records'], 1)
            self.assertEqual(result['unresolved_dispatches'], ['slot-0'])

    async def test_missing_raw_and_changed_derived_text_are_detected(self):
        for operation in ('remove_raw', 'change_derived'):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                plan = make_run(root, (JSON_PROTOCOL,))
                await repair.run(root, client=FakeClient())
                directory = root / 'positions/slot-0'
                if operation == 'remove_raw':
                    (directory / 'response.json').unlink()
                else:
                    row = load(directory / 'result.json')
                    row['candidate']['body'] = '被改过的正文。'
                    atomic_json(directory / 'result.json', row)
                with self.assertRaisesRegex(ValueError, 'sealed_receipt'):
                    repair.report(root, plan)

    async def test_incomplete_derived_write_is_locally_sealed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = make_run(root, (JSON_PROTOCOL,))
            directory = saved_receipt(root, plan)
            atomic_json(directory / 'result.json', repair.result_from_receipt(root, plan, plan['positions'][0]))
            result = await repair.run(root)
            self.assertEqual(result['usable_candidates'], 1)
            self.assertTrue((directory / 'seal.json').exists())

    async def test_saved_blank_stays_blank_and_next_planned_variant_is_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_run(root)
            client = FakeClient(blank_json=True)
            result = await repair.run(root, client=client)
            self.assertEqual(client.calls, 2)
            self.assertEqual(result['rows'][0]['raw_content'], ' ' * 50)
            self.assertEqual(result['rows'][0]['candidate_status'], 'unusable')
            self.assertEqual(result['rows'][1]['candidate_status'], 'usable')
            self.assertNotEqual(result['rows'][0]['request_sha256'], result['rows'][1]['request_sha256'])

    async def test_changed_request_stops_before_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_run(root)
            atomic_json(root / 'positions/slot-0/request.json', {'tampered': True})
            client = FakeClient()
            with self.assertRaisesRegex(ValueError, 'repair_request_changed'):
                await repair.run(root, client=client)
            self.assertEqual(client.calls, 0)


class BeforeSendClient:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, *, save_raw):
        self.calls += 1
        raise ExpressionCallFailure('provider_transport_interrupted',
                                    {'request_headers_sent': False, 'response_headers_received': False})


class UnsentAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_rejects_multiple_received_results_instead_of_picking_best(self):
        rows = [{'original_position': 'old', 'protocol': JSON_PROTOCOL, 'state': 'received'}] * 2
        with self.assertRaisesRegex(ValueError, 'multiple_model_results'):
            eligible(rows, 'old', JSON_PROTOCOL)

    async def seed_unsent(self, root):
        plan = make_run(root)
        client = BeforeSendClient()
        await repair.run(root, client=client)
        return plan

    async def test_unsent_usage_is_not_applicable_and_remains_null(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = await self.seed_unsent(root)
            repair_continue.prepare(root)
            revision = load(root / 'execution-v3.json')
            report = repair_continue.report(root, plan, revision)
            self.assertEqual(report['not_dispatched_positions'], ['slot-0'])
            self.assertIsNone(report['rows'][0]['usage'])
            self.assertIsNone(report['rows'][0]['estimated_cost_cny'])
            self.assertEqual(report['unknown_usage_records'], 0)
            self.assertEqual(report['physical_calls_recorded'], 0)

    async def test_continue_keeps_failed_receipt_and_caps_real_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            await self.seed_unsent(root)
            failed = (root / 'positions/slot-0/result.json').read_bytes()
            repair_continue.prepare(root)
            client = FakeClient()
            first = await repair_continue.run(root, client=client)
            second = await repair_continue.run(root)
            self.assertEqual(client.calls, 2)
            self.assertEqual(first['usable_candidates'], 2)
            self.assertEqual(first['physical_calls_recorded'], 2)
            self.assertEqual(second['stop_reason'], 'physical_call_plan_complete')
            self.assertEqual((root / 'positions/slot-0/result.json').read_bytes(), failed)

    async def test_uncertain_call_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make_run(root)
            await repair.run(root, client=FakeClient(unknown=True))
            with self.assertRaisesRegex(ValueError, 'unconfirmed_call_cannot_be_rescheduled'):
                repair_continue.prepare(root)
            self.assertFalse((root / 'execution-v3.json').exists())

    async def test_new_unknown_call_stops_without_replacement_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            await self.seed_unsent(root)
            repair_continue.prepare(root)
            client = FakeClient(unknown=True)
            result = await repair_continue.run(root, client=client)
            self.assertEqual(client.calls, 1)
            self.assertEqual(result['stop_reason'], 'unknown_dispatch_no_replay')
            self.assertEqual(result['unknown_usage_records'], 1)
            self.assertIsNone(result['estimated_cost_cny'])

    async def test_request_that_received_headers_is_not_unsent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            row = {'state': 'failed', 'diagnostics': {'request_headers_sent': False, 'response_headers_received': True}}
            self.assertFalse(repair_continue.not_dispatched(root, row))
            row['diagnostics']['response_headers_received'] = False
            self.assertTrue(repair_continue.not_dispatched(root, row))
            (root / 'response-receipt.json').write_text('{}', encoding='utf-8')
            self.assertFalse(repair_continue.not_dispatched(root, row))


if __name__ == '__main__':
    unittest.main()
