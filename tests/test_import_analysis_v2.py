"""V2 evidence and receipt gates exercised with local, opaque records."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from persona_runtime.archive import fingerprint
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_learning_transport import CorpusCallFailure
from persona_runtime.import_admission_v2 import InvalidObservation, admit_extraction, admit_merge
from persona_runtime.import_analysis_v2 import ImportAnalysisV2, VERSION
from persona_runtime.import_library import ImportLibrary, encode
from persona_runtime.paths import RuntimePaths


def record(ident="1", role="A", text="123", start=0, group="1", kind="core"):
    return {"id": ident, "speaker": role, "text": text, "start": start, "end": start + len(text),
            "group": group, "fragment": ident, "kind": kind, "origin": 101, "redactions": [], "date": None}


def observation(ident, *, start=0, end=2, field="expression_style"):
    return {"field": field, "kind": "pattern", "value": "101", "scope": "102",
            "evidence": [{"id": ident, "start": start, "end": end}]}


def response(batch, observations):
    return {"target": batch["role"], "batch": batch["id"], "observations": observations}


class AdmissionTests(unittest.TestCase):
    def test_admission_is_item_scoped_and_cannot_borrow_context_or_unsent_text(self):
        batch = {"role": "A", "id": "10", "records": [
            record(start=2, text="123"), record("2", role="B"), record("3", kind="context")]}
        good = observation("1", start=2, end=5)
        wrong_quote = observation("1", start=2, end=5)
        wrong_quote["evidence"][0]["quote"] = "22"
        items = [good, observation("1", start=4, end=6), observation("2"), observation("3"), wrong_quote, good | {"scope": chr(0xd800)}]
        accepted, rejected = admit_extraction(response(batch, items), batch)
        self.assertEqual([r["index"] for r in rejected], [1, 2, 3, 4, 5])
        self.assertEqual(len(accepted), 1)
        ref = accepted[0]["evidence"][0]
        self.assertEqual((ref["quote"], ref["raw_start"], ref["raw_end"]), ("123", 103, 106))
        with self.assertRaises(InvalidObservation):
            admit_extraction(response(batch, [good]) | {"target": "B"}, batch)

    def test_core_traits_count_distinct_groups_and_redacted_boundaries(self):
        batch = {"role": "A", "id": "10", "records": [record(), record("2"), record("3", group="9")]}
        claim = observation("1", field="core_traits")
        claim["evidence"].extend([{"id": "2", "start": 0, "end": 2}] * 2)
        accepted, _ = admit_extraction(response(batch, [claim]), batch)
        self.assertTrue(accepted[0]["needs_review"])
        self.assertEqual(len(accepted[0]["evidence"]), 2)
        claim["evidence"].append({"id": "3", "start": 0, "end": 2})
        accepted, _ = admit_extraction(response(batch, [claim]), batch)
        self.assertFalse(accepted[0]["needs_review"])
        batch["records"][0].update(redactions=[[0, 8, 0, 3]])
        accepted, rejected = admit_extraction(response(batch, [observation("1", start=1, end=3)]), batch)
        self.assertFalse(accepted)
        self.assertEqual(rejected[0]["reason"], "ref_source_span")

    def test_merge_resolves_only_verified_same_field_and_role_lineage(self):
        role_a = {"role": "A", "id": "1", "records": [record()]}
        role_b = {"role": "B", "id": "2", "records": [record(role="B")]}
        observations, _ = admit_extraction(response(role_a, [observation("1"), observation("1", field="core_traits")]), role_a)
        others, _ = admit_extraction(response(role_b, [observation("1")]), role_b)
        valid, other_field, other_role = [item["id"] for item in observations + others]
        claim = {"field": "expression_style", "value": "201", "scope": "202", "support": [valid], "counter": []}
        trials = [claim, claim | {"support": [other_field]}, claim | {"support": [other_role]},
                  claim | {"counter": [valid]}, claim | {"evidence": observations[0]["evidence"]}]
        result = {"batch": "3", "target": "A", "claims": trials}
        accepted, rejected = admit_merge(result, "A", "3", observations + others)
        self.assertEqual(len(rejected), 4)
        self.assertEqual(accepted[0]["evidence"], [observations[0]["evidence"][0] | {"observation_id": valid}])


class MarkerTransport:
    def __init__(self):
        self.sent = []
        self.behavior = {}

    async def complete(self, model, request, *, save_raw):
        wire = json.loads(request["messages"][1]["content"])
        batch, role = wire["batch"], wire["target"]
        self.sent.append(batch)
        behavior = self.behavior.get(batch)
        if isinstance(behavior, BaseException):
            raise behavior
        if callable(behavior):
            return behavior(wire, save_raw)
        first = wire["records"][0]
        if "speaker" in first:
            data = {"target": role, "batch": batch, "observations": [observation(first["id"])]}
        else:
            data = {"target": role, "batch": batch, "claims": [
                {"field": first["field"], "value": "401", "scope": "402",
                 "support": [item["id"] for item in wire["records"]], "counter": []}]}
        body = {"choices": [{"message": {"content": encode(data)}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 21}}
        save_raw(encode(body).encode(), http_status=200, diagnostics={})


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.library = ImportLibrary(RuntimePaths(Path(directory.name)))
        self.ident = self.library.add(b"## X\n101\n## Y\n102\n")["id"]
        with self.library.db() as db:
            db.execute("UPDATE documents SET status='running' WHERE id=?", (self.ident,))
        model = {"name": "deepseek-v4-pro", "base_url": "https://api.deepseek.com/v1", "encrypted_key": "0",
                 "max_tokens": 1000, "max_input_chars": 4096, "timeout": 10}
        config = SimpleNamespace(load=lambda: {"model": model})
        self.transport = MarkerTransport()
        self.engine = ImportAnalysisV2(self.library, config, self.transport)
        self.contract = self.library.root / "contract-v2.json"
        self.contract.write_text(encode({"version": VERSION, "model": model["name"],
                                         "extract_system": "100 json", "merge_system": "200 json"}), encoding="utf8")
        identity, _ = self.engine.settings()
        self.plan = {"id": "91", "source_id": "92", "settings": identity, "batches": [
            {"id": "1", "role": "A", "records": [record()]},
            {"id": "2", "role": "B", "records": [record("2", role="B")]}]}
        self.output = {}

    def publish(self, batch, role, stage, state, items, rejected):
        self.output[batch] = {"role": role, "stage": stage, "state": state, "items": items, "rejected": rejected}

    def approve(self, descriptors, *, closed=False, purposes=("extract", "merge")):
        data = {"decision": "approved", "contract_sha256": digest(self.contract.read_bytes()),
                "purposes": list(purposes), "dispatch_closed": closed,
                "request_hashes": [p["wire_hash"] for p in descriptors]}
        (self.library.root / "approval-v2.json").write_text(encode(data), encoding="utf8")

    async def execute(self, *, cached=False, guard=lambda: True):
        await self.engine.run(self.ident, self.plan, cached_only=cached, guard=guard, publish=self.publish)

    async def test_deployed_template_needs_explicit_scope_and_provider(self):
        path = self.library.root / "approval-v2.json"
        config = self.plan["settings"]
        grant = {"decision":"approved", "contract_sha256":digest(self.contract.read_bytes()),
                 "purposes":["extract"], "model":config["model"], "base_url":config["base_url"]}
        for extra in ({}, {"scope":"user_confirmed_import","document":self.ident},
                      {"scope":"user_confirmed_import","model":"1"}):
            path.write_text(encode(grant | extra), encoding="utf8")
            await self.execute()
            self.assertFalse(self.transport.sent)
        path.write_text(encode(grant | {"scope":"user_confirmed_import"}), encoding="utf8")
        await self.execute()
        self.assertEqual(self.transport.sent, ["1","2"])

    async def test_approved_wire_only_and_first_receipt_replayed(self):
        packets = self.engine.preview(self.ident, self.plan)
        self.assertEqual(set(json.loads(packets[0]["wire"]["messages"][1]["content"])["records"][0]),
                         {"id", "speaker", "start", "end", "kind", "text"})
        self.assertEqual(packets[0]["wire_hash"], fingerprint(packets[0]["wire"]))
        await self.execute()
        self.assertFalse(self.transport.sent)
        self.approve(packets[:1])
        await self.execute()
        self.assertEqual(self.transport.sent, ["1"])
        self.assertEqual(self.output["2"]["state"], "awaiting_approval")
        previous = self.output["1"]
        self.approve(packets, closed=True)
        await self.execute()
        self.assertEqual(self.output["1"], previous)
        self.assertEqual(self.output["2"]["state"], "awaiting_approval")
        self.assertEqual(self.transport.sent, ["1"])
        self.approve(packets)
        await self.execute(cached=True)
        self.assertEqual(self.output["2"]["state"], "unsent")
        with self.library.db() as db:
            call, = db.execute("SELECT * FROM calls")
        self.assertEqual(call["status"], 200)
        self.assertEqual(json.loads(call["metadata"])["usage"], {"total_tokens": 21})

    async def test_unknown_and_invalid_batches_survive_reopen_without_dispatch(self):
        self.approve(self.engine.preview(self.ident, self.plan))
        self.transport.behavior["1"] = CorpusCallFailure("1", {"request_headers_sent": True})
        self.transport.behavior["2"] = lambda _, save: save(b'{"choices":[{"message":{"content":"  "},"finish_reason":"stop"}]}',
                                                          http_status=200, diagnostics={})
        await self.execute()
        self.assertEqual([self.output[key]["state"] for key in ("1", "2")], ["unknown", "rejected"])
        self.assertEqual(self.output["2"]["rejected"], [{"reason": "empty"}])
        self.engine = ImportAnalysisV2(self.library, self.engine.config, self.transport)
        await self.execute()
        self.assertEqual(self.transport.sent, ["1", "2"])

    async def test_admission_failure_isolated_and_merge_requires_separate_hash(self):
        self.plan["batches"].append({"id": "3", "role": "A", "records": [record("3", group="3")]})
        packets = self.engine.preview(self.ident, self.plan)
        self.approve(packets)
        def mixed(wire, save):
            claims = [observation("1"), observation("9")]
            body = {"target": "A", "batch": "1", "observations": claims}
            save(encode({"choices": [{"message": {"content": encode(body)}, "finish_reason": "stop"}]}).encode(),
                 http_status=200, diagnostics={})
        self.transport.behavior["1"] = mixed
        await self.execute()
        self.assertEqual(self.output["1"]["state"], "partial")
        self.assertEqual(self.output["2"]["state"], "accepted")
        observed = [o for b in self.output.values() for o in b["items"]]
        merges = self.engine.preview_merge(self.ident, self.plan, observed)
        self.assertEqual(len(merges), 1)
        batch_id = merges[0]["id"]
        self.assertEqual(self.output[batch_id]["state"], "awaiting_approval")
        self.approve(packets + merges)
        await self.execute()
        self.assertEqual(self.transport.sent, ["1", "2", "3", batch_id])
        self.assertEqual(len(self.output[batch_id]["items"][0]["support"]), 2)
        await self.execute()
        self.assertEqual(self.transport.sent.count(batch_id), 1)

    async def test_withdrawal_during_call_retains_receipt_and_blocks_publication(self):
        self.approve(self.engine.preview(self.ident, self.plan))
        active = True
        def guard():
            if not active:
                raise ValueError("1")
        def lose_source(wire, save):
            nonlocal active
            save(b"{}", http_status=200, diagnostics={})
            active = False
        self.transport.behavior["1"] = lose_source
        with self.assertRaises(ValueError):
            await self.execute(guard=guard)
        self.assertEqual(self.transport.sent, ["1"])
        self.assertFalse(self.output)
        with self.library.db() as db:
            self.assertEqual(db.execute("SELECT state FROM calls").fetchone()[0], "received")

    async def test_interruption_before_sending_can_resume(self):
        self.approve(self.engine.preview(self.ident, self.plan))
        error = asyncio.CancelledError()
        error.diagnostics = {"request_headers_sent": False}
        self.transport.behavior["1"] = error
        with self.assertRaises(asyncio.CancelledError):
            await self.execute()
        with self.library.db() as db:
            self.assertEqual(db.execute("SELECT state FROM calls").fetchone()[0], "unsent")
        self.transport.behavior.clear()
        await self.execute()
        self.assertEqual(self.output["1"]["state"], "accepted")

    async def test_approval_bound_to_document_plan_source_and_provider(self):
        packets = self.engine.preview(self.ident, self.plan)
        authority = self.library.root / "approval-v2.json"
        self.approve(packets)
        scope = {"document": self.ident, "plan": self.plan["id"], "source_id": self.plan["source_id"],
                 "model": self.plan["settings"]["model"], "base_url": self.plan["settings"]["base_url"]}
        original = json.loads(authority.read_bytes()) | scope
        for field in scope:
            with self.subTest(field=field):
                authority.write_text(encode(original | {field: "0"}), encoding="utf8")
                await self.execute()
                self.assertFalse(self.transport.sent)
                self.assertEqual(self.output["1"]["state"], "awaiting_approval")
        authority.write_text(encode(original), encoding="utf8")
        await self.execute()
        self.assertEqual(self.transport.sent, ["1", "2"])
        authority.write_text(encode(original | {"plan": "0"}), encoding="utf8")
        await self.execute(cached=True)
        self.assertEqual(self.output["2"]["state"], "awaiting_approval")
        self.assertFalse(self.output["2"]["items"])

    async def test_malformed_receipt_still_records_usage_and_timeout_is_auditable(self):
        self.approve(self.engine.preview(self.ident, self.plan))
        self.transport.behavior["1"] = CorpusCallFailure("1", {
            "request_headers_sent": True, "response_headers_received": False})
        def incomplete(wire, save):
            save(encode({"usage": {"total_tokens": 37}}).encode(), http_status=200, diagnostics={})
        self.transport.behavior["2"] = incomplete
        await self.execute()
        with self.library.db() as db:
            calls = {r["state"]: json.loads(r["metadata"]) for r in db.execute("SELECT * FROM calls")}
        self.assertEqual(self.output["2"]["state"], "rejected")
        self.assertEqual(calls["received"]["usage"], {"total_tokens": 37})
        self.assertTrue(calls["unknown"]["request_headers_sent"])
        self.assertEqual(calls["unknown"]["timeout"], 10)
        self.assertEqual(calls["unknown"]["max_tokens"], 1000)

    async def test_provider_change_while_receiving_prevents_projection(self):
        self.approve(self.engine.preview(self.ident, self.plan))
        def drift(wire, save):
            value = {"target": "A", "batch": "1", "observations": [observation("1")]}
            body = {"choices": [{"message": {"content": encode(value)}, "finish_reason": "stop"}]}
            save(encode(body).encode(), http_status=200, diagnostics={})
            self.engine.config.load()["model"]["max_tokens"] += 10
        self.transport.behavior["1"] = drift
        with self.assertRaisesRegex(ValueError, "已变化"):
            await self.execute()
        self.assertFalse(self.output)
        self.assertEqual(self.transport.sent, ["1"])

    async def test_frozen_template_required_even_in_replay(self):
        self.contract.unlink()
        identity, capacity = self.engine.settings()
        self.assertTrue(identity["configured"])
        self.assertIn("prompt_release", identity)
        self.assertNotIn("prompt_release", self.plan["settings"])
        self.assertGreater(capacity, 0)
        with self.assertRaisesRegex(ValueError, "已变化"):
            await self.execute(cached=True)
        self.assertFalse(self.transport.sent)
