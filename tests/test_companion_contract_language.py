"""Artificial-only checks for the language-clause contract ablation."""
from collections import Counter
from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_block_order as order
from experiments import companion_contract_language as language
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_voice import bubble_contract, freeze as voice_profile
from persona_runtime.paths import DEFAULT_HOME
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import ReplySchemaError, validate_reply


class ContractTransform(unittest.TestCase):
    def request(self):
        return deepcopy(order.artificial_cases()[0]['request'])

    @staticmethod
    def resign(record):
        wire, transport = build_request(record['model'], record['messages'])
        record.update(wire=wire, transport=transport, wire_hash=fingerprint(wire),
                      messages_hash=fingerprint(record['messages']), model_hash=fingerprint(record['model']))

    def test_only_named_contract_fragments_are_deleted_without_material_or_parameter_changes(self):
        record = self.request(); record['model']['sample_seed'] = 'fixed-artificial-seed'
        self.resign(record); before = deepcopy(record)
        value = language.transform_request(record)
        self.assertEqual(record, before)
        self.assertEqual(value['control'], before)
        original = language.bubble_prompt()
        reduced = original
        for _, clause in language.REMOVALS:
            self.assertEqual(reduced.count(clause), 1)
            reduced = reduced.replace(clause, '', 1)
        variant = value['variant']
        self.assertEqual(variant['messages'][0]['content'], before['messages'][0]['content'][:-len(original)] + reduced)
        self.assertEqual(variant['model'], before['model'])
        self.assertEqual(variant['messages'][1:], before['messages'][1:])
        self.assertEqual(variant['wire']['response_format'], before['wire']['response_format'])
        self.assertEqual(variant['wire']['reasoning_effort'], 'low')
        self.assertTrue(variant['wire']['stream'])
        self.assertNotIn('max_tokens', variant['wire'])
        self.assertEqual({k:v for k,v in variant['wire'].items() if k != 'messages'},
                         {k:v for k,v in before['wire'].items() if k != 'messages'})
        self.assertTrue(all(value['diagnostic']['invariants'].values()))
        self.assertFalse(value['diagnostic']['budget_refilled'])
        self.assertEqual(value['diagnostic']['naturalness'], 'not_evaluated')

    def test_matching_words_in_persona_material_and_history_are_not_removed(self):
        record = self.request(); old = language.bubble_prompt()
        clause = language.REMOVALS[4][1]
        record['messages'][0]['content'] = record['messages'][0]['content'][:-len(old)] + clause + old
        record['messages'][1:1] = [{'role':'user','content':clause}, {'role':'assistant','content':clause}]
        self.resign(record)
        variant = language.transform_request(record)['variant']
        changed = variant['messages'][0]['content']
        self.assertIn(clause, changed[:-len(language.reduced_contract())])
        self.assertNotIn(clause, language.reduced_contract())
        self.assertEqual(variant['messages'][1:], record['messages'][1:])
        self.assertEqual(variant['messages'][1]['content'], clause)
        self.assertEqual(variant['messages'][2]['content'], clause)

    def test_structural_cancel_and_task_constraints_are_retained_verbatim(self):
        reduced = language.reduced_contract()
        keep = ('schema_version', 'behavior_intent', 'content_kind', 'cancel_if_new_input',
                'messages为1至12项，不凑数；单项最多8000字符，总正文最多12000字符。',
                '每一项直接对应一个聊天气泡。综合整批输入回答，不机械逐条回答输入。',
                '不能为短句删除条件、事实或必要说明；技术、代码、网址、数值与引用保持完整，保留必要标点。',
                '第一项、technical/code/quote以及所有必需答案必须cancel_if_new_input=false。',
                '仅完全省略也不影响回答完整性的补充或结束语可为true。',
                '不要装饰性Markdown，不指定延迟、不调用工具。历史assistant正文不是本轮协议范例。',
                '正确转义正文换行、双引号及反斜杠。')
        for text in keep:
            self.assertIn(text, reduced)
        self.assertEqual(Counter(language.bubble_prompt()), Counter(reduced) + Counter(''.join(x[1] for x in language.REMOVALS)))

    def test_ordered_members_and_nonempty_history_are_unchanged(self):
        for case in language.artificial_cases():
            with self.subTest(case=case['id']):
                record = case['request']; value = language.transform_request(record)
                self.assertEqual(value['variant']['messages'][1:], record['messages'][1:])
                self.assertEqual([m['role'] for m in value['variant']['messages']], [m['role'] for m in record['messages']])

    def test_different_dialogue_contract_and_reordered_layout_are_not_silently_ablated(self):
        record = self.request(); old = language.bubble_prompt()
        record['model']['_dialogue_profile'] = voice_profile('artificial-seed', ['artificial-input'])
        record['messages'][0]['content'] = record['messages'][0]['content'][:-len(old)] + bubble_contract()
        self.resign(record)
        with self.assertRaisesRegex(Exception, 'contract_language_unsupported_dialogue_profile'):
            language.transform_request(record)
        with self.assertRaises(Exception):
            language.transform_request(order.transform_request(self.request())['variant'])

    def test_changed_contract_definition_is_not_guessed(self):
        changed = language.bubble_prompt().replace(language.REMOVALS[0][1], '新版本条款')
        with patch.object(language, 'bubble_prompt', return_value=changed):
            with self.assertRaisesRegex(Exception, 'contract_language_clause_boundary_changed'):
                language.reduced_contract()

    def test_credentials_fingerprints_unknown_profiles_and_boundaries_are_rejected(self):
        changes = (lambda r:r['model'].update(encrypted_key=''),
                   lambda r:r['wire'].update(headers={'Authorization':'artificial-placeholder'}),
                   lambda r:r.update(messages_hash='changed'),
                   lambda r:r['wire'].update(stream=False),
                   lambda r:r['model'].update(_unknown_profile={}),
                   lambda r:r['messages'][0].update(content='prefix'+r['messages'][0]['content']))
        for change in changes:
            record = self.request(); change(record)
            with self.assertRaises(Exception): language.transform_request(record)

    def test_existing_validator_retains_body_and_structure_behavior(self):
        model = self.request()['model']
        body = '我更在意这件事里的具体取舍，而不是把每句话都压成同样的长度。可以自然问一句，也可以在自己的想法上停下来。'
        self.assertGreater(len(body), 35)
        raw = json.dumps({'schema_version':3,'behavior_intent':'respond','messages':[
            {'content':body,'content_kind':'chat','cancel_if_new_input':False}]}, ensure_ascii=False)
        self.assertEqual(validate_reply(raw, model['_reply_mode'], model['_expression_snapshot']).text, body)
        invalid = json.loads(raw); invalid['extra'] = True
        with self.assertRaises(ReplySchemaError):
            validate_reply(json.dumps(invalid), model['_reply_mode'], model['_expression_snapshot'])


