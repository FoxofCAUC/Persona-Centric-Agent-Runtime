"""Read-only CR3-A.1 seal/checkpoint verifier; imports no runtime/provider code."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
OUT=Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A.1-real-consumption-ablation')
def digest(data):return hashlib.sha256(data).hexdigest()
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def child(root,name):
    if not isinstance(name,str) or not name:raise ValueError('cr31_invalid_path')
    p=(root/name).resolve()
    if not p.is_relative_to(root.resolve()) or p==root.resolve():raise ValueError('cr31_outside_path')
    return p
def fp(v):return digest(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))
def batch(root):
    root=Path(root);state=read(root/'state.json');receipts=root/'receipts'
    ids={p.name.split('.')[0] for p in receipts.glob('*.reserve.json')}
    for ext in ('transport','terminal'):
        if {p.name.split('.')[0] for p in receipts.glob('*.'+ext+'.json')}!=ids:raise ValueError('cr31_unsettled_receipt')
    slots={};failed=[]
    for ident in sorted(ids):
        r=read(receipts/(ident+'.reserve.json'));t=read(receipts/(ident+'.terminal.json'));wire=read(receipts/(ident+'.transport.json'))
        if not (r['real'] and t['real'] and wire['real']) or r['simulation'] or t['simulation']:raise ValueError('cr31_mock_mixed')
        if fp(r)!=t['reservation_hash'] or fp(r['messages'])!=r['input_hash'] or r['request_hash']!=t['runtime_attempt']['request_hash']:raise ValueError('cr31_receipt_hash_mismatch')
        if t['status'] not in ('completed','failed','cancelled'):raise ValueError('cr31_running_receipt')
        slots.setdefault(r['slot'],[]).append((r,t))
        if t['status']=='failed':failed.append({'id':ident,'slot':r['slot'],'code':t['runtime_attempt']['error_code'],'elapsed_seconds':wire['elapsed_seconds']})
    for slot,attempts in slots.items():
        if len(attempts)>4 or len({r['request_hash'] for r,t in attempts})!=1:raise ValueError('cr31_retry_changed_or_exceeded')
        successful=[i for i,(r,t) in enumerate(attempts) if t['status']=='completed']
        if successful and successful!=[len(attempts)-1]:raise ValueError('cr31_success_replayed')
    values={p.stem:read(p) for p in (root/'results').glob('*.json')}
    for v in values.values():
        if not v['real'] or v['simulation']:raise ValueError('cr31_mock_result')
        if v['status']=='aliased':
            c=values[v['canonical_id']]
            if c['status']!='completed' or c['group']!=v['group'] or any(v[k]!=c[k] for k in ('request_hash','messages','reply')):raise ValueError('cr31_bad_alias')
            if 'manifest' in v:raise ValueError('cr31_alias_fake_manifest')
        else:
            if fp(v['messages'])!=v['manifest']['input_snapshot_hash']:raise ValueError('cr31_bad_manifest')
            r=read(receipts/(v['calls'][-1]+'.reserve.json'))
            if r['messages']!=v['messages'] or r['request_hash']!=v['request_hash']:raise ValueError('cr31_actual_input_changed')
    return {'audit_passed':True,'batch_complete':state['status']=='completed','state':state,'physical_calls':len(ids),
        'completed_calls':sum(t['status']=='completed' for rows in slots.values() for r,t in rows),
        'failed_calls':failed,'logical_conditions':len(values),'unique_main':sum(v['status']=='completed' for v in values.values()),
        'aliases':sum(v['status']=='aliased' for v in values.values()),'auxiliary_completed':len(list((root/'auxiliary').glob('*.json'))),
        'unsettled_receipts':0,'success_replays':0,'writes':0,'generation_calls':0}
def verify(root=OUT,worktree=False):
    root=Path(root).resolve();final=(root/'closure.json').is_file();seal=read(root/('closure.json' if final else 'checkpoint.json'))
    if seal.get('stage')!='CR3-A.1':raise ValueError('cr31_wrong_stage')
    for n,h in seal['evidence_hashes'].items():
        if digest(child(root,n).read_bytes())!=h:raise ValueError('cr31_evidence_changed:'+n)
    if final:
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=root/'closure.json'}
        if actual!=set(seal['evidence_hashes']):raise ValueError('cr31_evidence_inventory_changed')
    with zipfile.ZipFile(child(root,seal['source_archive'])) as z:
        names=z.namelist()
        if len(names)!=len(set(names)) or set(names)!=set(seal['source_hashes']):raise ValueError('cr31_archive_inventory_changed')
        for n,h in seal['source_hashes'].items():
            if digest(z.read(n))!=h:raise ValueError('cr31_archived_source_changed:'+n)
            if worktree and digest(child(ROOT,n).read_bytes())!=h:raise ValueError('cr31_worktree_changed:'+n)
    simulation=read(root/'simulation-v2/verification.json')
    if not simulation.get('passed') or simulation['physical_calls']!=96 or simulation['logical_conditions']!=76:raise ValueError('cr31_simulation_not_passed')
    state=read(root/'real-v1/state.json')
    if state['status'] not in ('completed','stopped'):raise ValueError('cr31_unsettled_batch')
    if final and seal.get('stage_complete') is True:
        if state['status']!='completed' or seal.get('user_review')!='completed':raise ValueError('cr31_premature_completion')
    real=batch(root/'real-v1')
    return {'stage':'CR3-A.1','passed':True,'final_seal':final,'stage_complete':seal.get('stage_complete',False),
        'source_files':len(seal['source_hashes']),'evidence_files':len(seal['evidence_hashes']),
        'real_batch_status':state['status'],'real_physical_calls':real['physical_calls'],'writes':0,'generation_calls':0,'worktree_checked':worktree}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=OUT);p.add_argument('--worktree',action='store_true');a=p.parse_args()
    print(json.dumps(verify(a.root,a.worktree),ensure_ascii=False))
