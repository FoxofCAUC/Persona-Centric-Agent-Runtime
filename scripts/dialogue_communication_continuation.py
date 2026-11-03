"""User-authorized CR3-A.1 continuation; original sealed batch is read-only."""
import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import dialogue_communication_real as old
from scripts.dialogue_communication_real_audit import batch as audit_parent_batch
from scripts.dialogue_communication_real_audit import verify as audit_parent
from persona_runtime.config import atomic_json,ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.character_embedding import private_directory
from persona_runtime.archive import encoded,fingerprint

HOME=old.HOME
OUT=HOME/'exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation-continuation'
PARENT=old.OUT/'real-v1'
VERSION='cr3-real-continuation-v1'
read,sha,write_once,require=old.read,old.sha,old.write_once,old.require
def sources():return {n:sha(ROOT/n) for n in ('scripts/dialogue_communication_continuation.py','tests/test_dialogue_communication_continuation.py')}
def confined(root):
    root=Path(root).resolve();require(root.is_relative_to(OUT.resolve()) and root!=OUT.resolve(),'continuation_root_outside');return root
def references(root):
    imp=read(root/'imports.json');require(sha(Path(imp['parent_closure']['path']))==imp['parent_closure']['sha256'],'continuation_parent_seal_changed')
    for kind in ('results','auxiliary'):
        for item in imp[kind].values():require(sha(Path(item['path']))==item['sha256'],'continuation_import_changed')
    for path,h in imp.get('template_files',{}).items():require(sha(Path(path))==h,'continuation_accepted_template_changed')
    return imp
def remaining(parent,values):
    done=[];pending=[]
    for pos in parent['positions']:
        present=[pos['id']+'-'+c in values for c in pos['conditions']]
        require(not any(present) or all(present),'continuation_partial_group_requires_new_plan')
        (done if all(present) else pending).append(pos)
    return done,pending
async def prepare(root,real=False):
    root=confined(root);require(not root.exists(),'continuation_existing_package');audit_parent(worktree=True)
    parent=old.frozen(PARENT);state=read(PARENT/'state.json');require(state['status']=='stopped','continuation_parent_not_stopped')
    values={f.stem:read(f) for f in (PARENT/'results').glob('*.json')};done,pending=remaining(parent,values)
    require(len(done)==6 and len(pending)==22 and pending[0]['id']=='G11-process-r1','continuation_unexpected_boundary')
    audit_parent_batch(PARENT);root.mkdir(parents=True)
    batch=uuid.uuid4().hex;runtime=HOME/'tmp/cr3-real'/batch;private_directory(HOME,runtime)
    write_once(runtime/'owner.json',{'version':old.VERSION,'batch':batch,'artifact_root':str(root),'private':False})
    cfg=ConfigStore(RuntimePaths(HOME)).load()
    if real:require(all(cfg[k].get('encrypted_key') for k in ('model','character_selector')),'continuation_credentials_missing')
    for name in ('materials.json','evaluation-only.json','protocol.json'):write_once(root/name,read(PARENT/name))
    write_once(root/'imports.json',{'parent_root':str(PARENT),'parent_closure':{'path':str(old.OUT/'closure.json'),'sha256':sha(old.OUT/'closure.json')},
        'results':{n:{'path':str(PARENT/'results'/(n+'.json')),'sha256':sha(PARENT/'results'/(n+'.json'))} for n in values},
        'auxiliary':{f.stem:{'path':str(f),'sha256':sha(f)} for f in (PARENT/'auxiliary').glob('*.json')},
        'imported_completed_positions':[x['id'] for x in done],
        'template_files':{str(f):sha(f) for pos in pending for f in Path(parent['items'][pos['id']]['admitted']).rglob('*') if f.is_file()},
        'prior_physical_calls':15,'prior_failed_calls':5,'prior_receipts_origin':str(PARENT)})
    items={pos['id']:deepcopy(parent['items'][pos['id']]) for pos in pending}
    for item in items.values():
        cfgold=ConfigStore(RuntimePaths(Path(item['admitted']))).load()
        for k in ('model','character_selector'):
            a,b=old.public(cfgold[k]),old.public(cfg[k]);a.pop('_dialogue_profile',None);b.pop('_dialogue_profile',None)
            require(a==b,'continuation_public_configuration_changed')
            require(not cfgold[k].get('encrypted_key'),'continuation_template_has_credentials')
    if real:write_once(runtime/'credentials.dpapi.json',{k:cfg[k] for k in ('model','character_selector')})
    write_once(root/'prepared.json',{'version':VERSION,'real':real,'batch':batch,'runtime_root':str(runtime),'positions':pending,
        'original_positions':parent['positions'],'items':items,'parent_batch':parent['batch'],
        'authorization':{'user_requested_resume_after_supplier_recovered':True,'manual_new_attempt_window':True,
            'old_exhausted_slot':'G11-process-r1/joint','not_automatic_fifth_retry':True,'success_replay_prohibited':True}})
    write_once(root/'freeze.json',{'sources':old.sources(),'continuation_sources':sources(),
        'files':{p.relative_to(root).as_posix():sha(p) for p in root.rglob('*.json')}})
    return {'real':real,'imported_positions':6,'new_positions':22,'normal_new_calls_before_alias':88,'real_calls':0}
def frozen(root):
    root=confined(root);p=old.frozen(root);f=read(root/'freeze.json')
    require(f['continuation_sources']==sources(),'continuation_source_changed');references(root);return p
