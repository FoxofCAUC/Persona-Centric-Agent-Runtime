"""Read-only R06 evidence inventory; never dispatches, retries or fills reviews."""
import argparse
from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from scripts.m763_r06 import PACKAGE,TABLES
from scripts.m763_r04 import model_identity
from scripts.m763_retries import effective_case


def readonly(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    return closing(db)


def check_trace(db,trace,turn):
    """Actual committed purpose/Manifest/dispatch evidence, not capability flags."""
    purpose=trace['purpose']
    if purpose not in TABLES:raise ValueError('unexpected_dispatch_purpose')
    if purpose=='selector' and trace['arm']!='B' or purpose=='understanding' and trace['arm']!='C':
        raise ValueError('dispatch_arm_purpose')
    attempt=db.execute('SELECT * FROM '+TABLES[purpose]+' WHERE id=?',(trace['attempt_id'],)).fetchone()
    if not attempt or attempt['turn_id']!=turn['id']:raise ValueError('dispatch_attempt_binding')
    if purpose!='main' and attempt['execution_revision']!=turn['execution_revision']:raise ValueError('dispatch_revision_binding')
    if purpose=='main' and (turn['execution_revision']!=1 or attempt['model_snapshot']!=turn['model_snapshot']):
        raise ValueError('dispatch_primary_frozen_binding')
    key={'main':'attempt_id','selector':'selection_attempt_id','understanding':'observation_attempt_id'}[purpose]
    row=db.execute('SELECT * FROM context_manifests WHERE id=? AND '+key+'=?',(trace['manifest_id'],trace['attempt_id'])).fetchone()
    if not row or row['turn_id']!=turn['id'] or row['persona_id']!=turn['persona_id']:raise ValueError('dispatch_manifest_binding')
    manifest=json.loads(row['manifest_json'])
    if manifest.get('task_type')!={'main':'dialogue','selector':'character_selection','understanding':'situation_observation'}[purpose]:
        raise ValueError('dispatch_manifest_purpose')
    model=json.loads(attempt['model_snapshot'])
    if (trace['model_fingerprint']!=model_identity(model) or
        manifest.get('model_snapshot_hash')!=fingerprint({k:v for k,v in model.items() if k!='encrypted_key'})):
        raise ValueError('dispatch_model_identity')
    if manifest['input_snapshot_hash']!=trace['request_hash']:raise ValueError('dispatch_input_hash')
    if attempt['started_epoch']>trace['dispatch_epoch']:raise ValueError('dispatch_before_registration')
    usage=json.loads(attempt['usage_json']) if attempt['usage_json'] else None
    return {'purpose':purpose,'attempt_status':attempt['status'],'trace_state':trace['state'],
            'known_tokens':(usage or {}).get('total_tokens'),
            'finished':bool(trace.get('finished_epoch')) and attempt['status'] in ('completed','failed','interrupted')}


def inspect(out):
    prepared=json.loads((out/'prepared.json').read_text(encoding='utf-8'))
    if sha256((out/'independent-v1.json').read_bytes()).hexdigest()!=prepared['fixture_sha256']:
        raise ValueError('frozen_fixture_changed')
    ledger=EvaluationStore(RuntimePaths(out));status=ledger.status(PACKAGE)
    with ledger.connect(PACKAGE) as db:
        slots={(r['case_id'],r['arm']):dict(r) for r in db.execute('SELECT * FROM slots')}
        if db.execute('SELECT count(*) FROM cases').fetchone()[0]!=100:raise ValueError('case_count')
    if len(slots)!=300:raise ValueError('slot_count')
    traces={};stats=[];errors=[];counts=Counter();registered=Counter();turn_states=Counter();active_reservations=0;active_quotas=0
    completed_outputs=0;pending_audits=0
    for path in sorted((out/'dispatch').glob('*.json')):
        trace=json.loads(path.read_text(encoding='utf-8'))
        key=trace['attempt_id']
        if key in traces:raise ValueError('duplicate_dispatch')
        traces[key]=trace
    for case in prepared['cases']:
        selected_case=effective_case(out,case)
        sessions=set()
        for arm in 'ABC':
            root=Path(case['runtime_roots'][arm]).resolve()
            if not root.is_relative_to(out.resolve()/'cases'):raise ValueError('evidence_runtime_scope')
            journal=root/'runtime/acceptance-quotas.json'
            if journal.exists() and json.loads(journal.read_text(encoding='utf-8')).get('status')=='active':active_quotas+=1
            with readonly(root/'data/database/runtime.sqlite3') as db:
                selected_turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(selected_case['turns'][arm],)).fetchone())
                sessions.add(selected_turn['session_id']);turn_states[selected_turn['status']]+=1
                active_reservations+=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
                for saved in db.execute('SELECT * FROM turns WHERE persona_id=?',(case['persona_id'],)).fetchall():
                    turn=dict(saved)
                    if any(fingerprint(turn[k])!=v for k,v in case['frozen'][arm].items()):raise ValueError('frozen_request_changed')
                    for purpose,table in TABLES.items():
                        attempts=db.execute('SELECT * FROM '+table+' WHERE turn_id=?',(turn['id'],)).fetchall()
                        if len(attempts)>1:raise ValueError('multiple_attempts_per_execution')
                        for attempt in attempts:
                            registered[purpose]+=1
                            trace=traces.get(attempt['id'])
                            if trace is None:
                                errors.append({'case_id':case['case_id'],'arm':arm,'reason':'attempt_without_dispatch','purpose':purpose})
                                continue
                            counts[purpose]+=1
                            if trace['case_id']!=case['case_id'] or trace['arm']!=arm or Path(trace['runtime_home']).resolve()!=root:
                                raise ValueError('trace_scope')
                            stats.append(check_trace(db,trace,turn))
                turn=selected_turn
                if db.execute('SELECT count(*) FROM disposition_compile_attempts').fetchone()[0]:raise ValueError('unexpected_compile_call')
                slot=slots[(case['case_id'],arm)]
                if slot['state']=='success':
                    if turn['status']!='completed':raise ValueError('ledger_completion_mismatch')
                    m=db.execute('SELECT manifest_json FROM context_manifests WHERE attempt_id=?',(slot['attempt_id'],)).fetchone()
                    if not m:raise ValueError('completed_manifest_missing')
                    manifest=json.loads(m[0])
                    if manifest.get('evaluation',{}).get('arm')!=arm:raise ValueError('completed_variant_missing')
                    batch=db.execute('SELECT * FROM reply_batches WHERE turn_id=?',(turn['id'],)).fetchone()
                    if not batch:raise ValueError('completed_reply_batch_missing')
                    completed_outputs+=1
                else:pending_audits+=1
        if len(sessions)!=3:raise ValueError('sessions_not_independent')
    if sum(counts.values())!=len(traces):raise ValueError('orphan_trace')
    complete=completed_outputs==300 and not pending_audits and not errors
    # No semantic preferences or review answers are inferred from behavior_intent.
    # Loading is a zero-write check only after all source outputs are available.
    review_batches=[]
    if complete:
        for number in range(1,5):
            batch=ledger.load(PACKAGE,number)
            if len(batch['cases'])!=25:raise ValueError('blind_batch_count')
            if any(set(row)!={'case_id','scenario','author_summary','left','right','revision','review'} for row in batch['cases']):
                raise ValueError('blind_mapping_leak')
            review_batches.append({'batch':number,'cases':25,'mapping_hidden':True})
    return {'package_id':PACKAGE,'read_only':True,'model_calls_by_this_tool':0,'frozen_fixture_sha256':prepared['fixture_sha256'],
            'ledger':status,'authority_turn_states':dict(turn_states),'actual_calls':dict(counts),
            'registered_attempts':dict(registered),'registered_without_dispatch':sum(registered.values())-sum(counts.values()),
            'attempt_statuses':dict(Counter((r['purpose']+':'+r['attempt_status']) for r in stats)),
            'known_tokens':sum(r['known_tokens'] or 0 for r in stats),'unknown_usage_calls':sum(r['known_tokens'] is None for r in stats),
            'all_dispatches_terminal':all(r['finished'] for r in stats),'active_reservations':active_reservations,'active_temporary_quotas':active_quotas,
            'completed_outputs_audited':completed_outputs,'pending_output_audits':pending_audits,'evidence_errors':errors,
            'blind_batches':review_batches,'machine_inventory_complete':complete and not active_reservations and not active_quotas,
            'human_reviews_completed':status['reviewed'],'quality_gate_passed':False,
            'remaining':'complete generation and machine semantic screening before human review; no preferences inferred'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--report',type=Path,required=True);a=p.parse_args()
    root=a.output.resolve();dest=a.report.resolve()
    if not dest.is_relative_to(root) or dest.exists():raise ValueError('new controlled report path required')
    report=inspect(root);atomic_json(dest,report)
    print(json.dumps({k:report[k] for k in ('ledger','actual_calls','known_tokens','all_dispatches_terminal','active_reservations','active_temporary_quotas','machine_inventory_complete')}))
