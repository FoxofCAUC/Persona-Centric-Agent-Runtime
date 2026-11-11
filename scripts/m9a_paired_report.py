"""Read-only provenance analysis for a frozen public knowledge comparison."""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

def load(path):return json.loads(path.read_text(encoding='utf-8'))
def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def dispatched(row):
    if row.get('kind')=='fixed_source_input' or row.get('physical') is False:return False
    diagnostics=row.get('diagnostics') or {}
    if diagnostics.get('request_headers_sent') is False:return False
    if diagnostics.get('request_headers_sent') is True or row.get('state')=='received':return True
    return None
def dispatch_counts(calls):
    sent=[row for row in calls if dispatched(row) is True]
    paid=[row for row in sent if row['kind'] in ('jev','main','processing','web')]
    source=[row for row in sent if row['kind']=='wikimedia']
    return {'physical_dispatches':len(paid),'physical_by_kind':dict(Counter(row['kind'] for row in paid)),
        'external_execution_records':len(sent),'source_api_executions':len(source),
        'source_api_completed_http_responses':sum((row.get('usage') or {}).get('source_network_requests',0) for row in source),
        'source_api_actual_http_attempts':None,
        'dispatch_count_note':'physical_dispatches counts recorded model and DeepSeek tool dispatches; Wikimedia source executions may make two requests and bounded retries; completed HTTP responses are a lower bound, not an exact attempt count'}

def percentile(values,q):
    if not values:return None
    values=sorted(values);index=(len(values)-1)*q;left=int(index);right=min(left+1,len(values)-1)
    return round(values[left]+(values[right]-values[left])*(index-left),3)
def judge(row,calls,db):
    knowledge=row.get('knowledge') or {};evidence=knowledge.get('evidence',[])
    shown=(row.get('shown_evidence') or {}).get('evidence',[])
    expected=row['expected_knowledge'];main=[c for c in calls if c.get('position')==row['id'] and c['kind']=='main' and c['state']=='received']
    manifest=db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type=\'dialogue\' ORDER BY rowid DESC LIMIT 1',(row['turn_id'],)).fetchone()
    metadata=json.loads(manifest[0]) if manifest else {}
    actual=next((c for c in reversed(main) if fingerprint(c['input'])==metadata.get('input_snapshot_hash')),None)
    texts=[]
    if knowledge.get('processing'):
        saved=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.processing.record.'+knowledge['processing']['record_id'],)).fetchone()
        if saved:texts=[u['text'] for u in json.loads(saved[0])['data']['result']['translation']]
    translated=bool(actual and texts and all(t in actual['input'][0]['content'] for t in texts))
    source_bound=bool(evidence and len(evidence)==len(shown) and all(e['end']-e['start']==len(e['quote']) for e in shown))
    route=knowledge.get('routing',{});valid=bool(row['status']=='completed' and source_bound and translated and actual)
    direct=bool(row['status']=='completed' and route.get('choice')=='none' and route.get('reuse') is False and not evidence
                and not any(c['kind'] not in ('jev','main') for c in calls if c.get('position')==row['id']))
    return {'id':row['id'],'arm':row['arm'],'phase':row['phase'],'status':row['status'],'error':row.get('error'),
        'expected_knowledge':expected,'evidence_count':len(evidence),'actual_main_request_bound':bool(actual),'reviewed_translation_in_actual_input':translated,
        'source_bound':source_bound,'evidence_injection_pass':valid if expected else None,'direct_chitchat_pass':direct if not expected else None,
        'fact_hints_present':all(any(word.casefold() in (row.get('answer') or '').casefold() for word in group) for group in row.get('fact_hints',[])),
        'seconds':row['seconds']+row.get('initial_failed_seconds',0),'final_segment_seconds':row['seconds'],
        'wall_seconds_with_operator_recovery':row.get('wall_seconds_with_operator_recovery',row['seconds']),
        'reply':row.get('answer'),'route':route.get('choice'),'reuse':route.get('reuse')}

def summarize(rows):
    output={}
    for arm in ('A','B'):
        selected=[r for r in rows if r['arm']==arm];knowledge=[r for r in selected if r['expected_knowledge']];chat=[r for r in selected if not r['expected_knowledge']]
        output[arm]={'positions':len(selected),'completed':sum(r['status']=='completed' for r in selected),
            'knowledge_positions':len(knowledge),'evidence_injection_pass':sum(r['evidence_injection_pass'] for r in knowledge),
            'chitchat_positions':len(chat),'direct_chitchat_pass':sum(r['direct_chitchat_pass'] for r in chat),
            'completed_without_required_evidence':sum(r['status']=='completed' and not r['evidence_injection_pass'] for r in knowledge),
            'all_observed_latency_seconds':{'p50':percentile([r['seconds'] for r in selected],.5),'p95':percentile([r['seconds'] for r in selected],.95)},
            'wall_latency_including_operator_recovery_seconds':{'p50':percentile([r['wall_seconds_with_operator_recovery'] for r in selected],.5),'p95':percentile([r['wall_seconds_with_operator_recovery'] for r in selected],.95)},
            'failure_codes':dict(Counter(r['error'] for r in selected if r['error']))}
    return output

