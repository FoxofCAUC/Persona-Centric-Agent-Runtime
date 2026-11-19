"""Reconcile a structure-33 desktop v2 submission using local metadata only."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .pcar_acceptance_check import safe_file


def collect(home: Path, request_id: str) -> dict:
    if not home.is_absolute() or not home.is_dir():
        raise ValueError("An existing absolute instance directory is required.")
    file = safe_file(home / "data/database/runtime.sqlite3", home)
    if not file.is_file():
        raise ValueError("The selected instance has no database.")
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        if schema != 33:
            raise ValueError("This report requires business schema 33.")
        accepted = db.execute(
            """SELECT r.id AS receipt_id, r.request_id, r.persona_id, r.session_id,
                      r.event_id, r.batch_id, r.received_at, b.state AS batch_state,
                      t.id AS turn_id, t.status AS turn_state,
                      t.persona_id AS turn_persona_id, t.session_id AS turn_session_id
                 FROM ingress_receipts r
                 JOIN input_batches b ON b.id=r.batch_id
                 LEFT JOIN turns t ON t.input_batch_id=b.id
                WHERE r.channel='desktop' AND r.request_id=?""",
            (request_id,),
        ).fetchone()
        report = {
            "format": "pcar-desktop-turn-evidence-v1",
            "schema": schema,
            "request_id": request_id,
            "acceptance": dict(accepted) if accepted else None,
            "logical_main_attempts": [],
            "main_receipts": [],
            "speech_publications": [],
        }
        if not accepted or not accepted["turn_id"]:
            return report
        turn_id = accepted["turn_id"]
        for row in db.execute(
            "SELECT id, status, usage_json FROM attempts WHERE turn_id=? ORDER BY rowid", (turn_id,)
        ):
            usage = json.loads(row["usage_json"] or "{}") or {}
            attempts = usage.get("generation_retry", {}).get("attempts", [])
            report["logical_main_attempts"].append({
                "id": row["id"],
                "status": row["status"],
                "transport_evidence": "recorded" if attempts else "not_recorded",
                "transport_attempts": [
                    {
                        "id": attempt["id"],
                        "status": attempt["status"],
                        "started_epoch": attempt.get("started_epoch"),
                        "finished_epoch": attempt.get("finished_epoch"),
                        "headers_sent": (attempt.get("diagnostics") or {}).get("request_headers_sent"),
                        "response_headers_received": (attempt.get("diagnostics") or {}).get("response_headers_received"),
                        "effect_unknown": (attempt.get("diagnostics") or {}).get("effect_unknown"),
                        "network_route": (attempt.get("diagnostics") or {}).get("network_route"),
                        "usage_known": attempt.get("usage_known"),
                        "total_tokens": (attempt.get("usage") or {}).get("total_tokens"),
                    }
                    for attempt in attempts
                ],
            })
        report["main_receipts"] = [
            dict(row) for row in db.execute(
                """SELECT id, logical_attempt_id, physical_attempt_id, execution_revision, valid, created_at
                     FROM expression_main_receipts WHERE turn_id=? ORDER BY execution_revision""",
                (turn_id,),
            )
        ]
        for row in db.execute(
            """SELECT p.id, u.id AS utterance_id, u.phase, p.revision, p.state
                 FROM expression_speech_units u JOIN expression_speech_publications p ON p.utterance_id=u.id
                WHERE u.turn_id=? ORDER BY u.phase""",
            (turn_id,),
        ):
            items = db.execute(
                """SELECT i.id AS fragment_id, i.ordinal, i.required, i.state,
                          r.id AS visible_receipt_id,
                          CASE WHEN r.id IS NOT NULL THEN v.presented_at END AS presented_at
                     FROM expression_speech_items i
                     LEFT JOIN expression_speech_visible v
                       ON v.fragment_id=i.id AND v.publication_id=i.publication_id
                      AND v.utterance_id=? AND v.ordinal=i.ordinal
                     LEFT JOIN expression_speech_receipts r
                       ON r.id=v.receipt_id AND r.id=i.receipt_id AND r.fragment_id=i.id
                      AND r.lease_id=i.lease_id AND r.presenter_id=i.presenter_id
                      AND r.body_hash=i.body_hash AND r.state='presented'
                      AND i.state='presented'
                    WHERE i.publication_id=? AND i.revision=?
                    ORDER BY i.ordinal""",
                (row["utterance_id"], row["id"], row["revision"]),
            )
            report["speech_publications"].append(dict(row) | {"items": [dict(item) for item in items]})
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.home, args.request_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
