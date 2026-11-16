"""Exact public artwork approvals never exempt private or changed content."""
from pathlib import Path
import hashlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime import private_personas as privacy
from persona_runtime.paths import RuntimePaths


class ReadmeAssetPublicationTests(unittest.TestCase):
    def setUp(self):
        parent = RuntimePaths.from_environment().home / 'tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='readme-publication-artificial-', dir=parent)
        self.root = Path(self.temp.name)
        self.assertTrue(self.root.resolve().is_relative_to(parent.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.paths = RuntimePaths(self.root / 'home')
        registry = self.paths.home / 'private/personas'
        folder = registry / 'fixture'
        folder.mkdir(parents=True)
        archive = b'Protected artificial source only, with a persistent identity.'
        (folder / 'source-archive.md').write_bytes(archive)
        (folder / 'initial-relationship.txt').write_text('Artificial relationship', encoding='utf-8')
        record = {'id': 'fixture', 'package_sha256': '0' * 64,
                  'archive_sha256': hashlib.sha256(archive).hexdigest()}

        class Registry:
            def __init__(obj, paths):
                obj.root = registry

            def load(obj, required):
                return {'records': [record]}

            def package(obj, record):
                return {'persona_spec': {'name': 'PrivateFixtureName2026'}}

        self.registry_patch = patch.object(privacy, 'PrivatePersonas', Registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.path = 'assets/readme/approved.png'
        self.data = b'\x89PNG\r\n\x00Public artificial image data.'
        self.mapping_patch = patch.dict(privacy._PUBLIC_README_IMAGE_HASHES,
                                        {self.path: privacy.sha(self.data)}, clear=True)
        self.mapping_patch.start()
        self.addCleanup(self.mapping_patch.stop)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def write(self, path, data):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def scan(self, **kwargs):
        return privacy.publication_scan(self.paths, self.repo, **kwargs)['passed']

    def test_exact_path_and_hash_permit_public_image(self):
        self.write(self.path, self.data)
        self.assertTrue(self.scan())
        self.git('add', self.path)
        self.assertTrue(self.scan(staged=True))

    def test_changed_bytes_remain_blocked(self):
        self.write(self.path, self.data + b'changed')
        self.assertFalse(self.scan())

    def test_identical_image_at_another_path_remains_blocked(self):
        self.write('assets/readme/copied.png', self.data)
        self.assertFalse(self.scan())

    def test_runtime_binary_remains_blocked(self):
        self.write('assets/readme/state.db', b'SQLite\x00artificial')
        self.assertFalse(self.scan())

    def test_private_content_overrides_hash_exception(self):
        data = self.data + b'PrivateFixtureName2026'
        privacy._PUBLIC_README_IMAGE_HASHES[self.path] = privacy.sha(data)
        self.write(self.path, data)
        self.assertFalse(self.scan())

    def test_staged_tampering_not_hidden_by_clean_worktree(self):
        self.write(self.path, self.data + b'changed')
        self.git('add', self.path)
        self.write(self.path, self.data)
        self.assertFalse(self.scan(staged=True))

    def test_private_path_stays_blocked_even_with_matching_hash(self):
        path = 'private/approved.png'
        privacy._PUBLIC_README_IMAGE_HASHES[path] = privacy.sha(self.data)
        self.write(path, self.data)
        self.assertFalse(self.scan())

    def test_restored_image_does_not_hide_unapproved_outgoing_history(self):
        def commit(message):
            self.git('add', '--all')
            self.git('-c', 'user.name=Publication Fixture', '-c',
                     'user.email=fixture@example.invalid', 'commit', '-qm', message)
            return self.git('rev-parse', 'HEAD')

        self.write('safe.txt', b'Public fixture')
        base = commit('base')
        self.git('update-ref', 'refs/remotes/origin/main', base)
        self.write(self.path, self.data + b'unapproved historical change')
        commit('unapproved image')
        self.write(self.path, self.data)
        commit('restore approved image')
        self.assertTrue(self.scan(staged=True))
        self.assertFalse(self.scan(staged=True, base=base))
