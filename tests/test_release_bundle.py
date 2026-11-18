"""Publication guards for a complete, data-free desktop release."""
from pathlib import Path
import tempfile
import unittest
from persona_runtime.paths import RuntimePaths
from scripts.build_desktop_bundle import build

class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='release-',dir=root);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_release_requires_revision_models_and_public_fixture_before_writing(self):
        target=self.root/'release.zip'
        with self.assertRaisesRegex(ValueError,'提交编号'):build(target,release_tag='v0.1')
        self.assertFalse(target.exists())

    def test_unapproved_migration_package_cannot_become_a_public_sample(self):
        fixture=self.root/'user-business.zip';fixture.write_bytes(b'artificial secret-bearing package used only for this rejection test')
        target=self.root/'release.zip'
        with self.assertRaisesRegex(ValueError,'不能发布用户业务包'):build(target,artificial_fixture=fixture)
        self.assertFalse(target.exists())

    def test_release_fixture_is_pinned_to_the_target_release(self):
        import hashlib
        from unittest.mock import patch

        legacy = self.root/'legacy.zip'
        legacy.write_bytes(b'ID-PUBLIC-SAMPLE-27')
        digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
        approved = {'v0.1':digest, 'v0.2':'0'*64}
        target = self.root/'release.zip'
        with patch('scripts.build_desktop_bundle.PUBLIC_FIXTURE_SHA256',approved):
            with self.assertRaisesRegex(ValueError,'本版本核准'):
                build(target,self.root,release_tag='v0.2',source_revision='1'*40,artificial_fixture=legacy)
        self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
