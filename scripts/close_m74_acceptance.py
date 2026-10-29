"""Zero-call source, export, restart and original-state restoration checks."""
import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from scripts.acceptance_m74 import Acceptance
from persona_runtime.config import atomic_json
from persona_runtime.lifecycle import ServiceController
from persona_runtime.quotas import QuotaService


def snapshot(driver):
    pid=driver.state['persona_id']
    with driver.store.connection() as db:
        calls={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('attempts','organization_attempts')}
        parts=[dict(r) for r in db.execute('SELECT id,text_hash,state,attempt,platform_message_id,sent_at,cancel_reason,trigger_event_id FROM reply_delivery_parts WHERE persona_id=? ORDER BY rowid',(pid,))]
        manifests=[dict(r) for r in db.execute('SELECT * FROM context_manifests WHERE persona_id=? ORDER BY rowid',(pid,))]
        pending={table:db.execute('SELECT count(*) FROM '+table+' WHERE persona_id=? AND '+condition,(pid,)).fetchone()[0] for table,condition in (
            ('turns',"status IN ('queued','running')"),('input_batches',"state NOT IN ('linked','cancelled')"),
            ('reply_delivery_parts',"state IN ('pending','sending','failed','uncertain')"),
            ('organization_tasks',"status IN ('queued','running','waiting_quota','interrupted')"))}
    return {'global_calls':calls,'parts':parts,'manifest_hash':hashlib.sha256(json.dumps(manifests,sort_keys=True).encode()).hexdigest(),'pending':pending}


def export_check(driver):
    receipt=driver.client.manage('data.export',name='M74-final-acceptance')
    raw=Path(receipt['path']).read_bytes();data=json.loads(raw)
    forbidden={'encrypted_key','admin_token','bridge_token','claim_token','model_snapshot','api_key'}
    def check(value):
        if isinstance(value,dict):
            assert not forbidden.intersection(value)
            for item in value.values():check(item)
        elif isinstance(value,list):
            for item in value:check(item)
        elif isinstance(value,str) and value[:1] in ('{','['):
            try:item=json.loads(value)
            except ValueError:return
            check(item)
    check(data)
    assert data['format_version']==11 and any(p['cancel_reason']=='new_input' for p in data['reply_delivery_parts'])
    assert all(t['expression_snapshot'] for t in data['turn_context_policies'])
    atomic_json(driver.out/'export-final-check.json',{'passed':True,'format':11,'sha256':hashlib.sha256(raw).hexdigest(),
        'credentials_excluded':True,'path':receipt['path'],'batches':len(data['reply_batches']),'manifests':len(data['context_manifests'])})


