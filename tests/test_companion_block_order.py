"""Artificial-only checks for the single prompt-block-order transform."""
from collections import Counter
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import companion_block_order as order
from persona_runtime.archive import fingerprint
from persona_runtime.companion import MINIMAL_INSTRUCTION, choose
from persona_runtime.config import atomic_json
from persona_runtime.paths import DEFAULT_HOME
from persona_runtime.provider_compat import build_request


class RequestTransform(unittest.TestCase):
    def request(self):
        return deepcopy(order.artificial_cases()[0]['request'])

    def resign(self, record):
        wire, transport = build_request(record['model'], record['messages'])
        record.update(wire=wire, transport=transport, wire_hash=fingerprint(wire),
                      messages_hash=fingerprint(record['messages']), model_hash=fingerprint(record['model']))

    def test_only_exact_block_order_changes_and_source_object_is_untouched(self):
        record = self.request()
        # Public snapshot values not consumed by build_request are still frozen.
        record['model']['sample_seed'] = 'fixed-artificial-seed'
        self.resign(record)
        before = deepcopy(record)
        value = order.transform_request(record)
        self.assertEqual(record, before)
        self.assertEqual(value['control'], before)
        control, variant = value['control'], value['variant']
        protocol = order.prompt_contract('natural_structured', record['model']['_expression_snapshot'])
        material = control['messages'][0]['content'][len(MINIMAL_INSTRUCTION):-len(protocol)]
        self.assertEqual(variant['messages'][0]['content'], protocol + MINIMAL_INSTRUCTION + material)
        self.assertEqual(variant['messages'][1:], control['messages'][1:])
        self.assertEqual(variant['model'], control['model'])
        self.assertEqual(variant['model']['sample_seed'], 'fixed-artificial-seed')
        self.assertEqual({k:v for k,v in variant['wire'].items() if k != 'messages'},
                         {k:v for k,v in control['wire'].items() if k != 'messages'})
        self.assertEqual(Counter(control['messages'][0]['content']), Counter(variant['messages'][0]['content']))
        self.assertTrue(all(value['diagnostic']['invariants'].values()))
        variant['messages'][-1]['content'] = 'changed output copy'
        self.assertEqual(record, before)

    def test_ordered_correction_and_nonempty_history_survive_unchanged(self):
        cases = order.artificial_cases()
        correction = cases[1]['request']
        current = correction['messages'][-1]['content']
        rows = json.loads(current.split('\n', 1)[1])
        self.assertEqual([r['event_id'] for r in rows], ['artificial-member-01', 'artificial-member-02'])
        self.assertEqual(rows[-1]['text'], '改口，今天不想动它了，就随便聊聊吧。')
        self.assertEqual(order.transform_request(correction)['variant']['messages'][-1]['content'], current)
        history = cases[2]['request']['messages'][1:-1]
        self.assertEqual(len(history), 2)
        self.assertEqual(order.transform_request(cases[2]['request'])['variant']['messages'][1:-1], history)

    def test_credentials_are_rejected_in_nested_evidence_and_urls(self):
        mutations = [lambda r:r['model'].update(encrypted_key=''),
                     lambda r:r['wire'].update(headers={'Authorization':'artificial-placeholder'}),
                     lambda r:r['model'].update(base_url='https://user:pass@offline.invalid/v1'),
                     lambda r:r['model'].update(base_url='https://offline.invalid/v1?api-key=placeholder')]
        for change in mutations:
            with self.subTest(change=change):
                record = self.request(); change(record)
                with self.assertRaisesRegex(Exception, 'block_order_credentials_forbidden'):
                    order.transform_request(record)

    def test_unknown_profiles_and_nonminimal_request_fail_closed(self):
        mutations = [lambda r:r['model'].update(_companion_profile=choose('current')),
                     lambda r:r['model'].update(_generation_profile={'version':'unknown'}),
                     lambda r:r['model'].update(_unknown_profile={'version':1}),
                     lambda r:r['model']['_expression_snapshot'].update(version=99)]
        for change in mutations:
            record=self.request();change(record)
            with self.assertRaises(Exception):order.transform_request(record)

    def test_text_boundaries_cannot_be_repaired_or_guessed(self):
        for mutate in (lambda text:'prefix'+text, lambda text:text+'tail'):
            record=self.request();record['messages'][0]['content']=mutate(record['messages'][0]['content'])
            self.resign(record)
            with self.assertRaisesRegex(Exception,'reference_minimal_boundaries_mismatch'):
                order.transform_request(record)

    def test_stored_wire_message_and_model_hashes_are_checked(self):
        mutations=[lambda r:r.update(messages_hash='bad'), lambda r:r.update(wire_hash='bad'),
                   lambda r:r['wire'].update(stream=False), lambda r:r.update(model_hash='bad'),
                   lambda r:r['transport'].update(profile='unexpected')]
        for change in mutations:
            record=self.request();change(record)
            with self.assertRaises(Exception):order.transform_request(record)


