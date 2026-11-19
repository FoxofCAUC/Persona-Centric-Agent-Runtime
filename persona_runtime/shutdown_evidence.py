"""Read-only observations made at exit. Recovery still uses the original receipts."""
from contextlib import closing
import sqlite3


def observe(paths):
    database=paths.home/'data/database/runtime.sqlite3'
    if not database.is_file():return {}
    queries={
        'waiting_inputs':"SELECT id FROM input_batches WHERE state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')",
        'waiting_turns':"SELECT id FROM turns WHERE status='queued'",
        'main_inflight':"""SELECT a.id FROM attempts a WHERE a.status='running' AND NOT EXISTS
            (SELECT 1 FROM expression_main_receipts r WHERE r.logical_attempt_id=a.id AND r.valid=1)""",
        'main_receipt_pending':"""SELECT r.logical_attempt_id FROM expression_main_receipts r JOIN turns t ON t.id=r.turn_id
            WHERE r.valid=1 AND t.status IN ('queued','running','interrupted','failed')""",
        'organization_inflight':"""SELECT a.id FROM organization_attempts a WHERE a.status='running' AND NOT EXISTS
            (SELECT 1 FROM organization_receipts r WHERE r.attempt_id=a.id)""",
        'organization_received':"""SELECT r.attempt_id FROM organization_receipts r JOIN organization_attempts a ON a.id=r.attempt_id
            JOIN organization_tasks t ON t.id=a.task_id WHERE t.status IN ('queued','running','waiting_quota')""",
        'dialogue_inflight':"SELECT attempt_id FROM dialogue_generation_stages WHERE state='sending' AND raw_response IS NULL",
        'dialogue_received':"SELECT attempt_id FROM dialogue_generation_stages WHERE state='sending' AND raw_response IS NOT NULL",
        'display_unconfirmed':"SELECT id FROM expression_speech_items WHERE state IN ('offered','unknown')",
        'legacy_display_unconfirmed':"SELECT id FROM expression_presentation_items WHERE state IN ('offered','unknown')",
        'expression_inflight':"SELECT attempt_id FROM expression_runs WHERE state='dispatched' AND raw_response IS NULL",
        'expression_received':"SELECT attempt_id FROM expression_runs WHERE state='dispatched' AND raw_response IS NOT NULL",
        'speech_inflight':"SELECT attempt_id FROM expression_speech_runs WHERE state='dispatched' AND raw_response IS NULL",
        'speech_received':"SELECT attempt_id FROM expression_speech_runs WHERE state='dispatched' AND raw_response IS NOT NULL",
        'compile_inflight':"SELECT id FROM disposition_compile_attempts WHERE status='running'",
        'delivery_unconfirmed':"SELECT id FROM reply_delivery_parts WHERE state='sending'",
        'selector_inflight':"SELECT id FROM character_selection_attempts WHERE status='running'",
        'observation_inflight':"SELECT id FROM situation_observation_attempts WHERE status='running'",
    }
    result={}
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.5)) as db:
        db.execute("BEGIN")
        for name,query in queries.items():
            ids=[r[0] for r in db.execute(query+' LIMIT 129')]
            if ids:
                result[name]={'count':len(ids) if len(ids)<=128 else db.execute('SELECT count(*) FROM ('+query+')').fetchone()[0],
                              'ids':ids[:128]}
    return result
