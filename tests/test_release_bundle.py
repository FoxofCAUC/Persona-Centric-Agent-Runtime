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

    def test_desktop_kit_includes_the_same_frozen_prompt_for_runtime_and_picker(self):
        import json
        from zipfile import ZipFile
        from persona_runtime.import_prompts import BUNDLE_PATH, BUNDLE_SHA256
        target = self.root / "desktop.zip"
        build(target, profile="scene-desktop")
        with ZipFile(target) as kit:
            manifest = json.loads(kit.read("desktop-kit.json"))
            asset = manifest["import_prompt_bundle"]
            self.assertEqual(kit.read(asset["file"]), BUNDLE_PATH.read_bytes())
            self.assertEqual(kit.read(asset["file"]), kit.read("persona_runtime/import_prompt_assets/" + BUNDLE_PATH.name))
            self.assertEqual(manifest["files"][asset["file"]]["sha256"], BUNDLE_SHA256)
            self.assertEqual(asset["sha256"], BUNDLE_SHA256)

    def test_revisioned_workbench_includes_readme_links_and_artwork(self):
        import json
        import re
        from zipfile import ZipFile

        target = self.root / "workbench.zip"
        revision = "a" * 40
        build(target, profile="scene-desktop", source_revision=revision)
        with ZipFile(target) as kit:
            manifest = json.loads(kit.read("desktop-kit.json"))
            self.assertEqual(manifest["source_revision"], revision)
            for verifier in ("scripts/verify_m8d_migration.py", "scripts/verify_m761_migration.py"):
                self.assertIn(verifier, manifest["files"])
            readme = kit.read("README.md").decode("utf-8")
            links = re.findall(r'\]\(([^)]+)\)|(?:src|href)="([^"]+)"', readme)
            for link, asset in links:
                path = (link or asset).partition("#")[0]
                if path and "://" not in path:
                    self.assertIn(path, manifest["files"])

    def test_candidate_binds_the_tree_and_includes_the_desktop_evidence_tool(self):
        import json
        from zipfile import ZipFile
        target=self.root/"candidate.zip"
        build(target,candidate_base_revision="b"*40)
        with ZipFile(target) as kit:
            manifest=json.loads(kit.read("desktop-kit.json"))
            self.assertIsNone(manifest["source_revision"])
            self.assertFalse(manifest["candidate_source"]["published"])
            self.assertIn("assets/readme/superfish-vision.webp",manifest["files"])
            self.assertIn("scripts/pcar_desktop_turn_evidence.py",manifest["files"])
            self.assertIn("scripts/private_copy_workbench.py",manifest["files"])

    def test_release_requires_revision_models_and_public_fixture_before_writing(self):
        target=self.root/'release.zip'
        with self.assertRaisesRegex(ValueError,'提交编号'):build(target,release_tag='v0.1')
        self.assertFalse(target.exists())

    def test_build_refuses_a_changed_prompt_bundle_before_creating_the_zip(self):
        from unittest.mock import patch
        prompt = self.root / "edited.json"
        prompt.write_bytes(b"{}")
        output = self.root / "rejected.zip"
        with patch("persona_runtime.import_prompts.BUNDLE_PATH", prompt), self.assertRaisesRegex(ValueError, "提示词"):
            build(output, profile="scene-desktop")
        self.assertFalse(output.exists())

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