class ArtificialPackages(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=DEFAULT_HOME/'tmp',prefix='block-order-artificial-unit-')
        self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name)/'packages'
        setting=patch.object(order,'EVIDENCE_PARENT',self.parent)
        setting.start();self.addCleanup(setting.stop)

    def test_prepare_and_verify_need_no_private_config_generation_or_network(self):
        with patch('persona_runtime.config.ConfigStore.load', side_effect=AssertionError('formal_config_read')), \
             patch('persona_runtime.chat.ChatCompletionsClient.complete', side_effect=AssertionError('generation_forbidden')):
            root=order.prepare()
            before={p.name:p.read_bytes() for p in root.iterdir()}
            result=order.verify(root)
            self.assertEqual(result['cases'],3)
            self.assertEqual(result['real_model_calls'],0)
            self.assertFalse(result['private_data_read'])
            self.assertEqual(result['naturalness'],'not_evaluated')
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
        manifest=json.loads((root/'manifest.json').read_text('utf-8'))
        self.assertFalse(manifest['current_provider_config_loaded'])
        self.assertEqual(manifest['network'],'blocked')

    def test_prepare_has_no_arbitrary_output_and_verify_rejects_outside_scope(self):
        with self.assertRaises(TypeError):order.prepare(Path(self.temp.name)/'outside')
        with self.assertRaisesRegex(Exception,'private_path_outside_root'):
            order.verify(Path(self.temp.name)/'outside')
        with patch('sys.argv',['companion_block_order.py','run']),redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:order.main()
        self.assertEqual(error.exception.code,2)

    def test_artifact_tamper_is_rejected(self):
        root=order.prepare();case=root/'O01.json'
        value=json.loads(case.read_text('utf-8'));value['variant']['messages'][-1]['content']='changed'
        atomic_json(case,value)
        with self.assertRaisesRegex(Exception,'block_order_artifact_changed'):order.verify(root)

    def test_resigned_other_material_cannot_masquerade_as_builtin_fixture(self):
        root=order.prepare();case=root/'O01.json'
        value=json.loads(case.read_text('utf-8'))
        model=value['control']['model']
        messages=deepcopy(value['control']['messages']);messages[-1]['content']='Unrelated replacement material'
        changed=order.transform_request(order._request(messages,model))
        value.update(changed);atomic_json(case,value)
        manifest=json.loads((root/'manifest.json').read_text('utf-8'))
        manifest['cases'][0]['sha256']=order._hash_file(case)
        atomic_json(root/'manifest.json',manifest)
        atomic_json(root/'freeze.json',{'version':order.VERSION,'manifest_hash':fingerprint(manifest)})
        with self.assertRaisesRegex(Exception,'block_order_not_builtin_artificial_case'):
            order.verify(root)

    def test_network_guard_blocks_dns_and_connection_even_during_future_extension(self):
        import socket
        with order.offline_guard():
            with self.assertRaisesRegex(Exception,'block_order_network_disabled'):
                socket.getaddrinfo('offline.invalid',443)
            with self.assertRaisesRegex(Exception,'block_order_network_disabled'):
                socket.create_connection(('offline.invalid',443))


if __name__=='__main__':unittest.main()
