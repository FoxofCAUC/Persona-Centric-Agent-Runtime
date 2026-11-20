"""Bounded source choice and fairness. This module cannot dispatch models."""
import json
import re
from datetime import datetime
from .archive import binding,turn_scope
from .models import LOCAL_USER_ID,RuntimeErrorBase,timestamp
from .organization import encoded
from . import import_memory_scope as history_scope


def position(db,key):
    row=db.execute("SELECT position FROM memory_scan_positions WHERE name=?",(key,)).fetchone()
    return row[0] if row else 0


def advance(db,key,value):
    db.execute("INSERT INTO memory_scan_positions VALUES(?,?) ON CONFLICT(name) DO UPDATE SET position=excluded.position",(key,value))


def page(db,key,query,args,limit):
    start=position(db,key)
    rows=db.execute(query,(*args,start,limit)).fetchall()
    if not rows and start:rows=db.execute(query,(*args,0,limit)).fetchall()
    return rows


def scan_sessions(db,pid,settings,model,store=None):
    from .import_history import history_allowed
    sessions=page(db,'sessions/'+pid,"SELECT rowid,id FROM sessions WHERE persona_id=? AND rowid>? ORDER BY rowid LIMIT ?",(pid,),2)
    result=[]
    for ordinal,sid in sessions:
        from .ordinary_materials import full_revision
        full=store and full_revision(store,db,pid,sid,model,'organization')
        picks=set();moves={'sessions/'+pid:ordinal}
        for kind in ('native','imported'):
            imported=kind=='imported'
            if imported and not (settings.get('memory_backfill',True) and model.get('encrypted_key') and (full or history_allowed(db,sid,'memory',model))):continue
            key=kind+'/'+sid
            comparison="GLOB" if imported else "NOT GLOB"
            rows=page(db,key,f"""SELECT rowid,id FROM turns WHERE session_id=? AND status='completed'
                AND source_key {comparison} 'imported.*' AND rowid>? ORDER BY rowid LIMIT ?""",(sid,),16)
            if rows:moves[key]=rows[-1][0]
            picks.update(r[1] for r in rows)
            picks.update(r[0] for r in db.execute(f"""SELECT c.turn_id FROM memory_coverage c JOIN turns t ON t.id=c.turn_id
                WHERE c.persona_id=? AND c.disposition IN ('unprocessed','stale') AND t.session_id=?
                AND t.source_key {comparison} 'imported.*' ORDER BY t.rowid LIMIT 10""",(pid,sid)))
        result.append((sid,picks,moves))
    return result