def close(output):
    d=Acceptance(output);initial=json.loads((d.out/'initial-state.json').read_text(encoding='utf-8'))
    if (d.out/'restoration.json').exists():raise RuntimeError('Final restoration already recorded; read evidence instead of repeating')
    assert len(d.state['calls'])==26 and all(r['status']=='completed' for r in d.state['calls'].values())
    assert json.loads((d.out/'QQ-pair-result.json').read_text(encoding='utf-8'))['passed']
    controller=ServiceController(d.paths);d.online();pid=d.state['persona_id'];cfg=d.cfg.load()
    args={'persona_id':pid,'channel':'qq','bot':cfg['bot_qq'],'peer':cfg['owner_qq']}
    first=json.loads((d.out/'QQ01.json').read_text(encoding='utf-8'));second=json.loads((d.out/'QQ02.json').read_text(encoding='utf-8'))
    cancelled=next(p for p in first['parts'] if p['cancel_reason']=='new_input');sent=next(p for p in first['parts'] if p['state']=='sent')
    before=snapshot(d)
    try:
        d.client.manage('persona.use',persona_id=pid)
        archive=d.client.manage('archive.get',**args,target={'kind':'turn','id':first['turn']['id']})
        assert archive['status']=='found' and [r['id'] for r in archive['replies']]==[sent['id']]
        assert cancelled['text'] not in json.dumps(archive,ensure_ascii=False)
        refs=second['manifests'][0]['evidence_refs'];used=[r for r in refs if r.get('turn_id')==first['turn']['id']]
        assert used and sent['id'] in used[0]['reply_ids'] and cancelled['id'] not in json.dumps(refs)
        # End only this test persona's completed sessions; no model tasks are submitted.
        for channel in ('desktop','qq'):
            selected=args if channel=='qq' else {'persona_id':pid,'channel':channel}
            for row in d.client.manage('session.list',**selected):
                if row['state']=='active':
                    result=d.client.manage('session.seal',**selected,session_id=row['session_id'],expected_revision=row['revision'],operation_id='m74-close-'+row['session_id'])
                    assert result['state']=='sealed'
        sid=first['turn']['session_id']
        replay=d.client.manage('session.replay',**args,session_id=sid)
        organization=d.client.manage('session.organization.preview',**args,session_id=sid)
        assert cancelled['text'] not in json.dumps(organization,ensure_ascii=False)
        assert sent['id'] in [r['id'] for r in organization['source_refs']]
        atomic_json(d.out/'source-chain.json',{'archive':archive,'replay':replay,'organization_preview':organization,
            'cancelled_text_excluded':True,'manifest_sent_only':True,'organization_calls':0,
            'cancel_after_sent_ms':(datetime.fromisoformat(cancelled['updated_at'])-datetime.fromisoformat(sent['sent_at'])).total_seconds()*1000})
        export_check(d)
        stable=snapshot(d);assert before['global_calls']==stable['global_calls'] and before['parts']==stable['parts']
        assert not any(stable['pending'].values())
        controller.stop('service');controller.start('service');time.sleep(6)
        d.online();after=snapshot(d);assert stable==after
        atomic_json(d.out/'restart-check.json',{'passed':True,'before':stable,'after':after,'additional_calls':0,'confirmed_resends':0})
    finally:
        d.client.manage('persona.use',persona_id=d.state['original_persona_id'])
        QuotaService(d.store,d.cfg).end_acceptance()
        controller.stop_all(include_qq=True)
        d.cfg.update({'organization':{'daily_enabled':initial['configuration']['organization']['daily_enabled']}})
        health=controller.snapshot();final=snapshot(d)
        with d.store.connection() as db:
            selected=d.store.current(db)
            policies_unchanged=all(dict(db.execute('SELECT * FROM interaction_policies WHERE id=?',(p['id'],)).fetchone())==p for p in initial['policies'])
            heads_unchanged=all(db.execute('SELECT version_id FROM interaction_heads WHERE persona_id=? AND channel=?',(p['persona_id'],p['channel'])).fetchone()[0]==p['id'] for p in initial['policies'])
        result={'full_configuration_restored':d.cfg.load()==initial['configuration'],'original_persona_restored':selected==d.state['original_persona_id'],
                'original_policies_restored':policies_unchanged and heads_unchanged,'services':health,'pending':final['pending'],
                'global_call_delta':{k:final['global_calls'][k]-before['global_calls'][k] for k in before['global_calls']},
                'actual_calls':26,'tokens':sum(r['usage']['total_tokens'] for r in d.state['calls'].values()),'amount':'unknown','user_read':'unknown'}
        result['passed']=all(result[k] for k in ('full_configuration_restored','original_persona_restored','original_policies_restored')) and all(v['state']=='已停止' for v in health.values()) and not any(final['pending'].values()) and not any(result['global_call_delta'].values())
        atomic_json(d.out/'restoration.json',result);assert result['passed'],'Restoration incomplete, inspect private evidence'
    print(json.dumps({'passed':True,'calls':26,'tokens':result['tokens'],'services':'stopped','configuration_restored':True}))


def verify_restoration(output):
    """Resume only final read-only verification after evidence already exists."""
    d=Acceptance(output);initial=json.loads((d.out/'initial-state.json').read_text(encoding='utf-8'))
    restarted=json.loads((d.out/'restart-check.json').read_text(encoding='utf-8'));assert restarted['passed']
    assert json.loads((d.out/'export-final-check.json').read_text(encoding='utf-8'))['passed']
    assert json.loads((d.out/'source-chain.json').read_text(encoding='utf-8'))['cancelled_text_excluded']
    final=snapshot(d);health=ServiceController(d.paths).snapshot()
    with d.store.connection() as db:
        selected=d.store.current(db)
        policies_unchanged=all(dict(db.execute('SELECT * FROM interaction_policies WHERE id=?',(p['id'],)).fetchone())==p for p in initial['policies'])
        heads_unchanged=all(db.execute('SELECT version_id FROM interaction_heads WHERE persona_id=? AND channel=?',(p['persona_id'],p['channel'])).fetchone()[0]==p['id'] for p in initial['policies'])
    result={'verification_only':True,'full_configuration_restored':d.cfg.load()==initial['configuration'],
        'original_persona_restored':selected==d.state['original_persona_id'],'original_policies_restored':policies_unchanged and heads_unchanged,
        'services':health,'pending':final['pending'],'global_call_delta':{k:final['global_calls'][k]-restarted['before']['global_calls'][k] for k in final['global_calls']},
        'actual_calls':26,'tokens':sum(r['usage']['total_tokens'] for r in d.state['calls'].values()),'amount':'unknown','user_read':'unknown'}
    result['passed']=all(result[k] for k in ('full_configuration_restored','original_persona_restored','original_policies_restored')) and all(v['state']=='已停止' for v in health.values()) and not any(final['pending'].values()) and not any(result['global_call_delta'].values())
    atomic_json(d.out/'restoration.json',result)
    assert result['passed'],'Restoration incomplete; no service was changed by this read-only verification'
    print(json.dumps({'passed':True,'calls':26,'tokens':result['tokens'],'services':'stopped','verification_only':True}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--verify-restoration',action='store_true');a=p.parse_args()
    (verify_restoration if a.verify_restoration else close)(a.output)
