"""Measure support for existing import drafts without changing their evidence or fields."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from persona_runtime.corpus_files import digest
from persona_runtime.import_analysis import checked_result
from persona_runtime.import_library import FIELDS, require
from persona_runtime.import_markdown import assign, learning_view, parse, partition_rows


def audit_snapshot(row):
    require(row["status"] in ("ready", "partial_ready") and row["view"] is not None,
            "需要一份已完成人设提炼的草稿。")
    require(digest(row["raw"]) == row["id"], "导入原件指纹与存档不一致。")
    assigned = assign(parse(row["raw"]), json.loads(row["mapping"]), json.loads(row["overrides"]))
    dataset = partition_rows(assigned, row["secret"], row["id"])
    view = json.loads(row["view"])
    require(learning_view(dataset) == view, "当前学习视图与存档不一致。")

    by_id = {record["id"]: record["group"] for record in view}
    support_group = {record["id"]: record["group_id"] for record in dataset["fragments"]}
    drafts = json.loads(row["draft"])
    require(set(drafts) == {"A", "B"}, "归档草稿缺少双人视角。")
    roles = {}
    for role, draft in drafts.items():
        stored = draft["claims"]
        require(isinstance(stored, list) and all(isinstance(item, dict) for item in stored),
                "归档草稿的建议列表无效。")
        claims, _ = checked_result({
            "claims": [{k: v for k, v in claim.items() if k != "needs_review"} for claim in stored],
            "samples": [],
        }, role, view)
        fields = {
            field: dict(claims=0, single_group=0, multiple_groups=0, multi_fragment_single_group=0,
                        v1_needs_review=0, edited=field in draft.get("edited_fields", []))
            for field in FIELDS
        }
        for claim, prior in zip(claims, stored, strict=True):
            fragments = {by_id[ref["id"]] for ref in claim["evidence"]}
            groups = {support_group[fragment] for fragment in fragments}
            counts = fields[claim["field"]]
            counts["claims"] += 1
            counts["single_group"] += len(groups) == 1
            counts["multiple_groups"] += len(groups) > 1
            counts["multi_fragment_single_group"] += len(fragments) > 1 and len(groups) == 1
            counts["v1_needs_review"] += prior.get("needs_review") is True
        roles[role] = fields
    return {"revision": row["revision"], "status": row["status"], "roles": roles}


def audit_database(path, document=None):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if document is None:
            rows = conn.execute("""SELECT * FROM documents WHERE status IN ('ready','partial_ready')
                                   LIMIT 2""").fetchall()
            require(len(rows) == 1, "请用 --document 指定要核对的已提炼草稿。")
            row = rows[0]
        else:
            row = conn.execute("SELECT * FROM documents WHERE id=?", (document,)).fetchone()
            require(row is not None, "未找到指定的导入草稿。")
    return audit_snapshot(row)


def main():
    parser = argparse.ArgumentParser(description="本地统计导入人设草稿的证据支持组。")
    parser.add_argument("database", type=Path)
    parser.add_argument("--document")
    args = parser.parse_args()
    try:
        report = audit_database(args.database, args.document)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        parser.exit(1, "无法核对草稿。请检查导入库、草稿状态与来源视图。\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
