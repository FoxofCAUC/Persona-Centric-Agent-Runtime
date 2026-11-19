"""Local-only coverage, provenance, partition and payload tests for v2 import planning."""
import json
import unittest

from persona_runtime.corpus_files import digest
from persona_runtime.import_library import encode
from persona_runtime.import_markdown import assign, learning_view, parse, partition_rows
from persona_runtime.import_plan import build_plan, parse_source, source_span, wire_records


class ImportPlanCases(unittest.TestCase):
    def document(self, raw, *, mappings=None, overrides=None):
        return {"id": digest(raw), "raw": raw, "secret": bytes(range(32)),
                "mapping": encode(mappings or {"X": "A", "Y": "B"}),
                "overrides": encode(overrides or {}), "view": None, "status": "preview"}

    def dated(self, days):
        return "".join(f"## X [2026-01-{day:02d} 10:00]\n{a}\n"
                       f"## Y [2026-01-{day:02d} 10:01]\n{b}\n"
                       for day, (a, b) in enumerate(days, start=1)).encode()

    def test_invalid_source_time_keeps_speech_and_recalculates_old_preview_bounds(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from persona_runtime.import_history import source_detail
        from persona_runtime.import_library import ImportLibrary
        from persona_runtime.paths import RuntimePaths

        raw = b"## X [2026-02-28T10:00+08:00]\n101\n## Y [2026-02-31 10:01]\n201\n## X\n102"
        rows = parse_source(raw)["rows"]
        self.assertEqual(source_detail(rows[1] | {"role":"B"}, "B", 480)["raw_time"], "2026-02-31 10:01")
        self.assertEqual([r["date"] for r in rows], ["2026-02-28T10:00+08:00", None, None])
        for label in ("2026-02-28T10:00+01:78", "2026-02-28T10:00-0178"):
            original = parse_source(("## X [" + label + "]\n101\n## Y\n201").encode())["rows"][0]
            detail = source_detail(original | {"role":"A"}, "B", 480)
            self.assertEqual((detail["raw_time"], detail["time"]), (label, None))
        plan = build_plan(self.document(raw), 2400)
        self.assertEqual(plan["roles"]["B"]["messages"], 1)
        self.assertIsNone(plan["roles"]["B"]["first_date"])
        self.assertIsNone(plan["roles"]["B"]["last_date"])
        with TemporaryDirectory() as folder:
            library = ImportLibrary(RuntimePaths(Path(folder)))
            result = library.add(raw)
            bounds = {"count":3, "from":"2026-02-28T10:00+08:00", "to":"2026-02-28T10:00+08:00"}
            self.assertEqual(result["preview"], bounds)
            with library.db() as db:
                db.execute("UPDATE previews SET metadata=? WHERE id=?",
                           (encode(bounds | {"to":"2026-02-31 10:01"}), result["id"]))
            self.assertEqual(library.status(result["id"])["preview"], bounds)
            with library.db() as db:
                self.assertEqual(json.loads(db.execute("SELECT metadata FROM previews WHERE id=?", (result["id"],)).fetchone()[0]), bounds)

    def test_raw_local_coordinates_with_redaction_crlf_and_unicode(self):
        original = ("## X\r\n> 800\r\n801\r\n\r\n100🟩 13912345678 101\r\n"
                    "[附件]\r\n202!\r\n## Y\r\n301\r\n302").encode("utf-16")
        doc = self.document(original)
        plan = build_plan(doc, 2800)
        self.assertEqual(plan["sections"]["quote"], 1)
        self.assertEqual(plan["sections"]["attachment"], 1)
        records = plan["batches"][0]["records"]
        first, last = [r for r in records if r["speaker"] == "A"]
        self.assertNotIn("800", encode(wire_records(plan["batches"][0])))
        self.assertNotIn("13912345678", encode(plan))
        self.assertNotIn("origin", wire_records(plan["batches"][0])[0])
        self.assertNotIn("redactions", wire_records(plan["batches"][0])[0])
        original_text = original.decode("utf-16")
        for record, expected in ((first, "100🟩 13912345678 101"), (last, "202!")):
            start, end = source_span(record, record["start"], record["end"])
            self.assertEqual(original_text[start:end], expected)
        left = first["text"].index("[PHONE_")
        right = first["text"].index("]") + 1
        start, end = source_span(first, left, right)
        self.assertEqual(original_text[start:end], "13912345678")
        with self.assertRaisesRegex(ValueError, "脱敏"):
            source_span(first, left + 1, right)
        with self.assertRaises(ValueError):
            source_span(first, first["start"], last["end"])
        self.assertEqual(plan["roles"]["A"]["stats"]["ending_punctuation"], {"!": 1})
        self.assertEqual(plan["roles"]["A"]["stats"]["messages"], 1)

    def test_markers_stay_with_source_spans(self):
        raw = ("# 000\n由 Moect 导出\n\n[2026-01-01 10:00:00] X\n101\n【888】\n"
               "[2026-01-01 09:00:00] Y\n809\n\n[2026-01-01 10:01:00] Y\n111\n"
               "\n[2026-01-01 10:02:00] 系统\n899\n\n[2026-01-01 10:03:00] X\n[图片]\n802").encode()
        rows = parse_source(raw)["rows"]
        self.assertEqual([r["text"] for r in rows], ["101", "111", "", ""])
        self.assertEqual([b["kind"] for b in rows[0]["sections"]], ["body", "forward"])
        self.assertEqual(rows[2]["sections"][0]["kind"], "system")
        self.assertEqual(rows[3]["sections"][0]["kind"], "attachment")
        plan = build_plan(self.document(raw), 1024)
        sent = wire_records(plan["batches"][0])
        self.assertEqual([r["text"] for r in sent], ["101", "111"])
        ticks = chr(96)*3
        raw = ("## X\n102\n" + ticks + "\n## Q\n808\n" + ticks +
               "\n[图片]\n103\n## Y\n104").encode()
        self.assertEqual(parse_source(raw)["rows"][0]["text"], "102\n103")
        self.assertEqual(parse_source(raw)["counts"], {"X": 1, "Y": 1})

    def test_ambiguous_names_and_missing_speaker_require_correction(self):
        doc = self.document(b"## X\n101\n## X\n102", mappings={"X": "A"})
        with self.assertRaises(ValueError):
            build_plan(doc, 1024)
        plan = build_plan(doc | {"overrides": '{"2":"B"}'}, 1024)
        self.assertEqual({b["role"] for b in plan["batches"]}, {"A", "B"})
        with self.assertRaises(ValueError):
            build_plan(doc | {"overrides": '{"3":"B"}'}, 1024)

    def test_oversize_message_owns_each_codepoint_once_and_never_splits_masks(self):
        original = "🟩1\\\n" * 52 + "13912345678" + "\\2🟦" * 20
        doc = self.document(("## X\n" + original + "\n## Y\n900").encode())
        plan = build_plan(doc, 178)
        windows = [b for b in plan["batches"] if b["role"] == "A"]
        self.assertTrue(1 < len(windows) <= 6)
        primary = []
        order = []
        for batch in windows:
            sent = wire_records(batch)
            self.assertLessEqual(len(encode(sent)), 178)
            for r in batch["records"]:
                if r["speaker"] == "A" and r["kind"] == "core":
                    primary.append(r)
                    a, b = source_span(r, r["start"], r["end"])
                    order.append(doc["raw"].decode()[a:b])
        self.assertEqual("".join(order), original)
        for one, two in zip(primary, primary[1:]):
            self.assertEqual(one["end"], two["start"])

    def test_selection_is_deterministic_limited_and_exposes_uncovered_batches(self):
        doc = self.document(self.dated([((str(n) + "-")*6, (str(40+n) + "-")*5) for n in range(1, 24)]))
        first = build_plan(doc, 198)
        self.assertEqual(build_plan(doc, 198), first)
        self.assertEqual(first["roles"]["A"]["selected"], 6)
        self.assertGreater(first["roles"]["A"]["eligible"], 6)
        choices = first["choices"]["A"]
        chosen = [p["ordinal"]-1 for p in choices if p["selected"]]
        for n, index in enumerate(chosen):
            self.assertLessEqual(n*len(choices)//6, index)
            self.assertLess(index, (n+1)*len(choices)//6)
        unused = next(p["id"] for p in choices if not p["selected"])
        more = build_plan(doc, 198, selection={"A": [unused], "B": []})
        self.assertEqual([b["id"] for b in more["batches"]], [unused])
        with self.assertRaises(ValueError):
            build_plan(doc, 198, selection={"A": [unused], "B": [unused]})

    def test_heldout_reservation_survives_remap_and_group_links(self):
        dates = [(str(100+d), str(200+d)) for d in range(1, 14)]
        dates[0] = dates[4] = ("411", "412")
        doc = self.document(self.dated(dates))
        old = assign(parse(doc["raw"]), json.loads(doc["mapping"]), {})
        dataset = partition_rows(old, doc["secret"], doc["id"])
        old_view = learning_view(dataset)
        reserved = {r["message_id"] for r in dataset["messages"] if r["split"] != "learn"}
        self.assertTrue(reserved)
        changed = doc | {"view": encode(old_view), "legacy_mapping": {"X": "A", "Y": "B"},
                         "legacy_overrides": {}, "mapping": encode({"X": "B", "Y": "A"}),
                         "reserved_ids": ["1"]}
        result = build_plan(changed, 8192)
        self.assertTrue(reserved <= set(result["reserved_ids"]))
        self.assertTrue({"1", "2", "9", "10"} <= set(result["reserved_ids"]))
        for batch in result["batches"]:
            self.assertFalse(set(result["reserved_ids"]) & {r["id"] for r in batch["records"]})
        old_view[0]["text"] += "999"
        with self.assertRaisesRegex(ValueError, "存档"):
            build_plan(changed | {"view": encode(old_view)}, 8192)

    def test_batches_preserve_intact_sessions_and_limit_context_to_own_fragment(self):
        in_one_session = "".join(f"## {('X','Y')[i % 2]}\n{100+i}\n" for i in range(15))
        raw = (in_one_session + "## X [2026-01-01 10:00]\n900\n## Y [2026-01-01 10:01]\n901").encode()
        plan = build_plan(self.document(raw), 420)
        overlapped = []
        for batch in plan["batches"]:
            self.assertLessEqual(len(encode(wire_records(batch))), 420)
            ctx = [r for r in batch["records"] if r["kind"] == "context"]
            if ctx:
                core = next(r for r in batch["records"] if r["kind"] == "core")
                self.assertEqual(ctx[-1]["fragment"], core["fragment"])
                overlapped.extend(r["id"] for r in ctx)
        self.assertTrue(overlapped)


if __name__ == "__main__":
    unittest.main()
