"""Deterministic checks for archived persona draft evidence."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from experiments.import_profile_evidence import audit_database, audit_snapshot
from persona_runtime.import_library import ImportLibrary, encode
from persona_runtime.paths import RuntimePaths


class ImportProfileEvidenceCases(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        library = ImportLibrary(RuntimePaths(Path(tmp.name)))
        repeated = ("p01 " * 11).strip()
        raw = "".join(
            f"## X [2026-01-{day:02d} 10:00]\n{text}\n"
            f"## Y [2026-01-{day:02d} 10:01]\n{200 + day}\n"
            for day, text in enumerate([repeated, repeated, "103", "104", "105", "106", "107", "108"], 1)
        ).encode()
        data = library.add(raw)
        ident = data["id"]
        data = library.prepare(ident, data["revision"], data["mapping"], {})
        row = library.row(ident)
        self.view = {record["id"]: record for record in json.loads(row["view"])}
        self.drafts = data["draft"]
        self.drafts["A"]["claims"] = [self.claim("1", "3"), self.claim("1", "3", "5")]
        self.drafts["B"]["claims"] = [self.claim("2") | {"field": "expression_style", "needs_review": True}]
        self.drafts["B"]["edited_fields"] = ["expression_style"]
        with library.db() as db:
            db.execute("UPDATE documents SET status='partial_ready', draft=? WHERE id=?",
                       (encode(self.drafts), ident))
        self.path = library.root / "imports.sqlite3"
        self.row = library.row(ident)

    def claim(self, *ids):
        return {"field": "core_traits", "value": "901", "needs_review": False,
                "evidence": [{"id": number, "quote": self.view[number]["text"]} for number in ids]}

    def test_distinct_fragments_with_duplicate_text_contribute_one_group(self):
        self.assertNotEqual(self.view["1"]["group"], self.view["3"]["group"])
        old = self.path.read_bytes()
        report = audit_database(self.path)
        self.assertEqual(self.path.read_bytes(), old)
        self.assertEqual(report["revision"], self.row["revision"])
        self.assertEqual(report["status"], "partial_ready")
        self.assertEqual(report["roles"]["A"]["core_traits"], {
            "claims": 2, "single_group": 1, "multiple_groups": 1,
            "multi_fragment_single_group": 1, "v1_needs_review": 0, "edited": False,
        })
        self.assertEqual(report["roles"]["B"]["expression_style"], {
            "claims": 1, "single_group": 1, "multiple_groups": 0,
            "multi_fragment_single_group": 0, "v1_needs_review": 1, "edited": True,
        })
        self.assertNotIn("901", encode(report))
        self.assertNotIn("p01", encode(report))

    def test_mismatched_source_views_and_revoked_drafts_are_rejected(self):
        first, *remaining = json.loads(self.row["view"])
        changed = [first | {"text": "900"}, *remaining]
        with self.assertRaisesRegex(ValueError, "当前学习视图"):
            audit_snapshot(self.row | {"view": encode(changed)})
        with self.assertRaises(ValueError):
            audit_snapshot(self.row | {"status": "revoked"})

    def test_exact_quote_from_the_other_role_is_rejected(self):
        self.drafts["A"]["claims"] = [self.claim("2")]
        with self.assertRaisesRegex(ValueError, "目标人物"):
            audit_snapshot(self.row | {"draft": encode(self.drafts)})
