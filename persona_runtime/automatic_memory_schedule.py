"""Bounded source choice and fairness. This module cannot dispatch models."""
import json
import re
from datetime import datetime
from .archive import read_exact,binding,turn_scope
from .models import LOCAL_USER_ID,RuntimeErrorBase,timestamp
from .organization import encoded


def schedule(coordinator,db,pid,settings,enabled_at,force=False):
    from .automatic_memory import blocked_sources
    now=coordinator.engine.managed.clock();choices={};scopes={}
    request=db.execute("SELECT * FROM memory_operations WHERE persona_id=? AND id LIKE 'manual-memory-scan:%' ORDER BY created_at DESC LIMIT 1",(pid,)).fetchone()
    manual=json.loads(request['result_json']) if request else None
    if manual and manual['policy_id']!=coordinator.engine.managed.policy(db,pid)['id']:manual=None
    unknown=blocked_sources(db,pid)
    for (sid,) in db.execute('SELECT id FROM sessions WHERE persona_id=? ORDER BY created_at,id',(pid,)).fetchall():
        groups={}
        for row in db.execute("SELECT rowid AS ordinal,* FROM turns WHERE session_id=? AND status='completed' ORDER BY rowid",(sid,)):
            t=dict(row);selected=turn_scope(db,t)
            if selected['channel'] not in settings['channels'] or selected['channel']=='qq' and (selected['bot'],selected['peer'])!=(settings['qq']['bot'],settings['qq']['peer']):continue
            key=encoded(selected);groups.setdefault(key,[]).append(t);scopes[key]=selected
        for key,turns in groups.items():
            selected=scopes[key]
            db.execute('INSERT OR IGNORE INTO memory_scopes VALUES(?,?,?,?,?,?)',(key,pid,LOCAL_USER_ID,key,datetime.fromisoformat(enabled_at).timestamp(),now))
            s=db.execute('SELECT * FROM memory_scopes WHERE scope_key=?',(key,)).fetchone()
            pending=[]
            for t in turns:
                r=read_exact(db,selected,{'kind':'turn','id':t['id']})
                if r['status']!='found':continue
                c=db.execute('SELECT * FROM memory_coverage WHERE turn_id=?',(t['id'],)).fetchone();sha=binding(r)['hash']
                if not c:db.execute("INSERT INTO memory_coverage VALUES(?,?,?,?,?,NULL,'unprocessed','pending',?)",(pid,LOCAL_USER_ID,t['id'],key,sha,timestamp()))
                if c and c['source_hash']==sha and c['disposition'] not in ('unprocessed','stale'):continue
                if t['id'] not in unknown:pending.append(t)
            active=db.execute("SELECT 1 FROM memory_batches WHERE scope_json=? AND state IN ('extracting','verifying','waiting')",(key,)).fetchone()
            if active:continue
            lifecycle=db.execute('SELECT state FROM session_lifecycle WHERE session_id=?',(sid,)).fetchone()
            sealed=lifecycle and lifecycle[0]=='sealed';idle=now-max(datetime.fromisoformat(t['updated_at']).timestamp() for t in turns)>=120
            ready=force or sealed or idle
            if ready:
                fresh=[t for t in pending if datetime.fromisoformat(t['created_at']).timestamp()>=s['enabled_epoch']]
                urgent=[t for t in fresh if re.search(r'更正|纠正|改为|以这次为准|\bcorrection\b',db.execute('SELECT text FROM events WHERE id=?',(t['user_event_id'],)).fetchone()[0],re.I)]
                historical=[t for t in pending if t not in fresh]
                trigger='sealed' if sealed else 'incremental'
                if trigger in settings['triggers']:
                    if urgent:choices.setdefault(key,[]).append((0,urgent[0]['ordinal'],sid,urgent,None,False,trigger))
                    if fresh and not urgent:choices.setdefault(key,[]).append((2,fresh[0]['ordinal'],sid,fresh,None,False,trigger))
                    if historical and settings.get('memory_backfill',True):choices.setdefault(key,[]).append((2 if now-s['backfill_epoch']>=600 else 3,historical[0]['ordinal'],sid,historical,None,True,trigger))
            if 'incremental' in settings['triggers']:
                for fragment in db.execute("SELECT * FROM memory_fragments WHERE session_id=? AND state='invalid' ORDER BY rowid",(sid,)).fetchall():
                    p=json.loads(fragment['data_json'])['payload']
                    if p['scope']!=selected:continue
                    ids={b['turn_id'] for b in p['bindings']}
                    if ids & unknown:continue
                    remaining=[t for t in turns if t['id'] in ids and read_exact(db,selected,{'kind':'turn','id':t['id']})['status']=='found']
                    if not remaining:db.execute("UPDATE memory_fragments SET state='superseded' WHERE id=?",(fragment['id'],));continue
                    choices.setdefault(key,[]).append((1,remaining[0]['ordinal'],sid,remaining,fragment['id'],False,'incremental'))
    for key,work in choices.items():
        if db.execute("SELECT 1 FROM memory_batches WHERE scope_json=? AND state IN ('extracting','verifying','waiting')",(key,)).fetchone():continue
        for priority,_,sid,turns,previous,backfill,trigger in sorted(work,key=lambda w:w[:2]):
            p=coordinator.payload(db,pid,sid,scopes[key],turns,purpose='repair' if previous else 'extract',previous=previous)
            if manual and p['rows'] and max(p['rows'])<=manual['upper_order']:p['automatic_memory']['manual_operation']=request['id']
            if previous:
                f=db.execute('SELECT data_json FROM memory_fragments WHERE id=?',(previous,)).fetchone();op=json.loads(f[0]).get('manual_rebuild_operation')
                if op:p['automatic_memory']['manual_operation']=op
            if not p['rows']:
                for t in turns[:1]:db.execute("UPDATE memory_coverage SET disposition='input_too_long',summary_state='unavailable' WHERE turn_id=?",(t['id'],))
                continue
            p['automatic_memory']['urgency']='correction' if priority==0 else 'backfill' if backfill else 'normal'
            try:b=coordinator.begin(db,p,trigger)
            except RuntimeErrorBase as error:
                if str(error)!='organization_queue_full':raise
                break
            if b and b['state'] in ('extracting','verifying','waiting'):
                if backfill:db.execute('UPDATE memory_scopes SET backfill_epoch=? WHERE scope_key=?',(now,key))
                break
