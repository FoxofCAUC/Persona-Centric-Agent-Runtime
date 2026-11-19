"""Local rating, provenance and scope checks with opaque marker records."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from experiments.import_profile_quality import (
    blind_pack, compare_scope, review_html, score, verified_study,
)
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.corpus_files import digest
from persona_runtime.import_admission_v2 import admit_extraction
from persona_runtime.import_analysis import make_wire
from persona_runtime.import_analysis_v2 import VERSION, ImportAnalysisV2
from persona_runtime.import_library import CONTRACT, ImportLibrary, encode
from persona_runtime.import_profile_workspace import ProfileWorkspace
from persona_runtime.paths import RuntimePaths


class QualityCases(unittest.TestCase):
    def setup_ratings(self, n=12):
        versions = {a: {} for a in ("v1", "v2")}
        baseline = {"scoped": {r: {"1": (r, "101")} for r in ("A", "B")},
                    "fragments": {"1": "3"}, "dates": {"1": None}}
        for arm in versions:
            for role in ("A", "B"):
                versions[arm][role] = [
                    {"field": "core_traits" if i % 2 == 0 else "expression_style", "value": str(20+i),
                     "evidence": [{"id": "1", "quote": "101"}]} for i in range(n)]
        packet, key = blind_pack("111", versions, baseline, [])
        ratings = {k: packet[k] for k in ("format", "study", "packet_hash")}
        ratings["reviewer"] = "201"
        ratings["ratings"] = [{"id": r["id"], "supported": True, "adoptable": True, "critical": False, "reason": ""}
                              for r in packet["cases"]]
        return packet, key, ratings

    def test_per_role_floors_and_matched_baseline(self):
        p, k, r = self.setup_ratings()
        self.assertEqual(score(p, k, r, "111")["status"], "passed")
        # A grade on the wrong side must never offset a role-specific failure.
        per_field = deepcopy(r)
        for vote, case in zip(per_field["ratings"], p["cases"], strict=True):
            if case["role"] == "A" and case["field"] == "core_traits":
                vote.update(adoptable=False, reason="902")
        report = score(p, k, per_field, "111")
        self.assertEqual(report["roles"], {"A": "failed", "B": "passed"})
        row = next(i for i in r["ratings"] if k["arms"][i["id"]] == "v2")
        row.update(adoptable=False, reason="901")
        self.assertEqual(score(p, k, r, "111")["status"], "failed")
        row.update(critical=True)
        self.assertEqual(score(p, k, r, "111")["status"], "failed")
        p, k, r = self.setup_ratings(11)
        self.assertEqual(score(p, k, r, "111")["status"], "insufficient")

    def test_no_unreviewed_votes_and_version_swap(self):
        p, k, r = self.setup_ratings()
        for bad in (r | {"study": "2"}, r | {"ratings": r["ratings"][:-1]},
                    r | {"ratings": r["ratings"][:-1] + [r["ratings"][0]]}):
            with self.assertRaises(ValueError):
                score(p, k, bad, "111")
        for bad in (k | {"binding": "4"}, k | {"arms": {i: "v1" for i in k["arms"]}}):
            with self.assertRaises(ValueError):
                score(p, bad, r, "111")
        with self.assertRaises(ValueError):
            score(p, k, r, "112")
        r["ratings"][0].update(supported=False)
        with self.assertRaises(ValueError):
            score(p, k, r, "111")

    def test_card_masks_version_escapes_raw_html_and_requires_current_scope(self):
        p, k, r = self.setup_ratings()
        p["cases"][0]["value"] = "<script>101</script>"
        html = review_html(p)
        self.assertNotIn("<script>101</script>", html)
        self.assertNotIn('"arms"', html)
        self.assertNotIn("control/key", html)
        with self.assertRaises(ValueError):
            score(p, k, r, "111")
        scoped = {"A": {"1": ("A", "123")}, "B": {"1": ("B", "123")}}
        rows = [{"role": role, "records": [{"id": "1", "speaker": role, "start": 0, "end": 2,
                                            "text": "12", "kind": "core"},
                                           {"id": "1", "speaker": role, "start": 2, "end": 3,
                                            "text": "3", "kind": "core"}]} for role in ("A", "B")]
        compare_scope(scoped, rows)
        for patch in ({"kind": "context"}, {"text": "9"}, {"speaker": "B"}, {"id": "2"}):
            invalid = deepcopy(rows)
            invalid[0]["records"][-1].update(patch)
            with self.assertRaises(ValueError):
                compare_scope(scoped, invalid)

    def test_bound_first_receipts_and_draft_projection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = (ImportLibrary(RuntimePaths(root / k)) for k in ("01", "02"))
            requests = root / "03"
            requests.mkdir()
            raw = b"## X\n101\n## Y\n201\n"
            setup = [lib.add(raw) for lib in (old, new)]
            document = setup[0]["id"]
            prep = old.prepare(document, 1, {"X": "A", "Y": "B"}, {})
            contract = {"version": CONTRACT, "model": "deepseek-v4-flash", "analysis_system": "00 json"}
            atomic_json(old.root / "contract.json", contract)
            old_sha = digest((old.root / "contract.json").read_bytes())
            current = {"version": VERSION, "model": "deepseek-v4-flash", "extract_system": "01 json", "merge_system": "02 json"}
            atomic_json(new.root / "contract-v2.json", current)
            new_sha = digest((new.root / "contract-v2.json").read_bytes())
            config = SimpleConfig()
            frontend = ProfileWorkspace(new, config)
            p = frontend.plan(document, setup[1]["revision"], {"X": "A", "Y": "B"}, {})
            plan_id = p["plan"]["id"]
            plan = frontend.load(document, plan_id)
            requests_v2 = ImportAnalysisV2(new, config).preview(document, plan)
            old_hashes = []
            stored = old.row(document)
            drafts = prep["draft"]
            original = json.loads(stored["view"])
            model = config.load()["model"] | {"_json_output": True}
            for b, desc in zip(plan["batches"], requests_v2, strict=True):
                role = b["role"]
                target = next(m for m in original if m["speaker"] == role)
                claim = {"field": "expression_style", "value": "911", "evidence": [{"id": target["id"], "quote": target["text"]}]}
                drafts[role]["claims"] = [claim | {"needs_review": False}]
                wire = make_wire(model, role, original, contract["analysis_system"])
                atomic_json(requests / (role + ".json"), wire)
                old_hashes.append(fingerprint(wire))
                legacy_key = fingerprint([CONTRACT, document, old_sha, model["base_url"], wire])
                self.insert_receipt(old, document, legacy_key, {"claims": [claim], "samples": []})
                value = {"target": role, "batch": b["id"], "observations": [{"field": "expression_style", "value": "912",
                         "scope": "910", "kind": "pattern", "evidence": [{"id": target["id"], "start": 0, "end": 3}]}]}
                new_key = fingerprint([VERSION, document, plan["source_id"], new_sha, model["base_url"], "extract", desc["wire"]])
                self.insert_receipt(new, document, new_key, value)
                accepted, rejected = admit_extraction(value, b)
                with new.db() as db:
                    db.execute("INSERT INTO profile_results VALUES(?,?,?,?,?,?,?)", (plan_id,b["id"],role,"extract",
                               "accepted",encode(accepted),encode(rejected)))
            with old.db() as db:
                db.execute("UPDATE documents SET draft=?,approved=?,status='partial_ready'",(encode(drafts),old_sha))
            atomic_json(old.root / "approval.json", {"decision":"approved","purposes":["analysis"],"contract_sha256":old_sha,
                                                    "request_hashes":old_hashes})
            with new.db() as db:
                db.execute("UPDATE documents SET status='ready'")
            atomic_json(new.root / "approval-v2.json", {"decision":"approved","purposes":["extract"],
                        "contract_sha256":new_sha,"request_hashes":[d["wire_hash"] for d in requests_v2]})
            args = [root/"01", requests, root/"02", document, plan_id]
            binding, versions, baseline = verified_study(*args)
            self.assertEqual(len(binding), 64)
            self.assertEqual(versions["v1"]["A"][0]["value"], "911")
            self.assertEqual(versions["v2"]["B"][0]["value"], "912")
            packet, _ = blind_pack(binding, versions, baseline, [])
            self.assertEqual(len(packet["cases"]), 4)
            # A frozen earlier merge remains as audit history after its support changes.
            with new.db() as db:
                db.execute("INSERT INTO profile_results VALUES(?,?,?,?,?,?,?)", (
                    plan_id, "900", "A", "merge", "superseded", encode([{"value": "991"}]), "[]"))
            updated_binding, verified_versions, _ = verified_study(*args)
            self.assertEqual(binding, updated_binding)
            self.assertEqual(versions, verified_versions)
            # An old binary can re-prepare the document without touching profile_current.
            with new.db() as db:
                db.execute("UPDATE documents SET view=?", (encode(original),))
            with self.assertRaisesRegex(ValueError, "当前使用旧版"):
                verified_study(*args)
            with new.db() as db:
                db.execute("UPDATE documents SET view=NULL")
                db.execute("UPDATE calls SET metadata='{}'")
            with self.assertRaises(ValueError):
                verified_study(*args)
            with new.db() as db:
                db.execute("UPDATE documents SET status='revoked'")
            with self.assertRaises(ValueError):
                verified_study(*args)

    def insert_receipt(self, library, document, key, response_value):
        # Isolated schema fixtures exercise the local archive reader and never call a provider.
        blob = encode({"id": "01", "model": "100", "choices": [
            {"finish_reason":"stop","message":{"content": encode(response_value)}}]}).encode()
        meta = {"network_route":"system_proxy","request_headers_sent":True,"response_headers_received":True,
                "raw_complete":True,"response_sha256":digest(blob)}
        with library.db() as db:
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?)", (key, document, "received", blob, 200, encode(meta), "01"))


class SimpleConfig:
    def load(self):
        return {"model":{"base_url":"https://api.deepseek.com/v1","name":"deepseek-v4-flash",
                         "max_tokens":1000,"max_input_chars":2000}}


if __name__ == "__main__":
    unittest.main()
