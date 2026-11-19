"""Exercise first-run writes and version checks without external model transport."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.character import CharacterService, empty_character
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalAPIError
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import ProfileService
from persona_runtime.service import RuntimeService
from persona_runtime.workbench_conversation import WorkbenchConversation
from persona_runtime.workbench_setup import OFFICIAL_URL, WorkbenchSetup


class SetupBoundaryTests(unittest.TestCase):
    def setUp(self):
        parent = RuntimePaths.from_environment().home / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(directory.cleanup)
        self.paths = RuntimePaths(Path(directory.name))
        ConfigStore(self.paths).initialize()
        chat = WorkbenchConversation(self.paths, lambda: True, lambda: True)
        chat.store.initialize()
        self.service = RuntimeService(chat.store)
        self.characters = CharacterService(chat.store)
        self.profiles = ProfileService(chat.store)
        self.ui = WorkbenchSetup(chat, lambda: {"label": ""})
        self.writes = []
        self.client = chat.client
        manager = patch.object(self.client, "manage", side_effect=self.manage)
        caller = patch.object(self.client, "call", side_effect=self.local_call)
        manager.start()
        caller.start()
        self.addCleanup(manager.stop)
        self.addCleanup(caller.stop)

    def manage(self, action, **args):
        self.writes.append(action)
        commands = {
            "character.import": self.characters.import_package, "character.show": self.characters.show,
            "character.update": self.characters.update, "persona.use": self.service.use_persona,
            "profile.update": self.profiles.update, "profile.restore": self.profiles.restore,
        }
        try:
            return commands[action](**args)
        except RuntimeErrorBase as error:
            raise LocalAPIError(str(error), 400) from None

    def local_call(self, method, path, payload=None, timeout=8):
        if method == "GET" and path == "/v1/health":
            return {}
        self.writes.append((method, path))
        if method == "POST" and path == "/v1/config":
            return self.ui.config.update(payload)
        raise AssertionError("No model transport for setup tests")

    def call(self, action, **args):
        result = self.ui.call(action, args)
        self.assertTrue(result["ok"], result)
        return result["value"]

    def create(self, package_token=None, request_id=None, relationship=""):
        return self.call("create_role", spec=generic_persona().to_dict(), relationship=relationship,
                         package_token=package_token, request_id=request_id or identifier())

    def test_model_settings_keep_replace_and_clear_without_external_requests(self):
        self.assertFalse(self.call("connection")["configured"])
        name = self.call("connection")["choices"][0]
        saved = self.call("save_connection", name=name, key="00007", key_mode="replace")
        self.assertTrue(saved["has_key"])
        self.assertNotIn("00007", json.dumps(saved))
        ciphertext = self.ui.config.load()["model"]["encrypted_key"]
        self.assertTrue(ciphertext and ciphertext != "00007")
        self.assertTrue(self.call("save_connection", name=name, key_mode="keep")["configured"])
        self.assertEqual(self.ui.config.load()["model"]["encrypted_key"], ciphertext)
        self.assertEqual(self.ui.call("test_connection", {"request_id": identifier(), "confirmed": False}),
                         {"ok": False, "error": "test_confirmation_required"})
        self.assertFalse(self.call("save_connection", name=name, key_mode="clear")["configured"])
        self.assertEqual(self.writes, [("POST", "/v1/config")] * 3)

    def test_legacy_key_is_never_repointed_to_official_endpoint(self):
        self.ui.config.update({"model": {"base_url": "https://example.invalid", "api_key": "00006"}})
        state = self.call("connection")
        self.assertTrue(state["legacy"])
        self.assertFalse(state["key_reusable"])
        self.assertNotIn("example.invalid", json.dumps(state))
        code = self.ui.call("save_connection", {"name": state["choices"][0], "key_mode": "keep"})
        self.assertEqual(code, {"ok": False, "error": "replace_legacy_key"})
        self.assertEqual(self.writes, [])
        self.call("save_connection", name=state["choices"][0], key="00007", key_mode="replace")
        self.assertEqual(self.ui.config.public()["model"]["base_url"], OFFICIAL_URL)

    def test_blank_and_package_creation_have_stable_distinct_identities(self):
        data = empty_character()
        data["age"] |= {"policy": "fixed", "fixed_age": 12}
        draft = self.call("preview_package", text=json.dumps({"format": "pcar_character_v1",
                          "persona_spec": generic_persona().to_dict(), "character": data}))
        self.assertEqual(draft["materials"]["attributes"], 1)
        self.assertEqual(self.service.list_personas(), [])
        request = identifier()
        introduced = self.create(draft["token"], request, relationship="01")
        self.assertEqual(self.create(draft["token"], request, relationship="01"), introduced)
        self.assertEqual(self.ui.call("create_role", {
            "spec": generic_persona().to_dict(), "relationship": "02",
            "package_token": draft["token"], "request_id": request,
        }), {"ok": False, "error": "submission_conflict"})
        another = self.create()
        self.assertNotEqual(another["id"], introduced["id"])
        self.assertFalse(self.call("person", persona_id=introduced["id"])["selected"])
        self.assertEqual(self.call("person", persona_id=introduced["id"])["versions"]["relationship"]["data"],
                         {"description": "01"})
        self.call("select_person", persona_id=another["id"])
        self.assertEqual(self.call("creation_status", request_id=request), introduced)
        self.assertEqual(self.characters.current(introduced["id"])["data"]["age"]["fixed_age"], 12)

    def test_discarded_draft_and_invalid_input_do_not_create(self):
        self.assertEqual(self.ui.call("preview_package", {"text": "{}"}),
                         {"ok": False, "error": "invalid_package"})
        draft = self.call("preview_package", text=json.dumps(generic_persona().to_dict()))
        self.call("discard_package", token=draft["token"])
        result = self.ui.call("create_role", {"spec": draft["spec"], "relationship": "",
                              "package_token": draft["token"], "request_id": identifier()})
        self.assertEqual(result, {"ok": False, "error": "package_changed"})
        self.assertEqual(self.service.list_personas(), [])
        self.assertEqual(self.ui.call("read_package", {"path": str(self.paths.home)}),
                         {"ok": False, "error": "invalid_request"})

    def test_edit_conflict_and_restore_bind_persona_and_kind(self):
        first, second = self.create()["id"], self.create()["id"]
        self.call("select_person", persona_id=first)
        base = self.call("person", persona_id=first)["versions"]["expression"]
        args = {"persona_id": first, "kind": "expression", "expected": base["id"]}
        saved = self.call("save_profile", **args, data={"style": "101"}, request_id=identifier())
        conflict = self.ui.call("save_profile", args | {"data": {"style": "102"}, "request_id": identifier()})
        self.assertEqual(conflict, {"ok": False, "error": "profile_version_conflict"})
        foreign = self.ui.call("version_detail", {"persona_id": second, "kind": "expression", "version_id": base["id"]})
        self.assertEqual(foreign, {"ok": False, "error": "version_not_found"})
        detail = self.call("version_detail", persona_id=first, kind="expression", version_id=base["id"])
        self.assertEqual(detail["differences"]["style"]["current"], "101")
        self.assertTrue(detail["can_restore"])
        self.call("restore_version", persona_id=first, kind="expression", version_id=base["id"],
                  expected=saved["id"], request_id=identifier())
        self.assertEqual(self.call("person", persona_id=first)["spec"]["expression_style"],
                         generic_persona().expression_style)

    def test_append_replay_preserves_scope_and_other_material(self):
        first, second = self.create()["id"], self.create()["id"]
        self.call("select_person", persona_id=first)
        expected = self.call("person", persona_id=first)["materials"]["id"]
        entry = {"title": "10", "category": "20", "text": "30", "keywords": ["40"],
                 "enabled": False, "visibility": "creator_only"}
        params = {"persona_id": first, "group": "lorebook", "fields": entry, "expected": expected,
                  "request_id": identifier()}
        original = self.call("append_material", **params)
        self.assertEqual(self.call("append_material", **params), original)
        self.assertEqual(self.call("person", persona_id=first)["materials"]["private"], 1)
        self.assertEqual(self.ui.call("append_material", params | {"persona_id": second}),
                         {"ok": False, "error": "select_person_first"})
        self.assertEqual(self.call("person", persona_id=second)["materials"]["lore"], 0)

    def test_extension_update_preserves_other_sections_and_calendar_policy(self):
        data = empty_character()
        data["age"] |= {"policy": "calendar", "birth_date": "2000-01-01"}
        draft = self.call("preview_package", text=json.dumps({"format": "pcar_character_v1",
                          "persona_spec": generic_persona().to_dict(), "character": data}))
        self.assertFalse(draft["extensions"]["age_editable"])
        persona = self.create(draft["token"])["id"]
        self.call("select_person", persona_id=persona)
        version = self.call("person", persona_id=persona)["materials"]["id"]
        args = {"persona_id": persona, "expected": version, "request_id": identifier()}
        self.assertEqual(self.ui.call("save_extensions", args | {"changes": {"age": 20}}),
                         {"ok": False, "error": "invalid_extensions"})
        self.call("save_extensions", **args, changes={"hobbies": ["101"], "world": "102"})
        current = self.characters.current(persona)["data"]
        self.assertEqual(current["age"], data["age"])
        self.assertEqual(current["interests"]["hobbies"], ["101"])
        self.assertEqual(current["world"]["description"], "102")
