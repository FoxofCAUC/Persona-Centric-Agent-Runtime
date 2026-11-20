"""Reconcile a desktop v2 submission on schemas 33 through 36 using local metadata only."""

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
        if schema not in (33,34,35,36):
            raise ValueError("This report requires business schema 33 through 36.")
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
        if schema>=36:
            commit=db.execute("""SELECT attempt_id,status,json_array_length(fact_ids) AS facts,rejected,committed_at
                FROM knowledge_study_commits WHERE turn_id=?""",(turn_id,)).fetchone()
            reread=db.execute("""SELECT outcome,json_array_length(fact_ids) AS facts,checked_at
                FROM knowledge_study_reads WHERE turn_id=?""",(turn_id,)).fetchone()
            report['study_commit']=dict(commit) if commit else None
            reasons=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.study.rejections.'+turn_id,)).fetchone()
            report['study_rejections']=json.loads(reasons[0]) if reasons else []
            report['study_reread']=dict(reread) if reread else None
            report['study_timeline']=[dict(r) for r in db.execute("""SELECT event,at_epoch FROM request_activity
                WHERE turn_id=? AND (event LIKE 'knowledge_stud%' OR event='knowledge_ready') ORDER BY seq""",(turn_id,))]
        if schema >= 35:
            local=db.execute("""SELECT query_hash,outcome,json_array_length(fact_ids) AS selected_facts,checked_at
                FROM local_knowledge_reads WHERE turn_id=?""",(turn_id,)).fetchone()
            report['local_knowledge_read']=dict(local) if local else None
        if schema >= 34:
            report["private_reply_attempts"] = [dict(row) for row in db.execute(
                """SELECT a.id,a.phase,a.draft,a.state,a.started_at,a.received_at,d.id decision_id,
                          d.execution_revision FROM private_reply_attempts a JOIN private_reply_decisions d
                          ON d.id=a.decision_id WHERE a.turn_id=? ORDER BY a.rowid""", (turn_id,))]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='private_reply_nodes'").fetchone():
                report["private_decisions"] = [dict(row) for row in db.execute(
                    """SELECT node,draft,attempt_id,state,request_hash FROM private_reply_nodes
                       WHERE turn_id=? AND node!='handoff' ORDER BY rowid""",(turn_id,))]
                report["private_reviews"] = [dict(row) for row in db.execute(
                    """SELECT a.draft,r.receipt_id,r.approved,r.reason FROM private_reply_reviews r
                       JOIN private_reply_attempts a ON a.id=r.attempt_id WHERE a.turn_id=? ORDER BY a.draft""",(turn_id,))]
                report["private_format_reviews"] = [
                    {"attempt_id":row[0], **json.loads(row[1])} for row in db.execute(
                        """SELECT a.id,k.value FROM private_reply_attempts a JOIN knowledge_control k
                           ON k.key='private.reply.format.'||a.id WHERE a.turn_id=? ORDER BY a.rowid""",(turn_id,))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='private_lookup_calls'").fetchone():
            report['lookup_calls']=[dict(r) for r in db.execute(
                "SELECT step,attempt_id,binding,state FROM private_lookup_calls WHERE turn_id=? AND step!='organize' ORDER BY rowid",(turn_id,))]
            if schema>=36:
                report['study_attempts']=[dict(r) for r in db.execute(
                    "SELECT attempt_id,binding,state FROM private_lookup_calls WHERE turn_id=? AND step='organize'",(turn_id,))]
            saved=db.execute("""SELECT outcome,length(notes) notes_characters,json_array_length(sources_json) sources
                                FROM private_lookup_records WHERE turn_id=?""",(turn_id,)).fetchone()
            report['lookup_record']=dict(saved) if saved else None
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
