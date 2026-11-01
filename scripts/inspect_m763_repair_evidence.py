"""Read-only repair evidence inspection; never authorizes independent generation."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.acceptance_m763_observation import source_hashes
from persona_runtime.archive import fingerprint
from scripts.m763_r04 import CONTRACT, tool_hashes, model_identity, execution_hashes


def replay_legacy_success(db,turn,attempt,decision,bound):
    """Re-prove old successful observations without another paid dispatch."""
    from persona_runtime.situation_observation import policy_for,prompt_for,timeout_for,sources_for,parse_output,receipt,CONFIGURED_VERSION
    from persona_runtime.archive import encoded,turn_scope
    from persona_runtime.turn_input import load_turn_input
    from persona_runtime.disposition_shadow import ShadowService
    current=execution_hashes();old=bound.get('execution_hashes',{})
    changed={k for k in set(current)|set(old) if current.get(k)!=old.get(k)}
    if changed!={'persona_runtime/situation_observation.py'}:return False
    policy=policy_for(dict(turn))
    if not policy or policy['version']!=CONFIGURED_VERSION or attempt['status']!='completed':return False
    sources=sources_for(db,dict(turn));model=json.loads(attempt['model_snapshot'])
    payload=[{'role':'system','content':prompt_for(policy)},{'role':'user','content':encoded({'sources':sources})}]
    if fingerprint(payload)!=attempt['input_hash']:return False
    original=json.loads(attempt['result_json']);byid={s['id']:s['text'] for s in sources}
    raw={'schema_version':1,'status':original['status'],'items':[]}
    for item in original['items']:
        rebuilt={'name':item['name']}
        for key in ('task_evidence','provided_evidence'):
            rebuilt[key]=[{'source_id':r['source_id'],'quote':byid[r['source_id']][r['start']:r['end']]} for r in item[key]]
        raw['items'].append(rebuilt)
    if parse_output(json.dumps(raw,ensure_ascii=False),sources)!=original:return False
    pre=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE observation_attempt_id=?',(attempt['id'],)).fetchone()[0])
    if pre.get('timeout_seconds')!=timeout_for(model,policy):return False
    service=ShadowService(None)
    try:
        fresh=service._compute(db,turn['persona_id'],turn_scope(db,dict(turn)),load_turn_input(db,dict(turn)),
                               json.loads(turn['context_policy_snapshot'])['disposition'],receipt(attempt))
        return fingerprint(fresh['record'])==fingerprint(decision.get('record'))
    finally:service.close()


def inspect_real(folder, require_real=True):
    folder=folder.resolve()
    freeze=json.loads((folder/'freeze.json').read_text(encoding='utf-8'))
    state=json.loads((folder/'results.json').read_text(encoding='utf-8'))
    fixture_path=REPO/'experiments/persona_evaluation/repair_cases.json'
    fixture=json.loads(fixture_path.read_text(encoding='utf-8'))
    reviews=json.loads((folder/'reviews.json').read_text(encoding='utf-8')) if (folder/'reviews.json').exists() else []
    freezes={fingerprint(freeze):freeze}
    if (folder/'revisions').exists():
        for path in (folder/'revisions').glob('*.json'):
            variant=json.loads(path.read_text(encoding='utf-8'))
            if path.stem!=fingerprint(variant):raise ValueError('Frozen revision fingerprint mismatch')
            freezes[path.stem]=variant
    checks={'real_execution':freeze['mode']==state['mode']=='real' if require_real else state['mode']=='mock',
            'code_or_compatible_replay':True,'tool_binding_or_replay':True,'execution_or_compatible_replay':True,
            'fixture_current':freeze['fixture_sha256']==sha256(fixture_path.read_bytes()).hexdigest(),
            'execution_contract':freeze.get('contract')==state.get('contract')==CONTRACT,
            'cleanup_report':state.get('closed') is True and state.get('limits_restored') is True}
    if require_real:
        acceptance=next((p for p in folder.parents if p.name=='acceptance'),None)
        registry=acceptance/'m763-r04-real-run.json' if acceptance else None
        registered=json.loads(registry.read_text(encoding='utf-8')) if registry and registry.exists() else {}
        checks['registered_real_directory']=registered=={'contract':CONTRACT,'output':str(folder)}
    results=[]
    with closing(sqlite3.connect((folder/'runtime/data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        checks['no_other_calls']=all(db.execute('SELECT count(*) FROM '+name).fetchone()[0]==0 for name in ('attempts','character_selection_attempts','disposition_compile_attempts'))
        checks['no_unresolved']=db.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0]==0
        checks['no_active_reservations']=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]==0
        checks['no_duplicate_observations']=not db.execute('SELECT 1 FROM situation_observation_attempts GROUP BY turn_id,execution_revision HAVING count(*)>1').fetchall()
        checks['no_qq_events']=(db.execute("SELECT count(*) FROM turns WHERE channel='qq'").fetchone()[0]==0
                                and db.execute("SELECT count(*) FROM ingress_receipts WHERE channel='qq'").fetchone()[0]==0)
        for case in fixture['understanding_cases']:
            slots=[(k,v) for k,v in state['slots'].items() if v.get('case_id')==case['id']]
            # The reviewer explicitly chooses the accepted revision; never hide earlier failures.
            review=next((r for r in reviews if r.get('case_id')==case['id']),{})
            chosen=next(((k,v) for k,v in slots if k==review.get('slot_id')),slots[-1] if slots else (None,{}))
            key,slot=chosen;valid=False;reviewed=False
            bound=freezes.get(slot.get('freeze_hash'),{})
            same_code=bound.get('source_hashes')==source_hashes()
            same_tools=bound.get('tool_hashes')==tool_hashes()
            same_execution=bound.get('execution_hashes')==execution_hashes()
            compatibility=False
            a=db.execute('SELECT * FROM situation_observation_attempts WHERE id=? AND turn_id=?',(slot.get('observed_attempt_id'),slot.get('turn_id'))).fetchone()
            m=db.execute('SELECT * FROM context_manifests WHERE id=? AND turn_id=?',(slot.get('context_id'),slot.get('turn_id'))).fetchone()
            d=db.execute('SELECT * FROM disposition_decisions WHERE context_id=? AND turn_id=?',(slot.get('context_id'),slot.get('turn_id'))).fetchone()
            if a and m and d:
                result=json.loads(a['result_json'] or '{}');manifest=json.loads(m['manifest_json']);decision=json.loads(d['record_json'])
                turn=db.execute('SELECT * FROM turns WHERE id=?',(a['turn_id'],)).fetchone()
                if not (same_code and same_tools and same_execution) and review.get('passed') is True:
                    compatibility=replay_legacy_success(db,turn,a,decision,bound)
                tracepath=folder/'dispatch'/(a['id']+'.json')
                trace=json.loads(tracepath.read_text(encoding='utf-8')) if tracepath.exists() else {}
                pre=db.execute('SELECT * FROM context_manifests WHERE observation_attempt_id=?',(a['id'],)).fetchone()
                premanifest=json.loads(pre['manifest_json']) if pre else {}
                from persona_runtime.situation_observation import sources_for
                from persona_runtime.turn_input import load_turn_input
                from persona_runtime.disposition import status_for,version
                sources=sources_for(db,dict(turn));evidence=json.loads(a['evidence_json'])
                source_ok=evidence['sources']==[{'id':s['id'],'hash':fingerprint(s['text'])} for s in sources]
                byid={s['id']:s['text'] for s in sources}
                for item in result.get('items',[]):
                    for ref in item['task_evidence']+item['provided_evidence']:
                        text=byid.get(ref['source_id'],'')
                        source_ok &= (ref['source_hash']==fingerprint(text) and 0<=ref['start']<ref['end']<=len(text)
                                      and ref['fragment_hash']==fingerprint(text[ref['start']:ref['end']]))
                snap=json.loads(turn['context_policy_snapshot'])['disposition']
                valid=(a['status']=='completed' and a['persona_id']==m['persona_id']==d['persona_id']==turn['persona_id']
                    and a['execution_revision']==d['execution_revision']==turn['execution_revision']
                    and d['version_id']==snap['version_id'] and status_for(db,turn['persona_id'],snap['version_id'],current=False)=='ready'
                    and fingerprint(version(db,turn['persona_id'],snap['version_id'])['document'])==snap['document_hash']
                    and model_identity(json.loads(a['model_snapshot']))==state['model_fingerprint']==trace.get('model_fingerprint')
                    and json.loads(turn['observation_model_snapshot'])==json.loads(a['model_snapshot'])
                    and trace.get('mode')==state['mode'] and trace.get('state')=='response_received'
                    and trace.get('request_hash')==a['input_hash']==premanifest.get('input_snapshot_hash')
                    and premanifest.get('contract')==a['contract_version']==json.loads(turn['context_policy_snapshot'])['situation_observation']['version']
                    and pre is not None and trace.get('manifest_id')==pre['id'] and trace.get('attempt_id')==a['id']
                    and a['started_epoch']<=trace.get('dispatch_epoch',0)<=trace.get('finished_epoch',0)
                    and manifest.get('purpose')=='development_preflight' and manifest.get('dispatch_state')=='not_dispatched'
                    and manifest.get('main_model_called') is False and m['attempt_id'] is None and d['attempt_id'] is None
                    and manifest['input_snapshot_hash']==slot.get('final_input_hash') and m['task_type']=='dialogue'
                    and decision.get('applied_to_prompt') is False and manifest['shadow']['applied_to_prompt'] is False
                    and (decision.get('observation') or {}).get('attempt_id')==a['id']
                    and evidence['policy_hash']==fingerprint(turn['context_policy_snapshot'])
                    and evidence['model_hash']==fingerprint(turn['observation_model_snapshot'])
                    and source_ok and result.get('status') in case['allowed']
                    and ((decision.get('record') or {}).get('chosen_action')=='ask')==case['ask_allowed']
                    and [member['text'] for member in load_turn_input(db,dict(turn))]==case['members'])
                receiptids=[r[0] for r in db.execute('SELECT id FROM ingress_receipts WHERE request_id IN ('+','.join('?' for _ in slot['request_ids'])+')',slot['request_ids'])]
                valid &= len(receiptids)==len(case['members']) and set(receiptids)==set(slot.get('receipt_ids',[]))
                reviewed=(review.get('passed') is True and review.get('slot_id')==key and review.get('attempt_id')==a['id']
                          and review.get('result_hash')==fingerprint(result) and review.get('decision_id')==d['id']
                          and review.get('final_input_hash')==manifest['input_snapshot_hash']
                          and review.get('reviewer')=='execution_assistant' and 0<len(review.get('reason',''))<=1000)
                if require_real:
                    digest=str(trace.get('provider_check_hash',''))
                    if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):digest='invalid-check-fingerprint'
                    checkpath=folder/'checks'/digest
                    check=json.loads(checkpath.read_text(encoding='utf-8')) if checkpath.exists() else {}
                    valid &= (check.get('normal') is True and check.get('configured_model_listed') is True
                              and check.get('connection_fingerprint')==state['model_fingerprint']
                              and 0<=trace.get('dispatch_epoch',0)-check.get('checked_epoch',0)<=600
                              and sha256(checkpath.read_bytes()).hexdigest()==trace.get('provider_check_hash')) if checkpath.exists() else False
            checks['code_or_compatible_replay'] &= same_code or compatibility
            checks['tool_binding_or_replay'] &= same_tools or compatibility
            checks['execution_or_compatible_replay'] &= same_execution or compatibility
            results.append({'case':case['id'],'slot':key,'chain_verified':bool(valid),'semantic_review_passed':bool(reviewed),
                            'recorded_revisions':len(slots),'compatibility_replayed':compatibility})
    checks['all_chains_verified']=all(r['chain_verified'] for r in results) and len(results)==12
    checks['all_semantics_reviewed']=all(r['semantic_review_passed'] for r in results) and len(reviews)==12
    return {'checks':checks,'cases':results,'repair_ready_for_R05':all(checks.values()) and require_real,
            'independent_generation_allowed':False,'scope':'development_validation_not_independent_quality'}


def current_engineering(path):
    report=json.loads(path.read_text(encoding='utf-8'))
    return {'passed':report.get('passed') is True and report.get('source_hashes')==source_hashes()
                    and report.get('execution_hashes')==execution_hashes(),
            'tests':report.get('tests'),'scope':report.get('scope'),
            'note':'Supersedes changed runtime paths; original evidence and migration records remain historical.'}


def inspect(root):
    root = root.resolve()
    if root.drive.upper() != 'D:':
        raise ValueError('Explicit D-drive evidence directory required')
    engineering = json.loads((root/'engineering-final/engineering.json').read_text(encoding='utf-8'))
    fixture_path = REPO/'experiments/persona_evaluation/repair_cases.json'
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    code = source_hashes()
    checks = {
        'targeted_tests_passed': engineering['passed'] and engineering['failures'] == engineering['errors'] == 0,
        'tested_code_current': engineering['source_hashes'] == engineering['source_hashes_after'] == code,
        'fixture_current': engineering['fixture_hash'] == fingerprint(fixture),
    }
    folder = root/'observation-mock'
    freeze = json.loads((folder/'freeze.json').read_text(encoding='utf-8'))
    state = json.loads((folder/'results.json').read_text(encoding='utf-8'))
    checks['mock_code_current'] = freeze['source_hashes'] == code
    checks['mock_fixture_current'] = freeze['fixture_sha256'] == sha256(fixture_path.read_bytes()).hexdigest()
    checks['mock_explicitly_labelled'] = freeze['mode'] == state['mode'] == 'mock'
    rows = []
    database = folder/'runtime/data/database/runtime.sqlite3'
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for case in fixture['understanding_cases']:
            slot = state['slots'].get(case['id'], {})
            attempt = conn.execute('SELECT * FROM situation_observation_attempts WHERE id=? AND turn_id=?',
                                   (slot.get('observed_attempt_id'), slot.get('turn_id'))).fetchone()
            manifest = conn.execute("SELECT * FROM context_manifests WHERE task_type='situation_observation' AND observation_attempt_id=?",
                                    (slot.get('observed_attempt_id'),)).fetchone()
            valid = bool(attempt and manifest)
            if valid:
                result = json.loads(attempt['result_json'] or '{}')
                content = json.loads(manifest['manifest_json'])
                valid = (attempt['status'] == 'completed' and result.get('status') in case['allowed']
                         and content['input_snapshot_hash'] == attempt['input_hash']
                         and manifest['turn_id'] == attempt['turn_id']
                         and manifest['persona_id'] == attempt['persona_id']
                         and slot.get('contract_correct') is True)
                turn = conn.execute('SELECT * FROM turns WHERE id=?', (attempt['turn_id'],)).fetchone()
                snapshot = json.loads(turn['context_policy_snapshot'])
                projection = conn.execute('SELECT id FROM disposition_versions WHERE id=? AND persona_id=?',
                                          (snapshot['disposition']['version_id'], turn['persona_id'])).fetchone()
                valid = valid and bool(projection) and turn['status'] not in ('queued', 'running')
            rows.append({'case': case['id'], 'source_receipt_manifest_verified': bool(valid)})
        checks['zero_mock_main_attempts'] = conn.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        checks['no_unresolved_mock_turns'] = conn.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0] == 0
    checks['mock_chain_verified'] = len(rows) == 12 and all(r['source_receipt_manifest_verified'] for r in rows)
    checks['isolated_cleanup'] = state['closed'] and state['limits_restored']
    migration = json.loads((root/'migration/report.json').read_text(encoding='utf-8'))
    checks['migration_recovery'] = (migration['fault_rollback'] and migration['restored19_then_upgraded']
                                    and len(migration['chains']) == 2
                                    and all(c['old_columns_unchanged'] and c['to'] == 20 for c in migration['chains']))
    return {'checks': checks, 'cases': rows, 'local_evidence_consistent': all(checks.values()),
            'scope': 'mock_source_acceptance_observation_finalization_and_targeted_tests',
            'semantic_model_validation': 'not_executed',
            'independent_generation_allowed': False,
            'remaining': ['real_understanding_development_and_review', 'evaluation_ledger_and_blind_review_ui',
                          'live_candidate_and_required_full_regression', 'freeze_independent_dataset'],
            'note': 'This tool does not turn static capability flags or simulated labels into semantic quality evidence.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--real-run', type=Path)
    parser.add_argument('--engineering', type=Path)
    args = parser.parse_args()
    report = inspect(args.evidence)
    report['repair_ready_for_R05']=False
    if args.engineering:
        original=report
        current=current_engineering(args.engineering)
        schema_unchanged=original['checks']['migration_recovery'] and json.loads((args.evidence/'engineering-final/engineering.json').read_text(encoding='utf-8'))['source_hashes']['schema_observation.py']==source_hashes()['schema_observation.py']
        report={'historical_evidence':original,'current_engineering':current,'migration_reused_schema_unchanged':schema_unchanged,
                'local_evidence_consistent':current['passed'] and schema_unchanged,
                'repair_ready_for_R05':False,'independent_generation_allowed':False}
    if args.real_run:
        report['real_development']=inspect_real(args.real_run)
        report['repair_ready_for_R05']=report['local_evidence_consistent'] and report['real_development']['repair_ready_for_R05']
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ('local_evidence_consistent', 'independent_generation_allowed')}))
    raise SystemExit(0 if report['local_evidence_consistent'] and (not args.real_run or report['repair_ready_for_R05']) else 2)
