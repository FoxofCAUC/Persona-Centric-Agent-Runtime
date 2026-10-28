"""Transactional, local-only ingress aggregation. Network calls run after commit."""
import asyncio
import hashlib
import json
import time
import sqlite3
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, require_text, timestamp
from .profile import snapshot
from .character import head as character_head
from .progress import record
from .context_policy import DEFAULT_CONTEXT, values as context_values, snapshot as context_snapshot

PARAMS={'schema_version':1,'quiet_ms':5000,'max_wait_ms':15000,'max_messages':20,'max_chars':12000,'max_batches':10,'max_pending_chars':120000}
MODES=('legacy_per_message','natural_batch')
REPLY_MODES=('legacy_full','context_full','context_structured')

def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()

class IngressService:
    def __init__(self,engine,monotonic=time.monotonic,epoch=time.time):
        self.engine=engine;self.store=engine.store;self.config=engine.config
        self.monotonic=monotonic;self.epoch=epoch;self.clocks={}

    def recover(self):
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE input_batches SET state='interrupted_buffer',revision=revision+1,updated_at=? WHERE state IN ('collecting','sealed_waiting_capacity')",(timestamp(),))
        self.clocks.clear()

    def policy(self,conn,persona_id,channel):
        if channel not in ('desktop','qq'):raise RuntimeErrorBase('unsupported_channel')
        row=conn.execute('SELECT p.* FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.persona_id=? AND h.channel=?',(persona_id,channel)).fetchone()
        result=dict(row) if row else {'id':None,'persona_id':persona_id,'channel':channel,'revision':0,'mode':'legacy_per_message','reply_mode':'legacy_full','data_json':encoded({**PARAMS,'context':DEFAULT_CONTEXT})}
        return result|context_values(result)

    def current_policy(self,persona_id,channel):
        with self.store.connection() as conn:
            self.engine.service._persona(conn,persona_id)
            return self.policy(conn,persona_id,channel)

    def _operation(self,conn,persona,operation_id,value):
        require_text(operation_id,'operation_id')
        if len(operation_id)>200:raise RuntimeErrorBase('invalid_operation_id')
        fingerprint=digest(value)
        row=conn.execute('SELECT * FROM batch_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if row:
            if row['persona_id']!=persona or row['fingerprint']!=fingerprint:raise RuntimeErrorBase('submission_conflict')
            return fingerprint,json.loads(row['result_json'])
        return fingerprint,None

    def _remember(self,conn,persona,operation_id,fingerprint,result):
        conn.execute('INSERT INTO batch_operations VALUES (?,?,?,?,?,?)',(operation_id,persona,LOCAL_USER_ID,fingerprint,encoded(result),timestamp()))

    def update_policy(self,persona_id,channel,mode,expected_version,operation_id,pending_action=None,reply_mode=None,context_mode=None,cross_session=None):
        if mode not in MODES:raise RuntimeErrorBase('invalid_interaction_mode')
        if reply_mode is not None and reply_mode not in REPLY_MODES:raise RuntimeErrorBase('invalid_reply_mode')
        if context_mode is not None and context_mode not in ('working_set','legacy_recent'):raise RuntimeErrorBase('invalid_context_mode')
        if cross_session is not None and type(cross_session) is not bool:raise RuntimeErrorBase('invalid_cross_session')
        with self.store.connection(write=True) as conn:
            if self.store.current(conn)!=persona_id:raise RuntimeErrorBase('persona_selection_conflict')
            current=self.policy(conn,persona_id,channel)
            value={'channel':channel,'mode':mode,'reply_mode':reply_mode,'context_mode':context_mode,'cross_session':cross_session,'expected_version':expected_version,'pending_action':pending_action}
            fp,old=self._operation(conn,persona_id,operation_id,value)
            if old is not None:return old
            if current['id']!=expected_version:raise RuntimeErrorBase('version_conflict')
            reply_mode=reply_mode or current['reply_mode']
            context_mode=context_mode or current['context_mode']
            cross_session=current['cross_session'] if cross_session is None else cross_session
            context_changed=(context_mode,cross_session)!=(current['context_mode'],current['cross_session'])
            pending=conn.execute("SELECT * FROM input_batches WHERE persona_id=? AND channel=? AND state='collecting' ORDER BY first_seq",(persona_id,channel)).fetchall() if mode!=current['mode'] or context_changed else []
            if pending and pending_action not in ('seal','cancel'):raise RuntimeErrorBase('pending_batch_resolution_required')
            for row in pending:
                if pending_action=='seal':self._seal(conn,dict(row),'policy_changed')
                else:conn.execute("UPDATE input_batches SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",(timestamp(),row['id']))
            if current['mode']==mode and current['reply_mode']==reply_mode and not context_changed:result=current
            else:
                vid=identifier()
                data=json.loads(current['data_json']);data['context']={'context_mode':context_mode,'cross_session':cross_session}
                conn.execute('INSERT INTO interaction_policies (id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES (?,?,?,?,?,?,?,?,?)',(vid,persona_id,LOCAL_USER_ID,channel,current['revision']+1,mode,encoded(data),timestamp(),reply_mode))
                conn.execute('INSERT INTO interaction_heads VALUES (?,?,?,?) ON CONFLICT(persona_id,channel) DO UPDATE SET version_id=excluded.version_id',(persona_id,LOCAL_USER_ID,channel,vid))
                result=self.policy(conn,persona_id,channel)
            self._remember(conn,persona_id,operation_id,fp,result)
        self.schedule_committed();return result

    def _batch(self,conn,batch_id):
        row=conn.execute('SELECT * FROM input_batches WHERE id=?',(batch_id,)).fetchone()
        if not row:raise RuntimeErrorBase('batch_not_found')
        return dict(row)

    def _view(self,conn,batch_id):
        row=self._batch(conn,batch_id)
        for key in ('profile_snapshot','character_snapshot','model_snapshot','scope_key'):row.pop(key,None)
        row['policy']=json.loads(row.pop('policy_json'))
        row['origin']={'bot':row['bot'],'sender':row['peer']} if row['channel']=='qq' else None
        row['members']=[dict(r) for r in conn.execute('SELECT r.id AS receipt_id,r.accepted_seq,e.id AS event_id,e.text,e.created_at FROM input_batch_members m JOIN ingress_receipts r ON r.id=m.receipt_id JOIN events e ON e.id=m.event_id WHERE m.batch_id=? ORDER BY m.member_order',(batch_id,))]
        turn=conn.execute('SELECT id FROM turns WHERE input_batch_id=?',(batch_id,)).fetchone()
        row['turn_id']=turn[0] if turn else None
        row['status']=self.engine._view(conn,turn[0])['status'] if turn else row['state']
        clock=self.clocks.get(batch_id)
        row['remaining_ms']=max(0,round((clock[2]-self.monotonic())*1000)) if clock and row['state']=='collecting' else 0
        if clock and row['state']=='collecting':row['buffer_ms']=max(0,round((self.monotonic()-clock[0])*1000))
        return row

    def get(self,batch_id):
        with self.store.connection() as conn:return self._view(conn,batch_id)

    def list(self,persona_id,session_id=None,channel=None):
        with self.store.connection() as conn:
            self.engine.service._persona(conn,persona_id)
            return [self._view(conn,r[0]) for r in conn.execute('SELECT id FROM input_batches WHERE persona_id=? AND (? IS NULL OR session_id=?) AND (? IS NULL OR channel=?) ORDER BY first_seq',(persona_id,session_id,session_id,channel,channel))]

    def _receipt(self,conn,row):
        batch=self._view(conn,row['batch_id'])
        return {'receipt_id':row['id'],'batch_id':row['batch_id'],'accepted_seq':row['accepted_seq'],'persona_id':row['persona_id'],'session_id':row['session_id'],'channel':row['channel'],'origin':batch['origin'],'turn_id':batch['turn_id'],'status':batch['status'],'batch_revision':batch['revision'],'ingress_ms':row['ingress_ms']}

    def lookup(self,channel,request_id):
        with self.store.connection() as conn:
            row=conn.execute('SELECT * FROM ingress_receipts WHERE channel=? AND request_id=?',(channel,request_id)).fetchone()
            return self._receipt(conn,row) if row else None

    def accept(self,value):
        started=time.perf_counter()
        allowed={'channel','text','request_id','persona_id','session_id','bot','sender','platform_message_id','platform_time','reference'}
        if not isinstance(value,dict) or set(value)-allowed:raise RuntimeErrorBase('unknown_ingress_field')
        channel=value.get('channel','desktop');text=value.get('text');rid=value.get('request_id')
        require_text(text,'text');require_text(rid,'request_id')
        if channel not in ('desktop','qq') or len(text)>24000 or len(rid)>200:raise RuntimeErrorBase('invalid_ingress_input')
        if self.engine.draining:raise RuntimeErrorBase('service_draining')
        from .archive import target_spec
        if 'reference' in value:value={**value,'reference':target_spec(value['reference'],channel)}
        cfg=self.config.load();bot=peer=''
        if channel=='qq':
            bot=value.get('bot');peer=value.get('sender');mid=value.get('platform_message_id')
            if not cfg['bot_qq'] or not cfg['owner_qq'] or bot!=cfg['bot_qq'] or peer!=cfg['owner_qq']:raise RuntimeErrorBase('qq_identity_conflict')
            require_text(mid,'platform_message_id')
            if len(mid)>200:raise RuntimeErrorBase('invalid_platform_message_id')
            source=encoded([channel,bot,peer,mid])
        else:
            if any(k in value for k in ('bot','sender','platform_message_id','platform_time')):raise RuntimeErrorBase('invalid_desktop_metadata')
            source=encoded([channel,rid])
        if value.get('platform_time') is not None and (not isinstance(value['platform_time'],str) or len(value['platform_time'])>80):raise RuntimeErrorBase('invalid_platform_time')
        payload={k:v for k,v in value.items() if k not in ('persona_id','session_id')};payload['channel']=channel
        fp=digest(payload);now=self.epoch();mono=self.monotonic();clock_updates={}
        with self.store.connection(write=True) as conn:
            old=conn.execute('SELECT * FROM ingress_receipts WHERE source_key=? OR (channel=? AND request_id=?)',(source,channel,rid)).fetchone()
            if old:
                if old['source_key']!=source or old['fingerprint']!=fp or (value.get('persona_id') and old['persona_id']!=value['persona_id']) or (value.get('session_id') and old['session_id']!=value['session_id']):raise RuntimeErrorBase('submission_conflict')
                return self._receipt(conn,old)
            # A v1 intent cannot be silently replayed through v2 after upgrade.
            if conn.execute('SELECT 1 FROM turns WHERE source_key=?',(channel+':'+rid,)).fetchone():raise RuntimeErrorBase('legacy_submission_exists')
            pid=self.engine.service._persona(conn,value.get('persona_id'))['id']
            sid=self.engine._session(conn,pid,channel,value.get('session_id'))
            pol=self.policy(conn,pid,channel);policy={**PARAMS,'id':pol['id'],'mode':pol['mode'],'reply_mode':pol['reply_mode'],'revision':pol['revision'],'context':context_snapshot(pol)}
            frozen=snapshot(conn,pid);character=character_head(conn,pid)
            scope=digest([pid,LOCAL_USER_ID,channel,bot,peer,sid,policy,frozen['core']['id'],frozen['expression']['id'],frozen['relationship']['id'],character['id']])
            # A change in settings or session seals collecting batches at the prior boundary.
            for row in conn.execute("SELECT * FROM input_batches WHERE persona_id=? AND channel=? AND bot=? AND peer=? AND state='collecting' AND scope_key!=? ORDER BY first_seq",(pid,channel,bot,peer,scope)).fetchall():self._seal(conn,dict(row),'scope_changed')
            row=conn.execute("SELECT * FROM input_batches WHERE scope_key=? AND state='collecting'",(scope,)).fetchone()
            batch=dict(row) if row else None
            if batch:
                count,chars=conn.execute('SELECT count(*),coalesce(sum(length(e.text)),0) FROM input_batch_members m JOIN events e ON e.id=m.event_id WHERE m.batch_id=?',(batch['id'],)).fetchone()
                deadline=self.clocks.get(batch['id'])
                if (deadline and mono>=deadline[2]) or count>=20 or chars+len(text)>12000:
                    self._seal(conn,batch,'deadline' if deadline and mono>=deadline[2] else 'capacity');batch=None
            active=conn.execute("SELECT b.id FROM input_batches b LEFT JOIN turns t ON t.input_batch_id=b.id WHERE b.persona_id=? AND b.channel=? AND b.bot=? AND b.peer=? AND b.state!='cancelled' AND (t.id IS NULL OR t.status IN ('queued','running'))",(pid,channel,bot,peer)).fetchall()
            active_ids=[r[0] for r in active]
            chars=0
            for bid in active_ids:chars+=conn.execute('SELECT coalesce(sum(length(e.text)),0) FROM input_batch_members m JOIN events e ON e.id=m.event_id WHERE m.batch_id=?',(bid,)).fetchone()[0]
            if (not batch and len(active_ids)>=10) or chars+len(text)>120000:raise RuntimeErrorBase('buffer_full')
            if pol['mode']=='legacy_per_message' and conn.execute("SELECT count(*) FROM turns WHERE persona_id=? AND status='queued'",(pid,)).fetchone()[0]>=10:raise RuntimeErrorBase('queue_full')
            bid=batch['id'] if batch else identifier();eid=identifier();rawid=identifier();receipt=identifier()
            seq=conn.execute('SELECT coalesce(max(accepted_seq),0)+1 FROM ingress_receipts').fetchone()[0]
            if not batch:
                conn.execute('INSERT INTO input_batches (id,persona_id,user_id,session_id,channel,bot,peer,scope_key,state,revision,first_seq,first_epoch,last_epoch,deadline_epoch,max_deadline_epoch,sealed_at,seal_reason,policy_json,profile_snapshot,character_snapshot,model_snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(bid,pid,LOCAL_USER_ID,sid,channel,bot,peer,scope,'collecting',1,seq,now,now,now+5,now+15,None,None,encoded(policy),encoded(frozen),encoded(character),None,timestamp(),timestamp()))
            conn.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES (?,?,?,?,'user',?,?)",(eid,sid,pid,LOCAL_USER_ID,text,timestamp()))
            conn.execute('INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(rawid,pid,LOCAL_USER_ID,eid,channel,bot,peer,value.get('platform_message_id'),value.get('platform_time'),timestamp(),'normalized_payload',2 if 'reference' in value else 1,encoded(payload),fp))
            conn.execute('INSERT INTO ingress_receipts(id,source_key,request_id,persona_id,user_id,channel,bot,peer,session_id,fingerprint,raw_event_id,event_id,batch_id,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(receipt,source,rid,pid,LOCAL_USER_ID,channel,bot,peer,sid,fp,rawid,eid,bid,timestamp()))
            if 'reference' in value:
                from .archive import scope as archive_scope
                from .references import capture
                capture(conn,eid,archive_scope(pid,channel,bot,peer),value['reference'])
            order=conn.execute('SELECT count(*)+1 FROM input_batch_members WHERE batch_id=?',(bid,)).fetchone()[0]
            conn.execute('INSERT INTO input_batch_members VALUES (?,?,?,?,?,?)',(bid,pid,LOCAL_USER_ID,receipt,eid,order))
            first_mono=self.clocks[bid][0] if batch else mono
            deadline_mono=min(mono+5,first_mono+15)
            max_epoch=batch['max_deadline_epoch'] if batch else now+15
            conn.execute('UPDATE input_batches SET last_epoch=?,deadline_epoch=?,revision=revision+1,updated_at=? WHERE id=?',(now,min(now+5,max_epoch),timestamp(),bid))
            clock_updates[bid]=(first_mono,mono,deadline_mono)
            chars=conn.execute('SELECT sum(length(e.text)) FROM input_batch_members m JOIN events e ON e.id=m.event_id WHERE m.batch_id=?',(bid,)).fetchone()[0]
            if pol['mode']=='legacy_per_message' or order>=20 or chars>=12000:self._seal(conn,self._batch(conn,bid),'legacy' if pol['mode']=='legacy_per_message' else 'capacity')
            conn.execute('UPDATE ingress_receipts SET ingress_ms=? WHERE id=?',((time.perf_counter()-started)*1000,receipt))
            result=self._receipt(conn,conn.execute('SELECT * FROM ingress_receipts WHERE id=?',(receipt,)).fetchone())
        self.clocks.update(clock_updates);self.schedule_committed();return result

    def _seal(self,conn,batch,reason):
        if batch['state'] not in ('collecting','interrupted_buffer','sealed_waiting_capacity'):return
        if batch['state']!='sealed_waiting_capacity':
            clock=self.clocks.get(batch['id'])
            elapsed=max(0,(self.monotonic()-clock[0])*1000) if clock else max(0,(self.epoch()-batch['first_epoch'])*1000)
            conn.execute('UPDATE input_batches SET buffer_ms=? WHERE id=?',(elapsed,batch['id']))
            conn.execute("UPDATE input_batches SET state='sealed_waiting_capacity',sealed_at=?,seal_reason=?,model_snapshot=?,revision=revision+1,updated_at=? WHERE id=?",(timestamp(),reason,encoded(self.config.load()['model']),timestamp(),batch['id']))
        self._enqueue(conn,batch['persona_id'])

    def _enqueue(self,conn,persona_id):
        capacity=10-conn.execute("SELECT count(*) FROM turns WHERE persona_id=? AND status='queued'",(persona_id,)).fetchone()[0]
        waiting=conn.execute("SELECT * FROM input_batches WHERE persona_id=? AND state='sealed_waiting_capacity' ORDER BY first_seq LIMIT ?",(persona_id,max(0,capacity))).fetchall()
        for row in waiting:
            b=dict(row)
            # Earlier collecting/interrupted accepted input must be explicitly resolved first.
            if conn.execute("SELECT 1 FROM input_batches WHERE persona_id=? AND first_seq<? AND state IN ('collecting','interrupted_buffer')",(persona_id,b['first_seq'])).fetchone():break
            tid=identifier();first=conn.execute('SELECT event_id FROM input_batch_members WHERE batch_id=? ORDER BY member_order LIMIT 1',(b['id'],)).fetchone()[0]
            origin=encoded({'bot':b['bot'],'sender':b['peer']}) if b['channel']=='qq' else 'null'
            policy=json.loads(b['policy_json'])
            conn.execute("INSERT INTO turns(id,source_key,persona_id,session_id,channel,user_event_id,status,model_snapshot,created_at,updated_at,profile_snapshot,character_snapshot,origin_json,input_batch_id,reply_mode_snapshot) VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)",(tid,'batch:'+b['id'],persona_id,b['session_id'],b['channel'],first,b['model_snapshot'],timestamp(),timestamp(),b['profile_snapshot'],b['character_snapshot'],origin,b['id'],policy.get('reply_mode','legacy_full')))
            if policy.get('context') is not None:
                conn.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(encoded(policy['context']),tid))
            if b['channel']=='qq':conn.execute("INSERT INTO deliveries VALUES (?,'pending',?)",(tid,timestamp()))
            record(conn,tid,'accepted');record(conn,tid,'queued')
            conn.execute("UPDATE input_batches SET state='linked',revision=revision+1,updated_at=? WHERE id=?",(timestamp(),b['id']))

    def schedule_committed(self):
        # Called only after the write transaction succeeds.
        with self.store.connection() as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM turns WHERE input_batch_id IS NOT NULL AND status='queued' ORDER BY rowid")]
        for tid in ids:
            if tid not in self.engine.tasks:self.engine._schedule(tid)

    def action(self,batch_id,action,expected_revision,operation_id,persona_id):
        with self.store.connection(write=True) as conn:
            batch=self._batch(conn,batch_id)
            if batch['persona_id']!=persona_id or self.store.current(conn)!=persona_id:raise RuntimeErrorBase('persona_selection_conflict')
            fp,old=self._operation(conn,persona_id,operation_id,{'batch_id':batch_id,'action':action,'expected_revision':expected_revision})
            if old is not None:return old
            if type(expected_revision) is not int or batch['revision']!=expected_revision:raise RuntimeErrorBase('version_conflict')
            if action not in ('seal','resume','cancel'):raise RuntimeErrorBase('invalid_batch_action')
            if batch['state'] not in ('collecting','interrupted_buffer','sealed_waiting_capacity'):raise RuntimeErrorBase('batch_already_linked')
            if action!='cancel' and self.engine.draining:raise RuntimeErrorBase('service_draining')
            if action=='resume' and batch['state']!='interrupted_buffer':raise RuntimeErrorBase('batch_not_interrupted')
            if action=='seal' and batch['state']!='collecting':raise RuntimeErrorBase('batch_not_collecting')
            if action=='cancel':conn.execute("UPDATE input_batches SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",(timestamp(),batch_id))
            else:self._seal(conn,batch,'explicit_'+action)
            self._enqueue(conn,persona_id)
            result=self._view(conn,batch_id);self._remember(conn,persona_id,operation_id,fp,result)
        self.schedule_committed();return result

    def unresolved_count(self):
        with self.store.connection() as conn:return conn.execute("SELECT count(*) FROM input_batches WHERE state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')").fetchone()[0]

    def tick(self):
        if self.engine.draining:return
        with self.store.connection(write=True) as conn:
            for row in conn.execute("SELECT * FROM input_batches WHERE state='collecting' ORDER BY first_seq").fetchall():
                clock=self.clocks.get(row['id'])
                if clock and self.monotonic()>=clock[2]:self._seal(conn,dict(row),'deadline')
            for row in conn.execute("SELECT DISTINCT persona_id FROM input_batches WHERE state='sealed_waiting_capacity'").fetchall():self._enqueue(conn,row[0])
        self.schedule_committed()
        with self.store.connection() as conn:live={r[0] for r in conn.execute("SELECT id FROM input_batches WHERE state='collecting'")}
        self.clocks={k:v for k,v in self.clocks.items() if k in live}

    async def worker(self):
        while True:
            try:self.tick()
            except (OSError,RuntimeErrorBase,sqlite3.Error):pass
            await asyncio.sleep(.05)
