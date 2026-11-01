"""Publish a completed, screened R06 ledger for human review, never for Live.

This tool does not call models, generate preferences, unblind, or touch the
authoritative runtime database. All evidence and outputs remain under D/home.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe
from scripts.check_m763_r06 import inspect
from scripts.m763_r06 import PACKAGE
from scripts.screen_m763_r06 import screen
from scripts.audit_m763_context_replay import audit as audit_contexts


def validate_reviews(store, folder):
    """Require an actual assistant check of each immutable output, not a vote."""
    with store.connect(PACKAGE) as db:
        expected = {}
        for row in db.execute('SELECT case_id,attempt_id,output,state FROM slots'):
            if row['state'] != 'success' or not row['attempt_id']:
                raise ValueError('review_outputs_incomplete')
            key = sha256(row['attempt_id'].encode()).hexdigest()[:16]
            if key in expected:
                raise ValueError('review_identity_collision')
            expected[key] = (row['case_id'], sha256(row['output'].encode()).hexdigest())
    if len(expected) != 300:
        raise ValueError('review_slot_count')
    seen = set()
    counts = Counter()
    fingerprints = {}
    for path in sorted(folder.glob('batch-*.json')):
        safe(path, folder)
        raw = path.read_bytes()
        doc = json.loads(raw.decode('utf-8'))
        if (doc.get('reviewer') != 'execution_assistant'
                or doc.get('additional_model_calls') != 0
                or doc.get('human_blind_reviews_written') != 0
                or doc.get('naturalness_assessed') is not False):
            raise ValueError('review_wrong_scope')
        fingerprints[path.name] = sha256(raw).hexdigest()
        for row in doc['rows']:
            key = row['review_id']
            if key in seen or expected.get(key) != (row['case_id'], row['output_sha256']):
                raise ValueError('review_output_binding')
            if row['task_check'] not in ('pass', 'task_error', 'review_required') or not row.get('reason', '').strip():
                raise ValueError('review_incomplete_judgement')
            seen.add(key)
            counts[row['task_check']] += 1
    if seen != set(expected):
        raise ValueError('review_coverage_incomplete')
    return {'outputs': len(seen), 'task_checks': dict(counts), 'batch_sha256': fingerprints,
            'naturalness_assessed': False, 'human_answers_written': 0}


def copy_for_review(source, target_store, target_id, binding):
    """SQLite snapshot + exclusive atomic publication; never overwrite reviews."""
    dest = target_store.path(target_id)
    safe(source)
    if dest.resolve() == source.resolve() or dest.exists():
        raise FileExistsError('review_destination_exists')
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix='.review-stage-', suffix='.sqlite3', dir=dest.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(temp)) as dst:
                src.backup(dst)
                meta = {k: json.loads(v) for k, v in dst.execute('SELECT key,value FROM metadata')}
                if meta.get('simulated') is not False:
                    raise ValueError('review_simulation_rejected')
                if (dst.execute("SELECT count(*) FROM slots WHERE state='success'").fetchone()[0] != 300
                        or dst.execute('SELECT count(*) FROM slots').fetchone()[0] != 300
                        or dst.execute('SELECT count(*) FROM cases').fetchone()[0] != 100):
                    raise ValueError('review_outputs_incomplete')
                if dst.execute('SELECT count(*) FROM reviews').fetchone()[0] or meta.get('frozen'):
                    raise ValueError('review_already_started')
                if 'review_publication' in meta:
                    raise ValueError('review_copy_already_published')
                dst.execute('INSERT INTO metadata VALUES(?,?)', ('review_publication', json.dumps(binding, sort_keys=True)))
                dst.commit()
                if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('review_copy_integrity')
                # Validate the exact UI payload without exporting the secret mapping.
                for ordinal in range(100):
                    c = dst.execute('SELECT id,left_arm FROM cases WHERE ordinal=?', (ordinal,)).fetchone()
                    if not c or c[1] not in ('B', 'C'):
                        raise ValueError('review_mapping_contract')
        # NTFS supports hard links. Creation is atomic and cannot clobber an
        # existing package if another process races this tool.
        os.link(temp, dest)
        return {'package_id': target_id, 'snapshot_sha256': sha256(dest.read_bytes()).hexdigest(),
                'human_reviews': 0, 'batches': 4, 'cases_per_batch': 25,
                'quality_gate_passed': False, 'live_authorized': False}
    finally:
        temp.unlink(missing_ok=True)


def validate_context_replay(store, report):
    with store.connect(PACKAGE) as db:
        expected = {r['attempt_id'] for r in db.execute("SELECT attempt_id FROM slots WHERE state='success'")}
    rows = report.get('rows', [])
    if (report.get('passed') is not True or report.get('model_calls') != 0
            or report.get('authority_read_only') is not True
            or report.get('successful_contexts') != 300 or len(rows) != 300
            or len(expected) != 300 or {r.get('attempt_id') for r in rows} != expected
            or any(r.get('reproduced') is not True for r in rows)):
        raise ValueError('review_context_replay_incomplete')


def publish(out, home, target_id):
    out = safe(out)
    home = safe(home)
    safe(out, home / 'exports/acceptance')
    inventory = inspect(out)
    if (not inventory['machine_inventory_complete'] or not inventory['all_dispatches_terminal']
            or inventory['human_reviews_completed']):
        raise ValueError('review_inventory_not_ready')
    mechanical = screen(out)
    if mechanical['ledger']['slots'] != {'success': 300}:
        raise ValueError('review_screen_not_ready')
    source_store = EvaluationStore(RuntimePaths(out))
    semantic = validate_reviews(source_store, out / 'machine-semantic-reviews')
    if semantic['task_checks'].get('review_required', 0):
        raise ValueError('review_machine_triage_pending')
    replay = asyncio.run(audit_contexts(home, out))
    validate_context_replay(source_store, replay)
    binding = {
        'version': 'm763-review-publication-v1',
        'source_package_id': PACKAGE,
        'source_ledger_sha256': sha256(source_store.path(PACKAGE).read_bytes()).hexdigest(),
        'fixture_sha256': inventory['frozen_fixture_sha256'],
        'inventory': inventory,
        'mechanical': {k: v for k, v in mechanical.items() if k != 'rows'},
        'semantic': semantic,
        'context_replay': replay,
        'quality_gate_passed': False, 'live_authorized': False,
        'note': 'Machine task errors remain; publication only enables human judgement, not a passing verdict.'}
    return copy_for_review(source_store.path(PACKAGE), EvaluationStore(RuntimePaths(home)), target_id, binding)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--package-id', required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.output, args.home, args.package_id), ensure_ascii=False))
