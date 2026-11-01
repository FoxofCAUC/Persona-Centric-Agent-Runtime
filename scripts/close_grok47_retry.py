"""Seal completed retry evidence without dispatching or changing historical data."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.grok47_r1_retry import SUPPLEMENT,EVIDENCE,PARENT,RetryReceipts,protected,sources
from scripts.expression_selection_r1 import read,sha,fingerprint,timestamp,verify
from persona_runtime.config import atomic_json


def close():
    if (SUPPLEMENT/'closure.json').exists():raise RuntimeError('already_sealed')
    protected()
    old=read(EVIDENCE/'closure.json')
    for path,digest in old['evidence_hashes'].items():
        if sha(EVIDENCE/path)!=digest:raise RuntimeError('historical_evidence_changed:'+path)
    execution=sources()
    reconciled=[]
    for name,expected in (('simulation',8),('real',6)):
        root=SUPPLEMENT/name
        assert execution==read(root/'execution-sources.json')
        prepared=read(root/'prepared.json');verify(root,prepared)
        status=read(root/'run-status.json')
        assert status['complete'] and status['items']==3 and status['calls']==expected
        ledger=RetryReceipts(root/'receipts',limit=23)
        rows=ledger.check()
        assert len(rows)==expected
        for item in prepared['items']:
            value=read(root/'results'/(item['key']+'.json'))
            for kind,field in (('selector','selection_attempts'),('main','attempts')):
                attempts=value[field]
                assert len(attempts)==1 and attempts[0]['status']=='completed'
                usage=json.loads(attempts[0]['usage_json'])
                audit=usage['generation_retry']['attempts']
                receipts=[r for r in rows if r['item']==item['key'] and r['kind']==kind]
                assert len(audit)==len(receipts) and len(audit)<=4
                for a,r in zip(audit,receipts):
                    assert a['index']==r['attempt_index'] and a['status']==r['status']
                    assert a['request_hash']==fingerprint(r['request'])
                assert sum(a['status']=='completed' for a in audit)==1
                reconciled.append({'phase':name,'item':item['key'],'kind':kind,'physical_attempts':len(audit),
                    'indices':[a['index'] for a in audit],'statuses':[a['status'] for a in audit],
                    'audit_equals_receipts':True,'request_fingerprints_equal':True})
    origins=read(SUPPLEMENT/'analysis/result-sources.json')
    assert len(origins)==32 and sum(r['source']=='original_grok_success' for r in origins)==29
    assert all(sha(Path(r['path']))==r['sha256'] for r in origins)
    metrics=read(SUPPLEMENT/'analysis/metrics.json')
    assert metrics['status']=='complete' and metrics['all_differences_explained'] and metrics['all_final_hashes_match']
    assert metrics['supplement_calls']==6 and metrics['total_including_gate']==71
    assert (PARENT/'receipts/059.reserve.json').exists() and not (PARENT/'receipts/059.result.json').exists()
    tests=read(SUPPLEMENT/'tests-summary.json')
    assert tests['unique_tests']==154 and tests['all_current_expectations_passed']
    for script in (Path(__file__),ROOT/'scripts/report_grok47_retry.py'):
        ast.parse(script.read_text(encoding='utf-8'))
    check=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True)
    (SUPPLEMENT/'diff-check.txt').write_bytes(check.stdout+check.stderr)
    if check.returncode:raise RuntimeError('diff_check_failed')
    atomic_json(SUPPLEMENT/'physical-call-reconciliation.json',reconciled)
    protection={'at':timestamp(),'formal_files_checked':len(read(EVIDENCE/'start.json')['formal_before']),
        'formal_unchanged':True,'old_evidence_files_checked':len(old['evidence_hashes']),
        'old_evidence_unchanged':True,'old_closure_unchanged':True,'original_successes_unchanged':29,
        'execution_sources_unchanged':True,'old_missing_terminal_retained':59,'new_terminal_gaps':0,
        'simulation_physical_attempts':8,'real_physical_attempts':6,'runtime_audit_matches_receipts':True,
        'qq':0,'formal_service_started':False,'commit':False,'push':False,'deploy':False}
    atomic_json(SUPPLEMENT/'final-protection-check.json',protection)
    atomic_json(SUPPLEMENT/'ui-check.json',{'desktop_automated_tests':'passed_in_targeted_suite',
        'new_native_ui_review':'not_executed','new_buttons_or_policy_controls':False,
        'optional_steps':'docs/acceptance/M7.6.3-R1-三次重试验收手册.md',
        'historical_screenshots_reused_as_current_evidence':False})
    shutil.copyfile(ROOT/'docs/M7.6.3-R1-三次自动重试与续验.md',SUPPLEMENT/'report.md')
    paths=sorted(set(subprocess.check_output(['git','ls-files','-co','--exclude-standard','-z'],cwd=ROOT).decode('utf-8').split('\0'))-{''})
    source_hashes={p:sha(ROOT/p) for p in paths if (ROOT/p).is_file()}
    atomic_json(SUPPLEMENT/'source-hashes.json',source_hashes)
    with zipfile.ZipFile(SUPPLEMENT/'source-snapshot.zip','x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_hashes:archive.write(ROOT/path,path)
    (SUPPLEMENT/'final-worktree.patch').write_bytes(subprocess.check_output(['git','diff','--binary'],cwd=ROOT,stderr=subprocess.DEVNULL))
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()
    assert head=='19bca9ee6407927117ded3bd43d9605716afce40'
    evidence={p.relative_to(SUPPLEMENT).as_posix():sha(p) for p in sorted(SUPPLEMENT.rglob('*')) if p.is_file() and p.name!='closure.json'}
    closure={'task':'M7.6.3-R1 三次自动重试、日常运行时与剩余Grok续验','at':timestamp(),
        'status':'complete_engineering_and_data_collection','strict_effect_gate':'not_passed',
        'naturalness_improvement_proven':False,'candidate':'0.7.6.dev8','head':head,
        'schema':20,'export':17,'config':2,'bridge':'0.7.2','retry_policy':'initial_plus_at_most_3_retries',
        'retry_scope':['main_reply','character_selection','isolated_R1_supplement'],
        'logical_rows_preserved':True,'default_selector':'character-selector-v2','formal_mode':'Shadow',
        'source_files':len(source_hashes),'local_tests_passed':154,'R0_frozen_scenarios':34,
        'simulation_requests':3,'simulation_calls':8,'real':metrics,'protection':protection,
        'historical_terminal_gap_repaired':False,'historical_terminal_gap_preserved':True,
        'new_cancellation_receipts_verified_offline':True,'formal_service_started':False,'qq':0,
        'commit':False,'push':False,'deploy':False,'evidence_hashes':evidence}
    atomic_json(SUPPLEMENT/'closure.json',closure)
    print(json.dumps({'status':closure['status'],'source_files':len(source_hashes),'evidence_files':len(evidence),
        'protection':protection,'strict_effect_gate':closure['strict_effect_gate']},ensure_ascii=False,indent=2))


if __name__=='__main__':close()
