"""Read-only formal acceptance checks; evidence contains identifiers, never text."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plugins.m8d_acceptance.runner import read,write_new,require,fingerprint
from persona_runtime.models import timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.automatic_memory import validate_sources,usable_fact


def observe(out,position):
    out=Path(out);p=read(out/'formal-preview.json');store=Store(RuntimePaths.from_environment());number={'P1':1,'P2':2,'P3':3}[position]
    with store.connection() as db:
        require(store.current(db)==p['persona_id'],'target_changed')
        turns=[dict(r) for r in db.execute('SELECT * FROM turns WHERE persona_id=? AND rowid>? ORDER BY rowid',(p['persona_id'],p['max_turn_row']))]
        require(len(turns)>=number,'user_input_pending');turn=turns[number-1];require(turn['status']=='completed','main_not_completed')
        batches=[]
        for r in db.execute("SELECT * FROM memory_batches WHERE persona_id=? AND purpose='extract'",(p['persona_id'],)):
            b=dict(r);payload=json.loads(b['data_json'])['payload']
            if any(i['turn_id']==turn['id'] for i in payload['bindings']):batches.append((b,payload))
        if position!='P3':
            require(len(batches)==1 and batches[0][0]['state']=='completed','automatic_review_pending')
            b,payload=batches[0];validate_sources(db,payload,check_memories=False)
            decisions=[dict(r) for r in db.execute("SELECT * FROM memory_decisions WHERE batch_id=? AND state='published'",(b['id'],))];require(decisions,'fact_not_published')
            mid=json.loads(decisions[0]['data_json'])['memory_id'];memory=dict(db.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone())
            require(usable_fact(db,memory),'published_fact_not_current')
            stages=[]
            for tid in (b['extract_task_id'],b['verify_task_id']):
                task=db.execute('SELECT * FROM organization_tasks WHERE id=?',(tid,)).fetchone();require(task['status']=='completed','stage_incomplete')
                attempts=[]
                for a in db.execute('SELECT * FROM organization_attempts WHERE task_id=?',(tid,)):
                    receipt=db.execute('SELECT * FROM organization_receipts WHERE attempt_id=?',(a['id'],)).fetchone();manifest=db.execute('SELECT id,manifest_json FROM context_manifests WHERE organization_attempt_id=?',(a['id'],)).fetchone()
                    require(receipt and manifest and a['usage_json'],'receipt_manifest_usage_missing')
                    attempts.append({'id':a['id'],'status':a['status'],'usage':json.loads(a['usage_json']),'receipt_hash':fingerprint(receipt['response_text']),'manifest_id':manifest['id'],'manifest_hash':fingerprint(manifest['manifest_json'])})
                stages.append({'task_id':tid,'attempts':attempts})
            result={'passed':True,'at':timestamp(),'turn_id':turn['id'],'session_id':turn['session_id'],'batch_id':b['id'],'memory_id':mid,'revision':memory['revision'],'root_id':memory['root_id'],'previous_id':memory['previous_id'],'fact_hash':fingerprint(memory['text']),'stages':stages,'source_revalidated':True,'automatic_publish':True}
            if position=='P2':
                old=read(out/'formal-P1.json');require(memory['root_id']==old['root_id'] and memory['revision']>old['revision'] and memory['previous_id']==old['memory_id'],'correction_not_versioned')
                require(db.execute('SELECT status FROM memories WHERE id=?',(old['memory_id'],)).fetchone()[0]=='superseded','old_current_fact_remaining')
                invalid=db.execute("SELECT count(*) FROM memory_fragments WHERE persona_id=? AND state='invalid'",(p['persona_id'],)).fetchone()[0]
                repairs=db.execute("SELECT count(*) FROM memory_batches WHERE persona_id=? AND purpose='repair'",(p['persona_id'],)).fetchone()[0]
                result['summary_repair']='pending' if invalid else 'completed' if repairs else 'NO_ACTION'
        else:
            correction=read(out/'formal-P2.json');uses=[json.loads(r[0]) for r in db.execute('SELECT details_json FROM context_uses WHERE turn_id=?',(turn['id'],))]
            require(any(m['id']==correction['memory_id'] and m['revision']==correction['revision'] for u in uses for m in u.get('memories',[])),'corrected_fact_missing_in_actual_context')
            require(turn['session_id']!=correction['session_id'],'recall_requires_clean_session')
            require(not any(g['turn_id'] in (read(out/'formal-P1.json')['turn_id'],correction['turn_id']) for u in uses for g in u.get('history_groups',[])),'recall_used_recent_originals')
            require(not db.execute("SELECT 1 FROM memory_fragments WHERE persona_id=? AND state='invalid'",(p['persona_id'],)).fetchone(),'repair_pending')
            reply=db.execute('SELECT text FROM events WHERE id=?',(turn['reply_event_id'],)).fetchone()[0]
            result={'passed':True,'at':timestamp(),'turn_id':turn['id'],'session_id':turn['session_id'],'corrected_memory_id':correction['memory_id'],'revision':correction['revision'],'context_hashes':[fingerprint(u) for u in uses],'reply_hash':fingerprint(reply),'clean_session':True,'recent_originals_absent':True,'summary_repair':correction['summary_repair'],'answer_semantic_confirmation':'pending'}
    write_new(out/('formal-'+position+'.json'),result)
    return {k:v for k,v in result.items() if k in ('passed','revision','automatic_publish','summary_repair','clean_session','answer_semantic_confirmation')}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('position',choices=('P1','P2','P3'));p.add_argument('--out',type=Path,required=True);a=p.parse_args();print(json.dumps(observe(a.out,a.position)))
