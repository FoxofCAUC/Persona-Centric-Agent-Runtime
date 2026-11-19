"""Released prompts travel as one verified, data-free file. Model calls use local doubles."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime import import_prompts as prompts
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.corpus_files import digest
from persona_runtime.import_card_job import CardJobs
from persona_runtime.import_library import ImportLibrary, encode
from persona_runtime.import_profile_workspace import ProfileWorkspace
from persona_runtime.paths import RuntimePaths


class LocalModel:
    def __init__(self):
        self.requests = []

    async def complete(self, model, request, *, save_raw):
        self.requests.append(request)
        value = json.loads(request["messages"][1]["content"])
        if "target" in value:
            role = value["target"]
            message = next(r for r in value["records"] if r["speaker"] == role and r["kind"] == "core")
            ref = {k: message[k] for k in ("id", "start", "end")}
            result = {"target": role, "batch": value["batch"], "observations": [
                {"field": "expression_style", "value": "17", "scope": "19", "kind": "pattern", "evidence": [ref]}]}
        else:
            result = {r: {"common": {"value": "18", "sources": [r + "1"]}} for r in ("A", "B")}
        response = {"choices": [{"finish_reason": "stop", "message": {"content": encode(result)}}]}
        save_raw(encode(response).encode("utf8"), http_status=200, diagnostics={})


class ImportPromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = RuntimePaths(Path(directory.name) / "instance")
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.library = ImportLibrary(self.paths)
        self.transport = LocalModel()
        self.profiles = ProfileWorkspace(self.library, self.config, self.transport)
        self.cards = CardJobs(self.library, self.config, self.transport)
        self.bundle = prompts.BUNDLE_PATH.read_bytes()

    def test_shipped_contracts_are_byte_identical_to_reviewed_generic_files(self):
        self.assertEqual(digest(self.bundle), prompts.BUNDLE_SHA256)
        self.assertEqual(set(json.loads(self.bundle)), {"format", "profile", "card"})
        for kind, (_, keys, sha) in prompts.CONTRACTS.items():
            raw, release = prompts.contract_source(self.library, kind)
            self.assertEqual(digest(raw), sha)
            self.assertEqual(tuple(json.loads(raw)), keys)
            self.assertEqual(release, prompts.BUNDLE_SHA256)
        self.assertFalse(self.library.root.exists())
        identity, _ = self.profiles.engine.settings()
        self.assertEqual(identity["base_url"], "https://api.deepseek.com")
        self.assertEqual(identity["max_tokens"], 8192)
        self.assertEqual(self.config.load()["model"]["max_tokens"], 1024)

    def test_file_import_persists_only_the_release_bundle(self):
        file = self.paths.home.parent / "release.json"
        file.write_bytes(json.dumps(json.loads(self.bundle), ensure_ascii=False).encode("utf-8-sig"))
        self.assertEqual(prompts.prompt_bundle_status(self.library)["source"], "builtin")
        self.assertEqual(prompts.import_prompt_file(self.library, file)["source"], "file")
        installed = self.library.root / prompts.SELECTION
        self.assertEqual(installed.read_bytes(), self.bundle)
        reopened = ImportLibrary(self.paths)
        self.assertTrue(prompts.prompt_bundle_status(reopened)["active"])
        self.assertEqual(prompts.import_prompt_file(reopened, file)["sha256"], prompts.BUNDLE_SHA256)
        self.assertEqual(list(self.library.root.iterdir()), [installed])

    def test_modified_extra_and_invalid_files_do_not_replace_selection(self):
        prompts.install_prompt_bundle(self.library, self.bundle)
        data = json.loads(self.bundle)
        for changed in (data | {"request": 1}, data | {"profile": data["profile"] | {"extract_system": "1"}}):
            with self.assertRaises(ValueError):
                prompts.install_prompt_bundle(self.library, encode(changed).encode())
        for raw in (b'{"a":1,"a":2}', b"1" * (prompts.MAX_FILE_BYTES + 1)):
            with self.assertRaises(ValueError):
                prompts.install_prompt_bundle(self.library, raw)
        self.assertEqual((self.library.root / prompts.SELECTION).read_bytes(), self.bundle)
        self.assertFalse(self.transport.requests)

    def test_tampered_bundled_or_selected_template_fails_closed_and_can_be_reimported(self):
        damaged = self.paths.home.parent / "damaged.json"
        damaged.write_bytes(b"{}")
        with patch.object(prompts, "BUNDLE_PATH", damaged):
            self.assertEqual(prompts.prompt_bundle_status(self.library)["source"], "invalid")
            with self.assertRaises(ValueError):
                self.profiles.engine.settings()
            prompts.install_prompt_bundle(self.library, self.bundle)
            self.assertTrue(prompts.prompt_bundle_status(self.library)["active"])
        (self.library.root / prompts.SELECTION).write_bytes(b"{}")
        with self.assertRaises(ValueError):
            prompts.contract_source(self.library, "card")
        self.assertEqual(prompts.prompt_bundle_status(self.library)["source"], "invalid")

    def test_explicit_private_scope_and_withdrawal_never_inherit_global_authority(self):
        self.config.update({"model": {"base_url": "https://api.deepseek.com/v1"}})
        self.library.root.mkdir(parents=True)
        original = []
        for kind, (file, _, _) in prompts.CONTRACTS.items():
            raw, release = prompts.contract_source(self.library, kind)
            self.assertIsNotNone(release)
            original.append((self.library.root / file, raw))
        plan = {"settings": self.profiles.engine.settings()[0]}
        for path, raw in original:
            path.write_bytes(raw)
        private_id, _ = self.profiles.engine.settings()
        self.assertNotIn("prompt_release", private_id)
        self.assertEqual(prompts.prompt_bundle_status(self.library)["source"], "private")
        with self.assertRaises(ValueError):
            prompts.install_prompt_bundle(self.library, self.bundle)
        self.assertEqual(self.profiles.engine._permission("1", plan, private_id["contract_sha256"],
                                                         {"stage": "extract"}), (False, False))
        (self.library.root / "contract-v2.json").unlink()
        atomic_json(self.library.root / "approval-v2.json", {"decision": "withdrawn"})
        identity, _ = self.profiles.engine.settings()
        self.assertFalse(identity["configured"])

    def test_released_merge_uses_only_the_reviewed_merge_template(self):
        engine = self.profiles.engine
        settings, _ = engine.settings()
        source = {"id": "1", "batch": "31", "role": "A", "scope": "3", "field": "core_traits",
                  "kind": "pattern", "value": "4", "evidence": [{"id": "1","start": 0,"end": 1,"quote":"4"}]}
        plan = {"settings": settings, "id": "9", "source_id": "8"}
        merge, = engine.preview_merge("7", plan, [source, source | {"id":"2","batch":"32"}])
        self.assertEqual(merge["wire"]["messages"][0]["content"], json.loads(self.bundle)["profile"]["merge_system"])
        self.assertEqual(engine._permission("7", plan, settings["contract_sha256"], merge), (True, True))
        self.assertEqual(engine._permission("7", plan, settings["contract_sha256"], merge | {"stage":"compose"}),
                         (False, False))

    async def test_fresh_instance_both_roles_and_one_card_after_user_confirmation(self):
        self.config.update({"model": {"base_url": "https://api.deepseek.com/v1", "api_key": "123"}})
        data = self.library.add(b"## X\n301\n## Y\n302\n")
        data = self.profiles.plan(data["id"], data["revision"], data["mapping"], {})
        ident, pid = data["id"], data["plan"]["id"]
        with self.assertRaises(ValueError):
            self.profiles.start(ident, data["revision"], pid, False)
        self.assertEqual(self.transport.requests, [])
        self.profiles.start(ident, data["revision"], pid, True)
        await self.profiles.tasks[ident]
        current = self.library.status(ident)
        self.assertEqual(current["status"], "ready")
        with self.assertRaises(ValueError):
            self.cards.start(ident, current["revision"], pid, False)
        self.assertEqual(len(self.transport.requests), 2)
        self.cards.start(ident, current["revision"], pid, True)
        await self.cards.tasks[ident]
        self.assertEqual(self.cards.status(ident)["state"], "ready")
        self.assertEqual(len(self.transport.requests), 3)
        self.assertTrue(all(r["model"] == prompts.RELEASE_MODEL and r["max_tokens"] == prompts.OUTPUT_BUDGET
                            for r in self.transport.requests))
        card_contract = json.loads(prompts.contract_source(self.library, "card")[0])
        self.assertEqual(self.transport.requests[-1]["messages"][0]["content"], card_contract["system"])
        self.cards.start(ident, current["revision"], pid, True)
        self.assertEqual(len(self.transport.requests), 3)
