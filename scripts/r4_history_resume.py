"""Offline-only continuation of the user-paused sixth R4 simulation.

The original package stays immutable. Completed slots are read as evidence;
only uncalled slots are cloned and executed. This is not a paid-call resumer.
"""
import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import r4_expression_realization as common
from scripts import r4_history_validation as history

OUT=common.OUT
OLD=OUT/'simulation-batch-006'
TARGET=OUT/'simulation-batch-006-continuation'
PAUSE=OUT/'pause-002-20261102'
VERSION='r4-history-offline-continuation-v1'
EXTRAS=history.EXTRAS+('scripts/r4_history_resume.py',)


def original_unchanged():
    seal=common.read(PAUSE/'closure.json')
    for name,digest in seal['evidence_hashes'].items():
        if name.startswith('simulation-batch-006/') and common.sha(OUT/name)!=digest:
            raise common.StopRun('paused_simulation_changed:'+name)
    history.prior_batches_unchanged()
    common.protected(full=True)


def pending_items(prepared,results,receipts):
    if prepared['real']:raise common.StopRun('real_continuation_forbidden')
    keys=[i['key'] for i in prepared['items']]
    if len(set(keys))!=len(keys):raise common.StopRun('duplicate_item')
    if set(results)-set(keys) or any(r['item'] not in keys or r['real'] for r in receipts):
        raise common.StopRun('unexpected_or_real_evidence')
    done=[];pending=[]
    for item in prepared['items']:
        key=item['key'];calls=[r for r in receipts if r['item']==key]
        if key in results:
            value=results[key]
            if value['real'] or value['turn']['status']!='completed' or len(calls)!=1 or calls[0]['status']!='completed':
                raise common.StopRun('incomplete_saved_result')
            common.reconcile(value,calls)
            done.append(key)
        else:
            if calls:raise common.StopRun('called_position_without_saved_result')
            pending.append(item)
    return done,pending


def settle_uncalled_copy(db,turn_id):
    """Only change the NEW simulation copy; preserve the orphan audit record."""
    row=db.execute('SELECT status,cancelled_at FROM turns WHERE id=?',(turn_id,)).fetchone()
    if not row or row[1] or row[0] not in ('queued','running'):
        raise common.StopRun('uncalled_turn_state_invalid')
    attempts=db.execute('SELECT id,status,usage_json FROM attempts WHERE turn_id=?',(turn_id,)).fetchall()
    active=db.execute("SELECT count(*) FROM quota_reservations WHERE turn_id=? AND state='active'",(turn_id,)).fetchone()[0]
    if active:raise common.StopRun('uncalled_active_reservation')
    if row[0]=='queued':
        if attempts:raise common.StopRun('queued_attempt_exists')
        return []
    if len(attempts)!=1 or attempts[0][1]!='running' or attempts[0][2] is not None:
        raise common.StopRun('orphan_not_proven_before_dispatch')
    db.execute("UPDATE attempts SET status='interrupted',error_code='user_pause_before_dispatch' WHERE id=?",(attempts[0][0],))
    db.execute("UPDATE turns SET status='queued',error_code=NULL WHERE id=?",(turn_id,))
    return [{'attempt_id':attempts[0][0],'old_status':'running','new_status':'interrupted',
             'reason':'user_pause_before_dispatch','physical_call':False}]


def prepare():
    original_unchanged();prepared=common.verify_freeze(OLD)
    if TARGET.exists():raise common.StopRun('continuation_exists_never_overwrite')
    results={p.stem:common.read(p) for p in (OLD/'results').glob('*.json')}
    rows=common.Ledger(OLD/'receipts').check()
    done,pending=pending_items(prepared,results,rows)
    checkpoint=common.read(PAUSE/'simulation-interruption.json')
    if done!=checkpoint['completed_keys'] or [i['key'] for i in pending]!=checkpoint['unfinished_keys']:
        raise common.StopRun('pause_partition_changed')
    TARGET.mkdir();items=[];changes=[]
    for original in pending:
        item=deepcopy(original);destination=TARGET/'runtime'/item['pair']/item['arm']
        common.confined(destination,TARGET);shutil.copytree(Path(item['runtime']),destination)
        dbfile=destination/'data/database/runtime.sqlite3'
        with sqlite3.connect(dbfile) as db:
            changes.extend(dict(key=item['key'],**change) for change in settle_uncalled_copy(db,item['turn_id']))
        item['runtime']=str(destination);items.append(item)
        for folder in ('planned','accepted-public'):
            dest=TARGET/folder/(item['key']+'.json');dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(OLD/folder/dest.name,dest)
    for name in ('fixture.json','rubric.json','pair-controls.json'):
        shutil.copyfile(OLD/name,TARGET/name)
    common.write_once(TARGET/'prepared.json',{**prepared,'version':VERSION,'items':items,'normal_requests':len(items),
        'created_at':common.timestamp(),'original_package':str(OLD),'completed_original_keys':done})
    common.write_once(TARGET/'recovery.json',{'original_freeze_sha256':common.sha(OLD/'freeze.json'),
        'pause_seal_sha256':common.sha(PAUSE/'closure.json'),'original_read_only':True,'real_calls':0,
        'done_keys':done,'pending_keys':[i['key'] for i in items],'copy_only_state_changes':changes,
        'no_second_retry_layer':True,'no_completed_slot_replay':True})
    files=list(TARGET.glob('*.json'))+list((TARGET/'planned').glob('*.json'))+list((TARGET/'accepted-public').glob('*.json'))
    common.write_once(TARGET/'freeze.json',{'files':{p.relative_to(TARGET).as_posix():common.sha(p) for p in files},
        'execution_extras':list(EXTRAS),'execution_sources':common.sources(EXTRAS)})
    original_unchanged()
    print(json.dumps({'retained':len(done),'pending':len(items),'real_calls':0}),flush=True)