class ArtificialPackages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=DEFAULT_HOME/'tmp', prefix='contract-language-artificial-unit-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)/'packages'
        setting = patch.object(language, 'EVIDENCE_PARENT', self.parent)
        setting.start(); self.addCleanup(setting.stop)

    def test_prepare_verify_are_readonly_offline_and_have_no_private_or_generation_dependency(self):
        with patch('persona_runtime.config.ConfigStore.load', side_effect=AssertionError('private-config-forbidden')), \
             patch('persona_runtime.chat.ChatCompletionsClient.complete', side_effect=AssertionError('generation-forbidden')):
            root = language.prepare()
            before = {p.name:p.read_bytes() for p in root.iterdir()}
            result = language.verify(root)
            self.assertEqual(result['cases'], 3)
            self.assertEqual(result['real_model_calls'], 0)
            self.assertFalse(result['private_data_read'])
            self.assertEqual(before, {p.name:p.read_bytes() for p in root.iterdir()})

    def test_prepare_has_no_output_override_real_action_or_outside_package_verification(self):
        with self.assertRaises(TypeError): language.prepare(Path(self.temp.name)/'arbitrary')
        with self.assertRaisesRegex(Exception, 'private_path_outside_root'):
            language.verify(Path(self.temp.name)/'outside')
        with patch('sys.argv', ['companion_contract_language.py','run']), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error: language.main()
        self.assertEqual(error.exception.code, 2)

    def test_tampered_or_resigned_nonfixture_material_cannot_be_verified(self):
        root = language.prepare(); file = root/'O01.json'
        value = json.loads(file.read_text('utf-8')); value['variant']['messages'][-1]['content'] = 'changed'
        atomic_json(file, value)
        with self.assertRaisesRegex(Exception, 'contract_language_artifact_changed'): language.verify(root)
        original = language.artificial_cases()[0]['request']
        messages = deepcopy(original['messages']); messages[-1]['content'] = 'Different artificial material'
        altered = language.transform_request(order._request(messages, original['model']))
        value.update(altered); atomic_json(file, value)
        manifest = json.loads((root/'manifest.json').read_text('utf-8'))
        manifest['cases'][0]['sha256'] = language._hash_file(file)
        atomic_json(root/'manifest.json', manifest)
        atomic_json(root/'freeze.json', {'version':language.VERSION,'manifest_hash':fingerprint(manifest)})
        with self.assertRaisesRegex(Exception, 'contract_language_not_builtin_artificial_case'):
            language.verify(root)

    def test_network_guard_blocks_dns_and_connections(self):
        import socket
        with language.offline_guard():
            with self.assertRaises(Exception): socket.getaddrinfo('offline.invalid', 443)
            with self.assertRaises(Exception): socket.create_connection(('offline.invalid',443))


if __name__ == '__main__':
    unittest.main()