class GuardedLedger(old.Ledger):
    def __init__(self,root,prepared):
        self.package=root;self.prepared=prepared;super().__init__(root/'receipts',prepared['real'])
        self.allowed={pos['id']+'/joint' for pos in prepared['positions']}|{pos['id']+'-'+c+'/main' for pos in prepared['positions'] for c in pos['conditions']}
    def reserve(self,pos,kind,model,messages):
        frozen(self.package);require(pos+'/'+kind in self.allowed,'continuation_imported_success_replay')
        return super().reserve(pos,kind,model,messages)
async def run(root,real):
    p=frozen(root);require(p['real'] is real,'continuation_mode_mismatch');require(not (root/'run.started.json').exists(),'continuation_existing_batch_report_only')
    write_once(root/'run.started.json',{'real':real,'at':time.time(),'batch':p['batch'],'manual_authorized_continuation':True})
    ledger=GuardedLedger(root,p);completed=0
    try:
        for pos in p['positions']:
            frozen(root);await old.group(root,p,pos,ledger);completed+=1
            atomic_json(root/'state.json',{'status':'running','real':real,'completed_new_groups':completed,'new_groups':22,'physical_calls':len(ledger.rows)})
            print(encoded({'completed_new':completed,'of':22,'physical_calls':len(ledger.rows),'real':real}),flush=True)
            if completed==1:write_once(root/'connectivity.json',{'passed':True,'planned_position':pos['id'],'extra_probes':0,'quality_not_a_retry_gate':True})
    except BaseException as error:
        reason=str(error);reason=reason if re.fullmatch('[a-z][a-z0-9_]{0,100}',reason) else type(error).__name__
        atomic_json(root/'state.json',{'status':'stopped','real':real,'completed_new_groups':completed,'new_groups':22,'physical_calls':len(ledger.rows),'reason':reason});raise
    atomic_json(root/'state.json',{'status':'completed','real':real,'completed_new_groups':22,'new_groups':22,'physical_calls':len(ledger.rows)})
    return report(root)
def report(root):
    p=frozen(root);imports=references(root);values={x.stem:read(x) for x in (root/'results').glob('*.json')}
    result={'version':VERSION,'real':p['real'],'state':read(root/'state.json') if (root/'state.json').exists() else {'status':'not_started'},
        'new_logical_conditions':len(values),'imported_logical_conditions':len(imports['results']),'combined_logical_conditions':len(values)+len(imports['results']),
        'new_unique_main':sum(x['status']=='completed' for x in values.values()),'new_aliases':sum(x['status']=='aliased' for x in values.values()),
        'new_auxiliary':len(list((root/'auxiliary').glob('*.json'))),'new_physical_calls':len(list((root/'receipts').glob('*.reserve.json'))),
        'prior_real_physical_calls':15,'old_successes_replayed':0,'stage_complete':False,'machine_reading':'pending','user_review':'pending'}
    atomic_json(root/'report.json',result);return result
def verify(root):
    p=frozen(root);s=report(root);require(s['state']['status']=='completed' and s['new_logical_conditions']==66 and s['new_auxiliary']==22,'continuation_incomplete')
    ids={x.name.split('.')[0] for x in (root/'receipts').glob('*.reserve.json')};slots={}
    for ext in ('transport','terminal'):require({x.name.split('.')[0] for x in (root/'receipts').glob('*.'+ext+'.json')}==ids,'continuation_unsettled')
    for ident in ids:
        r=read(root/'receipts'/(ident+'.reserve.json'));t=read(root/'receipts'/(ident+'.terminal.json'))
        require(r['real']==t['real']==p['real'] and fingerprint(r)==t['reservation_hash'] and r['request_hash']==t['runtime_attempt']['request_hash'],'continuation_audit_binding')
        slots.setdefault(r['slot'],[]).append((r,t))
    for rows in slots.values():require(len(rows)<=4 and len({r['request_hash'] for r,t in rows})==1 and sum(t['status']=='completed' for r,t in rows)==1,'continuation_retry_or_replay')
    values={x.stem:read(x) for x in (root/'results').glob('*.json')}
    for v in values.values():
        if v['status']=='aliased':
            c=values[v['canonical_id']];require(c['status']=='completed' and c['group']==v['group'] and all(c[k]==v[k] for k in ('request_hash','messages','reply')) and 'manifest' not in v,'continuation_invalid_alias')
        else:require(fingerprint(v['messages'])==v['manifest']['input_snapshot_hash'],'continuation_manifest_changed')
    result=s|{'passed':True,'new_auxiliary':22,'combined_auxiliary':24,'combined_logical_conditions':76,'success_replays':0}
    atomic_json(root/'verification.json',result);return result
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','simulate','execute','report','verify'));parser.add_argument('--root',type=Path,default=OUT/'simulation-v1');parser.add_argument('--real',action='store_true');args=parser.parse_args();root=confined(args.root)
    require(not (OUT/'closure.json').exists(),'continuation_stage_sealed')
    os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp');os.environ['HF_HUB_OFFLINE']=os.environ['TRANSFORMERS_OFFLINE']='1'
    if args.action=='execute':require(args.real,'continuation_explicit_real_required');result=asyncio.run(run(root,True))
    else:
        with old.offline() as n,old.secondary() as n2:
            try:result=asyncio.run(prepare(root,args.real)) if args.action=='prepare' else asyncio.run(run(root,False)) if args.action=='simulate' else verify(root) if args.action=='verify' else report(root)
            finally:
                if root.exists():atomic_json(root/('network-'+args.action+'.json'),{'primary':n,'secondary':n2,'real_calls':0})
    print(encoded(result))
if __name__=='__main__':main()
