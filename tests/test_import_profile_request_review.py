"""Full author-review artifacts are local snapshots of the frozen provider payload."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from aiohttp.payload import JsonPayload

from experiments.import_profile_request_review import export_review
from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.import_admission_v2 import admit_extraction
from persona_runtime.import_analysis_v2 import ImportAnalysisV2, VERSION
from persona_runtime.import_library import ImportLibrary, encode
from persona_runtime.models import timestamp
from persona_runtime.paths import RuntimePaths


class ReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name)
        paths = RuntimePaths(self.parent / "instance")
        self.home = paths.home
        self.library = ImportLibrary(paths)
        self.config = ConfigStore(paths)
        self.config.initialize()
        self.config.update({"model": {"name": "deepseek-v4-pro", "base_url": "https://api.deepseek.com/v1"}})
        self.ident = self.library.add(b"## X\n100\n## Y\n200\n")["id"]
        self.contract = self.library.root / "contract-v2.json"
        self.contract.write_text(encode({"version": VERSION, "model": "deepseek-v4-pro",
                                         "extract_system": "<101> json", "merge_system": "102 json"}), encoding="utf8")
        identity, _ = ImportAnalysisV2(self.library, self.config).settings()
        batches = []
        for ident, role in [("1", "A"), ("2", "B"), ("3", "A")]:
            rec = {"id": ident, "speaker": role, "kind": "core", "start": 0, "end": 3, "text": "101",
                   "group": ident, "fragment": ident, "redactions": [], "origin": 12}
            batches.append({"id": ident, "role": role, "records": [rec]})
        self.plan = {"source_id": "103", "batches": batches, "settings": identity}
        self.plan_id = fingerprint([VERSION, self.ident, self.plan])
        self.plan["id"] = self.plan_id
        with self.library.db() as db:
            db.execute("INSERT INTO profile_plans VALUES(?,?,?,?,?)",
                       (self.plan_id, self.ident, encode(self.plan), "{}", timestamp()))
            db.execute("INSERT INTO profile_current VALUES(?,?)", (self.ident, self.plan_id))
            db.execute("UPDATE documents SET status='partial_ready' WHERE id=?", (self.ident,))
            for batch in batches:
                observation = {"field": "expression_style", "kind": "pattern", "value": "201", "scope": "202",
                               "evidence": [{"id": batch["id"], "start": 0, "end": 2}]}
                accepted, _ = admit_extraction({"target": batch["role"], "batch": batch["id"],
                                                 "observations": [observation]}, batch)
                db.execute("INSERT INTO profile_results VALUES(?,?,?,?,?,?,?)", (
                    self.plan_id, batch["id"], batch["role"], "extract", "accepted", encode(accepted), "[]"))

    def test_exact_extraction_and_merge_export_without_approval_or_transport(self):
        db_path = self.library.root / "imports.sqlite3"
        before = digest(db_path.read_bytes())
        for stage, roles in [("extract", ["A", "B"]), ("merge", ["A"])]:
            output = self.parent / stage
            with patch("persona_runtime.corpus_learning_transport.CorpusLearningHTTPClient.complete",
                       side_effect=AssertionError("unexpected network")):
                result = export_review(self.home, output, stage=stage)
            self.assertEqual([r["role"] for r in result["requests"]], roles)
            self.assertEqual(result["contract_sha256"], digest(self.contract.read_bytes()))
            self.assertEqual(result["manifest_sha256"],
                             fingerprint({k: v for k, v in result.items() if k != "manifest_sha256"}))
            page = (output / "review.html").read_text(encoding="utf8")
            self.assertIn("&lt;101&gt;", page)
            self.assertNotIn("<101>", page)
            for item in result["requests"]:
                body = (output / item["file"]).read_bytes()
                wire = json.loads(body)
                self.assertEqual(JsonPayload(wire)._value, body)
                self.assertEqual((digest(body), len(body)), (item["body_sha256"], item["bytes"]))
                self.assertEqual(fingerprint(wire), item["wire_hash"])
                self.assertIn(item["wire_hash"], page)
            with self.assertRaises(ValueError):
                export_review(self.home, output, stage=stage)
        self.assertFalse((self.library.root / "approval-v2.json").exists())
        self.assertEqual(before, digest(db_path.read_bytes()))

    def test_wrong_selection_or_revoked_source_cannot_generate_review(self):
        output = self.parent / "blocked"
        with self.assertRaises(ValueError):
            export_review(self.home, output, batch_ids=["4"])
        with self.library.db() as db:
            db.execute("UPDATE documents SET status='revoked' WHERE id=?", (self.ident,))
        with self.assertRaises(ValueError):
            export_review(self.home, output)
        self.assertFalse(output.exists())
