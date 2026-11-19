"""End-to-end import planning, local approval, decisions, formal creation and revocation."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.corpus_files import digest
from persona_runtime.import_analysis_v2 import VERSION
from persona_runtime.import_personas import PersonaImports
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store


class Markers:
    def __init__(self):
        self.wires = []

    async def complete(self, model, wire, *, save_raw):
        data = json.loads(wire["messages"][1]["content"])
        self.wires.append(data)
        record = next(r for r in data["records"] if r["kind"] == "core" and r["speaker"] == data["target"])
        evidence = [{k: record[k] for k in ("id", "start", "end")}]
        items = [{"field": field, "kind": "self_report", "scope": "501", "value": value, "evidence": evidence}
                 for field, value in (("introduction", "301"), ("values_and_boundaries", "302"))]
        body = {"batch": data["batch"], "target": data["target"], "observations": items}
        result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}]}
        save_raw(json.dumps(result).encode(), http_status=200, diagnostics={})


class ProfileWorkflowCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        paths = RuntimePaths(Path(temp.name))
        config = ConfigStore(paths)
        config.initialize()
        config.update({"model": {"base_url":"https://api.deepseek.com/v1", "name":"deepseek-v4-flash", "api_key":"100"}})
        self.store = Store(paths)
        self.store.initialize()
        self.transport = Markers()
        self.imports = PersonaImports(self.store, config, self.transport)
        self.lib = self.imports.library
        self.flow = self.imports.profiles
        self.data = self.lib.add(b"## X\n101\n> 901\n\n109\n## Y\n201\n## X\n102\n## Y\n202\n")
        self.ident = self.data["id"]
        self.contract = self.lib.root/"contract-v2.json"
        atomic_json(self.contract, {"version":VERSION,"model":"deepseek-v4-flash","extract_system":"100 json"})

    def plan(self, data):
        return self.flow.plan(self.ident, data["revision"], data["mapping"], data["overrides"])

    async def run_approved(self, data, replay=False):
        plan_id = data["plan"]["id"]
        plan = self.flow.load(self.ident, plan_id)
        wires = self.flow.engine.preview(self.ident, plan)
        atomic_json(self.lib.root/"approval-v2.json", {"decision":"approved", "purposes":["extract"],
                    "contract_sha256":digest(self.contract.read_bytes()),"dispatch_closed":replay,
                    "request_hashes":[d["wire_hash"] for d in wires]})
        self.imports.analyze(self.ident, data["revision"], True, plan_id, cached_only=replay)
        await self.flow.tasks[self.ident]
        return self.lib.status(self.ident)

    async def test_two_profiles_decisions_history_replay_and_withdrawal(self):
        data = self.plan(self.data)
        self.assertEqual(data["plan"]["requests"], 2)
        self.assertEqual(self.plan(data)["revision"], data["revision"])
        data = await self.run_approved(data)
        self.assertEqual(data["status"], "ready")
        plan_id = data["plan"]["id"]
        for role in ("A", "B"):
            data = self.flow.review_all(self.ident, data["revision"], plan_id, role)
            self.assertEqual(data["draft"][role]["spec"]["introduction"], "301")
            self.assertEqual(data["draft"][role]["spec"]["values_and_boundaries"], [])
            boundary = next(c for c in data["draft"][role]["claims"] if c["field"] == "values_and_boundaries")
            data = self.flow.review(self.ident, data["revision"], plan_id, role, boundary["id"], "accept")
            spec = data["draft"][role]["spec"] | {"core_traits":["303"],"expression_style":"304",
                                                  "initial_background":"305","introduction":"306"}
            data = self.lib.edit(self.ident, data["revision"], role, spec, "")
        data = await self.run_approved(data, replay=True)
        self.assertEqual(len(self.transport.wires), 2)
        self.assertEqual(data["draft"]["A"]["spec"]["introduction"], "306")
        create_args = (self.ident, data["revision"], ["A","B"], False, "404", True)
        ids = self.imports.create(*create_args)
        self.assertEqual(self.imports.create(*create_args), ids)
        with self.store.connection() as db:
            for pid, own in zip(ids, ["persona","user"]):
                rows = db.execute("SELECT role,text FROM events WHERE persona_id=? ORDER BY seq", (pid,)).fetchall()
                self.assertEqual([row["text"] for row in rows], ["101\n> 901\n\n109","201","102","202"])
                self.assertEqual(rows[0]["role"], own)
        await self.imports.revoke(self.ident)
        self.assertEqual(self.imports.info(ids[0])["affected_fields"], ["introduction", "values_and_boundaries"])
        with self.assertRaises(ValueError):
            self.flow.load(self.ident, plan_id)
        with self.assertRaises(ValueError):
            self.flow.review_all(self.ident, data["revision"], plan_id, "A")

    async def test_profile_review_all_skips_conflicts_and_keeps_old_evidence_flags(self):
        data = await self.run_approved(self.plan(self.data))
        plan_id = data["plan"]["id"]
        storage = json.loads(self.lib.row(self.ident)["draft"])
        source = storage["A"]["claims"][0]
        core = source | {"id": "401", "field": "core_traits", "value": "410",
                         "kind": "pattern", "needs_review": True}
        inference = source | {"id": "402", "field": "expression_style", "value": "420",
                              "kind": "inference", "needs_review": True}
        conflict = source | {"id": "403", "field": "initial_background", "value": "430",
                             "counter_evidence": source["evidence"], "needs_review": False}
        skipped = core | {"id": "404", "value": "440", "decision": "reject"}
        storage["A"]["claims"].extend([core, inference, conflict, skipped])
        before = json.dumps(storage)
        with self.lib.db() as db:
            db.execute("UPDATE documents SET draft=? WHERE id=?", (before, self.ident))
        current = self.lib.status(self.ident)
        flags = {c["id"]: c["needs_review"] for c in current["draft"]["A"]["claims"]}
        self.assertFalse(flags["401"])
        self.assertFalse(flags["402"])
        self.assertTrue(flags["403"])
        self.assertEqual(self.lib.row(self.ident)["draft"], before)
        with self.assertRaisesRegex(ValueError, "相反证据"):
            self.flow.review(self.ident, current["revision"], plan_id, "A", conflict["id"], "accept")
        adopted = self.flow.review_all(self.ident, current["revision"], plan_id, "A")
        specs = adopted["draft"]["A"]["spec"]
        self.assertEqual(specs["core_traits"], ["410"])
        self.assertEqual(specs["expression_style"], "420")
        self.assertEqual(specs["initial_background"], "")
        self.assertEqual(adopted["draft"]["A"]["decisions"][core["id"]], "accept")

    async def test_replan_archives_v1_and_protects_manual_fields(self):
        legacy = self.lib.prepare(self.ident, self.data["revision"], self.data["mapping"], {})
        spec = legacy["draft"]["A"]["spec"] | {"initial_background":"405"}
        old = self.lib.edit(self.ident, legacy["revision"], "A", spec, "406")
        data = self.plan(old)
        self.assertEqual(data["draft"]["A"]["spec"]["initial_background"], "405")
        archive = self.flow.archive(self.ident, data["plan"]["archived"][0]["id"])
        self.assertEqual(archive["draft"], old["draft"])
        with self.assertRaises(ValueError):
            self.lib.prepare(self.ident, data["revision"], data["mapping"], {})
        changed = self.flow.plan(self.ident, data["revision"], {"X":"B","Y":"A"}, {})
        self.assertNotEqual(changed["plan"]["id"], data["plan"]["id"])
        self.assertEqual(changed["draft"]["A"]["spec"]["initial_background"], "")
        with self.assertRaises(ValueError):
            self.flow.review_all(self.ident, data["revision"], data["plan"]["id"], "A")

    async def test_old_create_cannot_publish_over_an_existing_v2_card(self):
        data = self.plan(self.data)
        data = await self.run_approved(data)
        with self.lib.db() as db:
            db.execute("INSERT INTO profile_card_drafts VALUES(?,?,?,?,?)",
                       (self.ident, data["plan"]["id"], data["revision"], 1, "{}"))
        with self.assertRaisesRegex(ValueError, "角色卡窗口"):
            self.imports.create(self.ident, data["revision"], ["A"], False, "600", False)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM personas").fetchone()[0], 0)

    async def test_without_permission_replay_does_not_send(self):
        data = self.plan(self.data)
        self.imports.analyze(self.ident, data["revision"], True, data["plan"]["id"], cached_only=True)
        await self.flow.tasks[self.ident]
        result = self.lib.status(self.ident)
        self.assertEqual(self.transport.wires, [])
        self.assertEqual(result["plan"]["roles"]["A"]["batches"][0]["state"], "awaiting_approval")

    async def test_plan_digest_and_legacy_reprepare_invalidate_old_dispatch(self):
        initial = self.lib.prepare(self.ident, self.data["revision"], self.data["mapping"], {})
        legacy = self.lib.row(self.ident)
        data = self.plan(initial)
        pid = data["plan"]["id"]
        original = self.flow.load(self.ident, pid)
        with self.lib.db() as db:
            db.execute("UPDATE profile_plans SET payload=? WHERE id=?", (json.dumps(original | {"reserved_ids":["900"]}), pid))
        with self.assertRaisesRegex(ValueError, "指纹"):
            self.plan(data)
        with self.lib.db() as db:
            db.execute("UPDATE profile_plans SET payload=? WHERE id=?", (json.dumps(original), pid))
            db.execute("UPDATE documents SET view=?,draft=?,revision=revision+1 WHERE id=?",
                       (legacy["view"],legacy["draft"],self.ident))
        rolled_back = self.lib.status(self.ident)
        self.assertIsNone(rolled_back["plan"])
        with self.assertRaises(ValueError):
            self.imports.analyze(self.ident, rolled_back["revision"], True, pid)
        restored = self.plan(rolled_back)
        self.assertEqual(restored["plan"]["id"], pid)
        self.assertIn("decisions", restored["draft"]["A"])
        self.assertEqual(self.transport.wires, [])

    async def test_changed_extract_supersedes_old_merge_until_reconciliation(self):
        data = self.plan(self.data)
        pid = data["plan"]["id"]
        first_batch = data["plan"]["roles"]["A"]["batches"][0]["id"]
        first = {"id":"10","role":"A","field":"expression_style","batch":first_batch,"needs_review":False}
        second = first | {"id":"11"}
        merged = first | {"id":"12","support":["10"],"counter":[]}
        batch = self.flow.load(self.ident, pid)["batches"][0]
        assert batch["id"] == first_batch
        async def first_run(ident, plan, *, cached_only, guard, publish):
            publish(first_batch,"A","extract","accepted",[first],[])
            publish("20","A","merge","accepted",[merged],[])
        self.flow.engine.run = first_run
        self.imports.analyze(self.ident, data["revision"], True, pid)
        await self.flow.tasks[self.ident]
        data = self.lib.status(self.ident)
        self.assertEqual([c["id"] for c in data["draft"]["A"]["claims"]], ["12"])
        async def expanded(*args, publish, **kwargs):
            publish(first_batch,"A","extract","accepted",[first,second],[])
        self.flow.engine.run = expanded
        self.imports.analyze(self.ident, data["revision"], True, pid)
        await self.flow.tasks[self.ident]
        data = self.lib.status(self.ident)
        self.assertEqual([c["id"] for c in data["draft"]["A"]["claims"]], ["10","11"])
        self.assertFalse(data["plan"]["roles"]["A"].get("merges"))