def verify():
    original_unchanged();original=common.verify_freeze(OLD);continued=common.verify_freeze(TARGET)
    values={};calls={};provenance=[];materials=[]
    for root,prepared in ((OLD,original),(TARGET,continued)):
        rows=common.Ledger(root/'receipts').check()
        for item in prepared['items']:
            p=root/'results'/(item['key']+'.json')
            if not p.exists():
                if root==TARGET:raise common.StopRun('continuation_incomplete')
                continue
            value=common.read(p);key=item['key'];observed=[r for r in rows if r['item']==key]
            if key in values or value['real'] or value['turn']['status']!='completed':raise common.StopRun('replayed_or_incomplete_result')
            if len(observed)!=1 or observed[0]['real'] or observed[0]['status']!='completed':raise common.StopRun('simulation_receipt_invalid')
            common.reconcile(value,observed)
            manifest=next(m for m in value['manifests'] if m['task_type']=='dialogue')
            common.validate_request(item,observed[0]['request'],manifest)
            values[key]=value;calls[key]=observed[0]
            provenance.append({'key':key,'root':root.name,'result_sha256':common.sha(p),'receipt_ordinal':observed[0]['ordinal']})
            materials.append({'key':key,'actual_history_assistant_messages':sum(m['role']=='assistant' for m in observed[0]['request']['messages']),
                'selected_examples':item['selected_examples'],'material_selection':manifest['prompt_assembly']['material_selection']})
            db=Path(item['runtime'])/'data/database/runtime.sqlite3'
            with sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True) as c:
                if c.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]:raise common.StopRun('active_reservation')
    if set(values)!={i['key'] for i in original['items']}:raise common.StopRun('combined_slots_incomplete')
    items={i['key']:i for i in original['items']};pairs=[]
    for pair in dict.fromkeys(i['pair'] for i in original['items']):
        keys=[pair+'-'+arm for arm in history.ARMS];normalized=[]
        for key in keys:
            m=next(m for m in values[key]['manifests'] if m['task_type']=='dialogue')
            normalized.append(common.voice_removed(calls[key]['request'],m,items[key]['voice']))
        if normalized[0]!=normalized[1]:raise common.StopRun('unexplained_pair_difference')
        pairs.append({'pair':pair,'only_voice_difference':True})
    result={'passed':True,'simulation_requests':len(values),'simulation_calls':len(calls),'pairs':len(pairs),
        'retained_original_results':len(original['items'])-len(continued['items']),
        'newly_completed_results':len(continued['items']),'real_calls':0,'real_effect_status':'not_run',
        'actual_history_counts':dict(Counter(m['actual_history_assistant_messages'] for m in materials)),
        'six_history_pairs_authored':True,'history_budget_unchanged':True,
        'history_limit':'Only 2–5 assistant history messages survive the unchanged budget; no evidence of six-turn input coverage.',
        'old_evidence_unchanged':True,'formal_unchanged':True,'completed_positions_replayed':0}
    dest=TARGET/'analysis';dest.mkdir(exist_ok=True)
    for name,value in (('verification',result),('provenance',provenance),('request-differences',pairs),('material-differences',materials)):
        common.atomic_json(dest/(name+'.json'),value)
    print(json.dumps(result,ensure_ascii=False),flush=True);return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','simulate','verify'));args=p.parse_args()
    from persona_runtime.instance import InstanceLock
    lock=InstanceLock(OUT/'execution.lock')
    try:
        with common.offline() as network:
            if args.action=='prepare':prepare()
            elif args.action=='simulate':
                original_unchanged();asyncio.run(common.execute(TARGET,False));original_unchanged()
            else:verify()
        common.atomic_json(OUT/('revision-006-resume-network-'+args.action+'.json'),network)
    finally:lock.close()


if __name__=='__main__':main()