def analyze(root):
    freeze=load(root/'freeze.json');assert freeze['all_positions_share_one_authoritative_database']
    db=sqlite3.connect((root/'runtime/data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)
    focus=root/'focused-v2'
    calls=[load(p) for folder in (root/'calls',focus/'calls') for p in sorted(folder.glob('*.json'))]
    positions=[load(p) for p in sorted((root/'positions').glob('*.json'))]
    repaired={load(p)['parent_position']:load(p) for p in (root/'repairs').glob('*.json')}
    def observed(value,finished_path,original_id=None):
        value=dict(value)
        started=db.execute("SELECT min(at_epoch) FROM request_activity WHERE turn_id=? AND event='accepted'",(original_id or value['turn_id'],)).fetchone()[0]
        if started:value['wall_seconds_with_operator_recovery']=max(0,finished_path.stat().st_mtime-started)
        return value
    original=[judge(observed(r,root/'positions'/(r['id']+'.json')),calls,db) for r in positions]
    resolved=[judge(observed(repaired[r['id']],root/'repairs'/(r['id']+'.json'),r['turn_id']) if r['id'] in repaired else observed(r,root/'positions'/(r['id']+'.json')),calls,db) for r in positions]
    focused=[judge(load(p),calls,db) for p in sorted((focus/'positions').glob('*.json'))]
    compatible=[judge(load(p),calls,db) for p in sorted((focus/'compatibility').glob('*.json'))]
    paired=[r for r in resolved if r['phase']=='paired'];continuous=[r for r in resolved if r['phase']=='continuous'];fixed=[r for r in resolved if r['phase']=='fixed']
    history=[]
    for case in freeze['paired_cases']:
        values=[r for r in positions if r['case_id']==case['id'] and r['phase']=='paired']
        history.append({'case':case['id'],'matched':len(values)==2 and values[0]['input']==values[1]['input'] and values[0]['history_hash']==values[1]['history_hash']})
    physical=[c for c in calls if dispatched(c) is True];model=[c for c in physical if c['kind'] in ('jev','main','processing')]
    tool=[c for c in physical if c['kind']=='web'];money=sum((Decimal(str((c.get('usage') or {}).get('amount_usd','0'))) for c in tool),Decimal('0'))
    b_leaks=[r['id'] for r in positions if r['arm']=='B' and any(e.get('kind')=='encyclopedia' for e in r.get('knowledge',{}).get('evidence',[]))]
    result={'version':3,'raw_positions':len(positions),'planned_main_positions':freeze['planned_main_positions'],
        'execution_complete':len(positions)==freeze['planned_main_positions'],'paired_frozen_histories_match':all(r['matched'] for r in history),'b_local_evidence_leaks':b_leaks,
        'raw_original':summarize([r for r in original if r['phase']=='paired']),
        'focused_followup_rows':focused,'post_batch_compatibility_rows':compatible,
        'focused_followup_pass_count':sum(r['evidence_injection_pass'] is True for r in focused),
        'extra_source_confirmation':load(focus/'extra-followup-result.json') if (focus/'extra-followup-result.json').exists() else None,
        'controlled_no_results_main':load(focus/'controlled-no-results.json') if (focus/'controlled-no-results.json').exists() else None,
        'paired_after_explicit_epoch2_recovery':summarize(paired),'continuous':summarize(continuous),'fixed_source_processing':summarize(fixed),
        'audit_attempt_rows':len(calls),**dispatch_counts(calls),'not_dispatched_or_fixture_rows':sum(dispatched(c) is False for c in calls),
        'dispatch_unknown_rows':sum(dispatched(c) is None for c in calls),
        'known_model_tokens':sum((c.get('usage') or {}).get('total_tokens') or 0 for c in model),
        'unknown_model_usage':sum((c.get('usage') or {}).get('total_tokens') is None for c in model),
        'documentation_tool_usd_estimate':str(money),'tool_amount_unknown_calls':sum((c.get('usage') or {}).get('amount_usd') is None for c in tool),'actual_invoice_amount':None,
        'rows':resolved,'history_pairs':history,'selection':'not_automatically_selected_requires_common_quality_and_semantic_review',
        'latency_note':'active execution adds initial failed and recovery segments; wall latency separately includes operator repair downtime; do not rank faster failures as wins',
        'requires_semantic_review':True,'extra_format_recovery_not_merged_into_original_comparison':True}
    db.close();return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    root=args.root.resolve();result=analyze(root)
    path=root/'comparison-analysis.json'
    if path.exists() and not (root/'comparison-analysis-v1.json').exists():(root/'comparison-analysis-v1.json').write_bytes(path.read_bytes())
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','history_pairs','focused_followup_rows','post_batch_compatibility_rows','extra_source_confirmation','controlled_no_results_main')},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