def schedule(coordinator,db,pid,settings,enabled_at,pages,force=False):
    from .automatic_memory import blocked_sources
    now=coordinator.engine.managed.clock();choices={};scopes={}
    request=db.execute("SELECT * FROM memory_operations WHERE persona_id=? AND id LIKE 'manual-memory-scan:%' ORDER BY created_at DESC LIMIT 1",(pid,)).fetchone()
    manual=json.loads(request['result_json']) if request else None
    if manual and manual['policy_id']!=coordinator.engine.managed.policy(db,pid)['id']:manual=None
    unknown=blocked_sources(db,pid)
    model=coordinator.engine.model()
    from .copy_session_memory import has_decision,grant
    limited=has_decision(db,pid)
    for sid,picks,moves in pages:
        groups={}
        for key,value in moves.items():advance(db,key,value)
        from .ordinary_materials import full_revision,history_access
        full=full_revision(coordinator.store,db,pid,sid,model,'organization')
        if not picks or limited and not full and not grant(coordinator.store,db,pid,sid,model):continue
        query="SELECT rowid AS ordinal,* FROM turns WHERE persona_id=? AND session_id=? AND status='completed' AND id IN ("+','.join('?' for _ in picks)+") ORDER BY rowid"
        for row in db.execute(query,(pid,sid,*picks)):
            t=dict(row);selected=turn_scope(db,t)
            if limited and not full and history_scope.imported(t):continue
            if selected['channel'] not in settings['channels'] or selected['channel']=='qq' and (selected['bot'],selected['peer'])!=(settings['qq']['bot'],settings['qq']['peer']):continue
            key=encoded(selected);groups.setdefault(key,[]).append(t);scopes[key]=selected
        for key,turns in groups.items():
            selected=scopes[key]
            db.execute('INSERT OR IGNORE INTO memory_scopes VALUES(?,?,?,?,?,?)',(key,pid,LOCAL_USER_ID,key,datetime.fromisoformat(enabled_at).timestamp(),now))
            s=db.execute('SELECT * FROM memory_scopes WHERE scope_key=?',(key,)).fetchone()
            pending=[]
            for t in turns:
                with history_access(db,sid,bool(full)):
                    r=history_scope.source_group(db,selected,t,model=model)
                if r['status']!='found':
                    withdrawn=db.execute("""SELECT 1 FROM source_exclusions WHERE event_id IN (?,?)
                        OR event_id IN (SELECT event_id FROM input_batch_members WHERE batch_id=?) LIMIT 1""",
                        (t['user_event_id'],t['reply_event_id'],t['input_batch_id'])).fetchone()
                    if withdrawn:
                        db.execute("""INSERT INTO memory_coverage VALUES(?,?,?,?,?,NULL,'revoked','invalid',?)
                                   ON CONFLICT(turn_id) DO UPDATE SET disposition='revoked',summary_state='invalid',updated_at=excluded.updated_at""",
                                   (pid,LOCAL_USER_ID,t['id'],key,binding(r).get('hash',''),timestamp()))
                    continue
                c=db.execute('SELECT * FROM memory_coverage WHERE turn_id=?',(t['id'],)).fetchone();sha=binding(r)['hash']
                if not c:db.execute("INSERT INTO memory_coverage VALUES(?,?,?,?,?,NULL,'unprocessed','pending',?)",(pid,LOCAL_USER_ID,t['id'],key,sha,timestamp()))
                if c and c['source_hash']==sha and c['disposition'] not in ('unprocessed','stale'):continue
                if t['id'] not in unknown:pending.append(t)
            active=db.execute("SELECT 1 FROM memory_batches WHERE scope_json=? AND state IN ('extracting','verifying','waiting')",(key,)).fetchone()
            if active:continue
            lifecycle=db.execute('SELECT state FROM session_lifecycle WHERE session_id=?',(sid,)).fetchone()
            latest=db.execute("SELECT updated_at FROM turns WHERE session_id=? ORDER BY rowid DESC LIMIT 1",(sid,)).fetchone()
            sealed=lifecycle and lifecycle[0]=='sealed';idle=now-datetime.fromisoformat(latest[0]).timestamp()>=120
            overdue=any(now-datetime.fromisoformat(t['updated_at']).timestamp()>=600 for t in pending)
            ready=force or sealed or idle or overdue
            if ready:
                fresh=[t for t in pending if not history_scope.imported(t) and datetime.fromisoformat(t['created_at']).timestamp()>=s['enabled_epoch']]
                urgent=[t for t in fresh if t['user_event_id'] and re.search(r'更正|纠正|改为|以这次为准|\bcorrection\b',db.execute('SELECT text FROM events WHERE id=?',(t['user_event_id'],)).fetchone()[0],re.I)]
                historical=[t for t in pending if t not in fresh]
                trigger='sealed' if sealed else 'incremental'
                if trigger in settings['triggers']:
                    if urgent:choices.setdefault(key,[]).append((0,urgent[0]['ordinal'],sid,urgent,None,False,trigger))
                    if fresh and not urgent:choices.setdefault(key,[]).append((2,fresh[0]['ordinal'],sid,fresh,None,False,trigger))
                    if historical and settings.get('memory_backfill',True):choices.setdefault(key,[]).append((2 if now-s['backfill_epoch']>=600 else 3,historical[0]['ordinal'],sid,historical,None,True,trigger))
            if 'incremental' in settings['triggers']:
                for fragment in db.execute("SELECT * FROM memory_fragments WHERE session_id=? AND state='invalid' ORDER BY rowid LIMIT 4",(sid,)).fetchall():
                    p=json.loads(fragment['data_json'])['payload']
                    if p['scope']!=selected:continue
                    ids={b['turn_id'] for b in p['bindings']}
                    if ids & unknown:continue
                    originals = db.execute("SELECT rowid AS ordinal,* FROM turns WHERE session_id=? AND id IN ("+
                                           ",".join("?" for _ in ids)+") ORDER BY rowid",(sid,*ids))
                    remaining = [dict(t) for t in originals if history_scope.source_group(db,selected,dict(t),model=model)['status']=='found']
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
