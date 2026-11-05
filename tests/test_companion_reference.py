import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from experiments.companion_reference import ASTRBOT_TAG, PATCHES, UPSTREAM_COMMIT, source_status


class ReferenceSourceProtection(unittest.TestCase):
    def test_missing_sources_are_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            result = source_status(Path(folder))
            self.assertFalse(result['source_ready'])
            self.assertFalse(result['text_patches_applied'])

    def test_changed_source_and_wrong_version_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, ref in [('astrbot', ASTRBOT_TAG), ('upstream', UPSTREAM_COMMIT)]:
                (root / name).mkdir()
                (root / name / 'file.py').write_bytes(b'original')
                sha = hashlib.sha1(b'blob 8\0original').hexdigest()
                (root / (name + '-files.json')).write_text(json.dumps({
                    'ref': ref, 'files': [{'path': 'file.py', 'sha': sha}]}))
            self.assertTrue(source_status(root)['source_ready'])
            (root / 'upstream/file.py').write_text('changed')
            result = source_status(root)
            self.assertFalse(result['source_ready'])
            self.assertIn('upstream:source_changed:file.py', result['failures'])
            manifest = root / 'astrbot-files.json'
            value = json.loads(manifest.read_text())
            value['ref'] = 'main'
            manifest.write_text(json.dumps(value))
            self.assertIn('astrbot_source_version_mismatch', source_status(root)['failures'])


class IsolatedUpstreamTextFlow(unittest.TestCase):
    def test_real_upstream_code_with_fake_provider_and_capture(self):
        value = os.environ.get('PCAR_REFERENCE_ROOT')
        if not value:
            self.skipTest('set PCAR_REFERENCE_ROOT to the separately prepared reference instance')
        root = Path(value)
        python = root / '.venv/Scripts/python.exe'
        if not python.is_file():
            self.skipTest('separate reference Python environment unavailable')
        status = source_status(root)
        self.assertTrue(status['source_ready'], status['failures'])
        self.assertTrue(status['text_patches_applied'])
        with tempfile.TemporaryDirectory(dir=root) as folder:
            output = Path(folder) / 'result.json'
            script = Path(__file__).resolve().parents[1] / 'experiments/companion_reference.py'
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'PYTHONWARNINGS': 'ignore::SyntaxWarning', 'TEMP': str(root / 'tmp'), 'TMP': str(root / 'tmp')}
            result = subprocess.run([str(python), '-B', str(script), 'simulate', '--root', str(root),
                '--output', str(output)], env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(evidence['generation_kind'], 'simulated_callback')
            self.assertEqual(evidence['provider_dispatches'], 1)
            self.assertEqual(evidence['messages'][0]['role'], 'system')
            self.assertIn('# Persona Instructions', evidence['messages'][0]['content'])
            self.assertIn('inbound_hooks', evidence['stages'])
            self.assertIn('agent_and_response_hooks', evidence['stages'])
            self.assertIn('result_decorate_hooks', evidence['stages'])
            self.assertEqual(evidence['stages'][-1], 'respond_capture')
            self.assertNotEqual(evidence['raw_text'], evidence['after_response_hook'])
            # A single bubble never reads the upper interval bound. Exercise
            # the real second send, not only provider/hook completion.
            self.assertGreaterEqual(len(evidence['captured_messages']), 2)
            self.assertEqual([m['role'] for m in evidence['native_history']], ['user', 'assistant'])
            # Upstream stores the runner message, before display hook cleanup.
            self.assertEqual(evidence['native_history'][-1]['content'], evidence['raw_text'])
            # AstrBot's get_plain_text joins Plain components with a space;
            # separate delivery bubbles remain separately captured above.
            delivered = ' '.join(c['text'] for message in evidence['captured_messages'] for c in message if c['type'] == 'Plain')
            self.assertEqual(delivered, evidence['processed_text'])
            self.assertFalse(evidence['actual_qq'])
            self.assertEqual(evidence['semantic_quality'], 'not_evaluated')


if __name__ == '__main__':
    unittest.main()
