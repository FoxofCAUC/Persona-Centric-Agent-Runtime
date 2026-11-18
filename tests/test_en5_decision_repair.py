"""Offline contract checks for strict Flash receipts and their first-response ledger."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from experiments.en5_decision_contract import ACTIONS, STATES, UNDETERMINED, flash_base, flash_wire
from experiments.en5_decision_d2 import interpret
from experiments.en5_decision_tool import DecisionToolHTTPClient, ToolFirstReceipts, decision_pair
from persona_runtime.deepseek_reply_decision import BASE, FUNCTION, parse_receipt
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.provider_policy import require_generation_provider

TARGETS = [{'id': 1, 'origins': ['current'], 'text': 'T', 'role': 'user'}]
SOURCE = {'base_url': 'https://api.deepseek.com', 'name': 'deepseek-v4-pro'}
PERSONA = {'core_traits': ['TRAIT'], 'expression': {'style': 'STYLE'}}


def fixture():
    old = flash_base(SOURCE, PERSONA, TARGETS)
    return decision_pair(old, 'REVIEWED', TARGETS, 30)


def encoded_response(wire, *, args=None, text=None, finish='tool_calls', tool_name=FUNCTION, tool_count=1):
    if args is None:
        args = json.dumps({'target': 1, 'action': ACTIONS[0], 'task_state': STATES[0]})
    tool = {'type': 'function', 'id': 'TOOL_ID', 'function': {'name': tool_name, 'arguments': args}}
    return json.dumps({'model': wire['model'], 'usage': {'total_tokens': 7},
                       'choices': [{'index': 0, 'finish_reason': finish,
                                    'message': {'role': 'assistant', 'content': text,
                                                'tool_calls': [tool] * tool_count}}]}).encode()


class Contract(unittest.TestCase):
    def test_only_authorized_material_and_one_tool(self):
        model, wire = fixture()
        old = flash_wire(flash_base(SOURCE, PERSONA, TARGETS), 'REVIEWED')
        self.assertEqual(wire['messages'][1:], old['messages'][1:])
        prefix, data = wire['messages'][0]['content'].rsplit('\n\n', 1)
        self.assertEqual(prefix, 'REVIEWED')
        self.assertEqual(json.loads(data), {**PERSONA, 'undetermined': {
            'target': 0, 'action': UNDETERMINED, 'task_state': UNDETERMINED}})
        function = wire['tools'][0]['function']
        self.assertEqual(wire['tool_choice'], {'type': 'function', 'function': {'name': FUNCTION}})
        self.assertEqual((model['base_url'], wire['max_tokens'], wire['stream']), (BASE, 128, False))
        self.assertEqual(wire['thinking'], {'type': 'disabled'})
        self.assertNotIn('response_format', wire)
        self.assertTrue(function['strict'])
        self.assertEqual(set(function['parameters']['required']), {'target', 'action', 'task_state'})
        self.assertFalse(function['parameters']['additionalProperties'])

    def test_exact_tool_and_complete_fields(self):
        _, wire = fixture()
        result = parse_receipt(encoded_response(wire), wire)
        self.assertTrue(result['valid'])
        self.assertEqual(json.loads(result['body']), {'target': 1, 'action': ACTIONS[0], 'task_state': STATES[0]})
        for updates in ({'tool_name': 'OTHER'}, {'tool_count': 0}, {'tool_count': 2},
                        {'text': 'EXTRA'}, {'finish': 'length'}, {'finish': 'stop'},
                        {'args': '{"id":1,"source":[],"text":"T"}'},
                        {'args': '{"target":1,"target":0,"action":"参与交流","task_state":"无待办"}'},
                        {'args': '{"target":true,"action":"参与交流","task_state":"无待办"}'},
                        {'args': '{"target":2,"action":"参与交流","task_state":"无待办"}'},
                        {'args': '{"target":0,"action":"参与交流","task_state":"无待办"}'},
                        {'args': '{"target":1,"action":"未定","task_state":"未定"}'},
                        {'args': '{"target":1,"action":"参与交流","task_state":"无待办","text":"T"}'}):
            with self.subTest(updates=updates):
                invalid = parse_receipt(encoded_response(wire, **updates), wire)
                self.assertFalse(invalid['valid'])
                self.assertIsNone(invalid['body'])
                self.assertEqual(invalid['usage']['total_tokens'], 7)
        unknown = parse_receipt(encoded_response(wire, args=json.dumps({
            'target': 0, 'action': UNDETERMINED, 'task_state': UNDETERMINED,
        })), wire)
        self.assertEqual(json.loads(unknown['body'])['target'], None)
        _, status = interpret({'state': 'completed', 'content': unknown['body']}, {'targets': TARGETS})
        self.assertEqual(status, 'undetermined')

    def test_beta_requires_explicit_isolated_purpose(self):
        model, wire = fixture()
        with self.assertRaises(ValueError):
            require_generation_provider(model)
        with self.assertRaises(ValueError):
            require_generation_provider({'name': 'deepseek-flash', 'base_url': BASE},
                                        allow_reply_decision_tool=True)
        with self.assertRaises(ValueError):
            require_generation_provider(model | {'base_url': 'https://other.example/beta'},
                                        allow_reply_decision_tool=True)
        with self.assertRaises(ValueError):
            require_generation_provider(model | {'name': 'deepseek-v4-pro'}, allow_reply_decision_tool=True)
        with patch('persona_runtime.corpus_learning_transport.unprotect', return_value='LOCAL_SECRET'), \
             patch('persona_runtime.corpus_learning_transport.outbound_proxy', return_value=None), \
             patch('persona_runtime.corpus_learning_transport.model_tls_context', return_value=None):
            live = model | {'encrypted_key': 'LOCAL_MARKER'}
            self.assertEqual(DecisionToolHTTPClient.preflight(live, wire)[0], BASE+'/chat/completions')
            with self.assertRaises(ValueError):
                DecisionToolHTTPClient.preflight(live, wire | {'tool_choice': 'auto'})


class FixtureClient:
    def __init__(self):
        self.count = 0

    def preflight(self, model, wire):
        return None

    async def complete(self, model, wire, *, save_raw):
        self.count += 1
        save_raw(encoded_response(wire), http_status=200, diagnostics={'elapsed_ms': 1})


class Ledger(unittest.IsolatedAsyncioTestCase):
    async def test_one_physical_receipt_and_resume_from_original_bytes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)/'repair'
            root.mkdir()
            model, wire = fixture()
            client = FixtureClient()
            journal = ToolFirstReceipts(root, 'FIXTURE', client=client, guard=lambda _: None)
            credential = SOURCE | {'encrypted_key': 'LOCAL_MARKER'}
            a = await journal.send('1.decision', 'decision', (model, wire), [], credential)
            b = await ToolFirstReceipts(root, 'FIXTURE', client=client, guard=lambda _: None).send(
                '1.decision', 'decision', (model, wire), [], credential)
            self.assertEqual(client.count, 1)
            self.assertEqual(a, b)
            self.assertEqual(a['state'], 'completed')
            self.assertEqual(json.loads(a['content'])['target'], 1)
            with self.assertRaises((RuntimeErrorBase, ValueError)):
                journal.check_wire('body', model, wire)
