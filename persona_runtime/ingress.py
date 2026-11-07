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
ROLLING_PARAMS={**PARAMS,'schema_version':2,'max_wait_ms':None}

def buffer_policy(policy):
    data=json.loads(policy['data_json']) if 'data_json' in policy else policy
    version=data.get('schema_version',1)
    defaults=ROLLING_PARAMS if version==2 else PARAMS
    result={k:data.get(k,v) for k,v in defaults.items()}
    if version not in (1,2) or result['quiet_ms']!=5000 or result['max_wait_ms']!=(None if version==2 else 15000):
        raise RuntimeErrorBase('buffer_policy_invalid')
    return result

MODES=('legacy_per_message','natural_batch')
from .reply_batch import REPLY_MODES, expression_snapshot

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
        data=json.loads(result['data_json'])
        delivery=data.get('delivery')
        if delivery is None and not data.get('companion') and conn.execute(
                "SELECT 1 FROM interaction_policies WHERE persona_id=? AND channel=? AND json_extract(data_json,'$.companion') IS NOT NULL LIMIT 1",
                (persona_id,channel)).fetchone():
            delivery={'version':1,'source':'historical','state':'disabled'}
        return result|context_values(result)|{'buffer_policy':buffer_policy(result),'expression_policy':data.get('expression'),
            'delivery':delivery}

    def current_policy(self,persona_id,channel):
        with self.store.connection() as conn:
            self.engine.service._persona(conn,persona_id)
            admitted=False
            if getattr(self.engine,'assist_admission',None):
                from .decision_capsule import require_admission
                try:require_admission(self.engine,persona_id)
                except RuntimeErrorBase:pass
                else:admitted=True
            try:
                from .decision_capsule import require_admission
                require_admission(self.engine,persona_id,'live');live=True
            except RuntimeErrorBase:live=False
            return self.policy(conn,persona_id,channel)|{'assist_admitted':admitted,'live_admitted':live}

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

    def update_policy(self,persona_id,channel,mode,expected_version,operation_id,pending_action=None,disposition_mode=None,reply_mode=None,context_mode=None,cross_session=None,character_retrieval_mode=None,expected_selected=None,conversation_policy_version=None,expression_policy_version=None,situation_observation_mode=None,companion_mode=None,delivery_source=None):
        if delivery_source not in (None,'user','release') or delivery_source is not None and companion_mode in (None,'disabled'):
            raise RuntimeErrorBase('invalid_delivery_source')
        if companion_mode=='disabled':
            if any(value is not None for value in (disposition_mode,reply_mode,context_mode,cross_session,
                    character_retrieval_mode,conversation_policy_version,expression_policy_version,situation_observation_mode)):
                raise RuntimeErrorBase('companion_restore_conflict')
            return self._disable_companion_policy(persona_id,channel,mode,expected_version,operation_id,
                pending_action,expected_selected)
        if conversation_policy_version not in (None,2):raise RuntimeErrorBase('conversation_policy_invalid')
        if expression_policy_version not in (None,3):raise RuntimeErrorBase('expression_policy_invalid')
        if mode not in MODES:raise RuntimeErrorBase('invalid_interaction_mode')
        if reply_mode is not None and reply_mode not in REPLY_MODES:raise RuntimeErrorBase('invalid_reply_mode')
        if context_mode is not None and context_mode not in ('working_set','legacy_recent'):raise RuntimeErrorBase('invalid_context_mode')
        if cross_session is not None and type(cross_session) is not bool:raise RuntimeErrorBase('invalid_cross_session')
        from .character_policy import choose
        if situation_observation_mode is not None and situation_observation_mode not in ('disabled','on_demand'):raise RuntimeErrorBase('invalid_observation_mode')
        requested_observation_mode=situation_observation_mode
        requested_disposition_mode=disposition_mode
        from .companion import choose as choose_companion
        selected_companion=choose_companion(companion_mode) if companion_mode is not None else None
        selected_character=choose(character_retrieval_mode) if character_retrieval_mode is not None else None
        with self.store.connection(write=True) as conn:
            if self.store.current(conn)!=(persona_id if expected_selected is None else expected_selected):raise RuntimeErrorBase('persona_selection_conflict')
            self.engine.service._persona(conn,persona_id)
            current=self.policy(conn,persona_id,channel)
            situation_observation_mode=current.get('situation_observation_mode','disabled') if situation_observation_mode is None else situation_observation_mode
            observation_changed=situation_observation_mode!=current.get('situation_observation_mode','disabled')
            disposition_mode=current.get('disposition_mode','baseline') if disposition_mode is None else disposition_mode
            if disposition_mode not in ('baseline','shadow','assist','live'):raise RuntimeErrorBase('invalid_disposition_mode')
            effective_companion=selected_companion or current.get('companion')
            if effective_companion and effective_companion['mode']=='minimal':
                if requested_disposition_mode not in (None,'baseline') or requested_observation_mode not in (None,'disabled'):
                    raise RuntimeErrorBase('companion_policy_conflict')
                if character_retrieval_mode not in (None,'hybrid'):
                    raise RuntimeErrorBase('companion_policy_conflict')
                disposition_mode='baseline';situation_observation_mode='disabled'
                observation_changed=situation_observation_mode!=current.get('situation_observation_mode','disabled')
                selected_character=choose('hybrid')
            if disposition_mode in ('assist','live'):
                from .decision_capsule import require_admission
                require_admission(self.engine,persona_id,disposition_mode)
            value={'channel':channel,'mode':mode,'reply_mode':reply_mode,'context_mode':context_mode,'cross_session':cross_session,'expected_version':expected_version,'pending_action':pending_action}
            if requested_disposition_mode is not None:value['disposition_mode']=requested_disposition_mode
            if requested_observation_mode is not None:value['situation_observation_mode']=requested_observation_mode
            if character_retrieval_mode is not None:value['character_retrieval_mode']=character_retrieval_mode
            if expected_selected is not None:value['expected_selected']=expected_selected
            if conversation_policy_version is not None:value['conversation_policy_version']=conversation_policy_version
            if expression_policy_version is not None:value['expression_policy_version']=expression_policy_version
            if companion_mode is not None:value['companion_mode']=companion_mode
            if delivery_source is not None:value['delivery_source']=delivery_source
            old_data=json.loads(current['data_json'])
            companion_changed=selected_companion is not None and selected_companion!=old_data.get('companion')
            from .reply_batch import expression_version
            old_expression=expression_version(old_data.get('expression'))
            target_expression=expression_policy_version or max(old_expression,2 if conversation_policy_version==2 else 1)
            from .companion import CONTRACT_VERSIONS, NATIVE_VERSIONS
            if (effective_companion and effective_companion['version'] in CONTRACT_VERSIONS
                    and ((reply_mode or current['reply_mode'])!='natural_structured' or target_expression!=3)):
                raise RuntimeErrorBase('companion_format_conflict')
            if (effective_companion and effective_companion['version'] in NATIVE_VERSIONS
                    and ((reply_mode or current['reply_mode']) != 'context_full' or target_expression != 3)):
                raise RuntimeErrorBase('companion_format_conflict')
            from .context_policy import delivery_metadata
            delivery=delivery_metadata(delivery_source or 'user','enabled') if selected_companion is not None else old_data.get('delivery')
            strategy_changed=companion_changed or delivery!=old_data.get('delivery') or (conversation_policy_version==2 and buffer_policy(current)!=ROLLING_PARAMS) or target_expression!=old_expression
            fp,old=self._operation(conn,persona_id,operation_id,value)
            if old is not None:return old
            if current['id']!=expected_version:raise RuntimeErrorBase('version_conflict')
            if companion_mode in ('minimal_chat','minimal_persona','minimal_context_rules_off','minimal_experience','minimal_native','minimal_im','minimal_im_selective') and companion_changed and old_data.get('companion'):
                from .companion import restore_available
                if not restore_available(current):raise RuntimeErrorBase('companion_restore_missing')
            reply_mode=reply_mode or current['reply_mode']
            context_mode=context_mode or current['context_mode']
            cross_session=current['cross_session'] if cross_session is None else cross_session
            character_policy=selected_character if selected_character is not None else current['character_retrieval']
            context_changed=observation_changed or (context_mode,cross_session,character_policy)!=(current['context_mode'],current['cross_session'],current['character_retrieval'])
            pending=conn.execute("SELECT * FROM input_batches WHERE persona_id=? AND channel=? AND state='collecting' ORDER BY first_seq",(persona_id,channel)).fetchall() if mode!=current['mode'] or context_changed or reply_mode!=current['reply_mode'] or strategy_changed or disposition_mode!=current.get('disposition_mode','baseline') else []
            if pending and pending_action not in ('seal','cancel'):raise RuntimeErrorBase('pending_batch_resolution_required')
            for row in pending:
                if pending_action=='seal':self._seal(conn,dict(row),'policy_changed')
                else:conn.execute("UPDATE input_batches SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",(timestamp(),row['id']))
            if current['mode']==mode and current['reply_mode']==reply_mode and not context_changed and not strategy_changed and disposition_mode==current.get('disposition_mode','baseline'):result=current
            else:
                vid=identifier()
                data=json.loads(current['data_json']);data['expression']=expression_snapshot(reply_mode,vid,version=target_expression);data['context']={**data.get('context',{}),'context_mode':context_mode,'cross_session':cross_session,'character_retrieval':character_policy,'disposition_mode':disposition_mode,'situation_observation_mode':situation_observation_mode}
                if selected_companion is not None:
                    from .companion import capture_restore, RESTORE_KEY
                    if 'companion' not in old_data:
                        data[RESTORE_KEY]=capture_restore(current)
                    data['companion']=selected_companion
                    data['delivery']=delivery
                if conversation_policy_version==2:data.update(ROLLING_PARAMS)
                conn.execute('INSERT INTO interaction_policies (id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES (?,?,?,?,?,?,?,?,?)',(vid,persona_id,LOCAL_USER_ID,channel,current['revision']+1,mode,encoded(data),timestamp(),reply_mode))
                conn.execute('INSERT INTO interaction_heads VALUES (?,?,?,?) ON CONFLICT(persona_id,channel) DO UPDATE SET version_id=excluded.version_id',(persona_id,LOCAL_USER_ID,channel,vid))
                result=self.policy(conn,persona_id,channel)
            self._remember(conn,persona_id,operation_id,fp,result)
        callback=getattr(self.store,'character_committed',None)
        if callback:callback(persona_id)
        self.schedule_committed();return result

    def _disable_companion_policy(self,persona_id,channel,mode,expected_version,operation_id,pending_action,expected_selected):
        from .companion import RESTORE_KEY, checked, restore_settings
        if mode not in MODES:raise RuntimeErrorBase('invalid_interaction_mode')
        value={'channel':channel,'mode':mode,'expected_version':expected_version,'pending_action':pending_action,
               'companion_mode':'disabled'}
        if expected_selected is not None:value['expected_selected']=expected_selected
        with self.store.connection(write=True) as conn:
            if self.store.current(conn)!=(persona_id if expected_selected is None else expected_selected):
                raise RuntimeErrorBase('persona_selection_conflict')
            self.engine.service._persona(conn,persona_id)
            # Replay precedes restore-point checks: the successful new head no
            # longer has a companion profile or its one-cycle restore point.
            fp,old=self._operation(conn,persona_id,operation_id,value)
            if old is not None:return old
            current=self.policy(conn,persona_id,channel)
            if current['id']!=expected_version:raise RuntimeErrorBase('version_conflict')
            if mode!=current['mode']:raise RuntimeErrorBase('companion_restore_conflict')
            old_data=json.loads(current['data_json'])
            if 'companion' not in old_data or RESTORE_KEY not in old_data:
                raise RuntimeErrorBase('companion_restore_missing')
            checked(old_data['companion'])
            saved=restore_settings(old_data[RESTORE_KEY]);data=saved['data']
            from .context_policy import delivery_metadata
            data['delivery']=delivery_metadata('user','disabled')
            disposition=data.get('context',{}).get('disposition_mode','baseline')
            if disposition in ('assist','live'):
                from .decision_capsule import require_admission
                require_admission(self.engine,persona_id,disposition)
            pending=conn.execute("SELECT * FROM input_batches WHERE persona_id=? AND channel=? AND state='collecting' ORDER BY first_seq",(persona_id,channel)).fetchall()
            if pending and pending_action not in ('seal','cancel'):
                raise RuntimeErrorBase('pending_batch_resolution_required')
            for row in pending:
                if pending_action=='seal':self._seal(conn,dict(row),'policy_changed')
                else:conn.execute("UPDATE input_batches SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",(timestamp(),row['id']))
            vid=identifier()
            # Rebind only the new policy identity, never upgrade the saved
            # expression/buffer version or material-selection contract.
            if isinstance(data.get('expression'),dict):data['expression']['policy_id']=vid
            conn.execute('INSERT INTO interaction_policies (id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES (?,?,?,?,?,?,?,?,?)',
                (vid,persona_id,LOCAL_USER_ID,channel,current['revision']+1,saved['mode'],encoded(data),timestamp(),saved['reply_mode']))
            conn.execute('INSERT INTO interaction_heads VALUES (?,?,?,?) ON CONFLICT(persona_id,channel) DO UPDATE SET version_id=excluded.version_id',
                (persona_id,LOCAL_USER_ID,channel,vid))
            result=self.policy(conn,persona_id,channel)
            self._remember(conn,persona_id,operation_id,fp,result)
        callback=getattr(self.store,'character_committed',None)
        if callback:callback(persona_id)
        self.schedule_committed();return result

    def _batch(self,conn,batch_id):
        row=conn.execute('SELECT * FROM input_batches WHERE id=?',(batch_id,)).fetchone()
        if not row:raise RuntimeErrorBase('batch_not_found')
        return dict(row)

    def upgrade_archived_policy(self,persona_id,channel,expected_version,operation_id,*,expression_policy_version=2):
        """Local maintenance only; never resurrect a trashed identity or permit chat.

        Not exposed by HTTP. Active identities must use update_policy, with its
        selection and pending-input checks. Explicit legacy choices stay intact.
        """
        if expression_policy_version not in (2,3):raise RuntimeErrorBase('expression_policy_invalid')
        with self.store.connection(write=True) as conn:
            archived=conn.execute('SELECT deleted_at FROM persona_lifecycle WHERE persona_id=? AND user_id=?',
                                  (persona_id,LOCAL_USER_ID)).fetchone()
            if not archived or not archived[0]:raise RuntimeErrorBase('archived_policy_maintenance_only')
            fp,old=self._operation(conn,persona_id,operation_id,{'action':'upgrade_archived_policy',
                'channel':channel,'expected_version':expected_version,'conversation_policy_version':2,'expression_policy_version':expression_policy_version})
            if old is not None:return old
            current=self.policy(conn,persona_id,channel)
            if current['id']!=expected_version:raise RuntimeErrorBase('version_conflict')
            if conn.execute("SELECT 1 FROM input_batches WHERE persona_id=? AND state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')",(persona_id,)).fetchone():
                raise RuntimeErrorBase('pending_batch_resolution_required')
            data=json.loads(current['data_json'])
            if (current['mode']!='natural_batch'
                    or (expression_policy_version==3 and current['reply_mode']!='natural_structured')
                    or (buffer_policy(current)==ROLLING_PARAMS and data.get('expression',{}).get('version',1)>=expression_policy_version)):
                result=current
            else:
                vid=identifier();data.update(ROLLING_PARAMS)
                data['expression']=expression_snapshot(current['reply_mode'],vid,version=max(expression_policy_version,data.get('expression',{}).get('version',1)))
                conn.execute('INSERT INTO interaction_policies (id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES (?,?,?,?,?,?,?,?,?)',
                    (vid,persona_id,LOCAL_USER_ID,channel,current['revision']+1,current['mode'],encoded(data),timestamp(),current['reply_mode']))
                conn.execute('UPDATE interaction_heads SET version_id=? WHERE persona_id=? AND user_id=? AND channel=?',(vid,persona_id,LOCAL_USER_ID,channel))
                result=self.policy(conn,persona_id,channel)
            self._remember(conn,persona_id,operation_id,fp,result)
            return result

    def _view(self,conn,batch_id):
        row=self._batch(conn,batch_id)
        for key in ('profile_snapshot','character_snapshot','model_snapshot','selector_model_snapshot','observation_model_snapshot','scope_key'):row.pop(key,None)
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
            from .qq_testing import QQTesting
            QQTesting(self.store.paths).check_runtime(peer)
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
            from .private_personas import guard_acceptance_write
            guard_acceptance_write(self.store,pid)
            sid=self.engine._session(conn,pid,channel,value.get('session_id'),origin={'bot':bot,'sender':peer})
            pol=self.policy(conn,pid,channel);policy={**buffer_policy(pol),'id':pol['id'],'mode':pol['mode'],'reply_mode':pol['reply_mode'],'revision':pol['revision'],'context':context_snapshot(pol,conn),'expression':json.loads(pol['data_json']).get('expression') or expression_snapshot(pol['reply_mode'],pol['id'],version=1)}
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
                conn.execute('INSERT INTO input_batches (id,persona_id,user_id,session_id,channel,bot,peer,scope_key,state,revision,first_seq,first_epoch,last_epoch,deadline_epoch,max_deadline_epoch,sealed_at,seal_reason,policy_json,profile_snapshot,character_snapshot,model_snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(bid,pid,LOCAL_USER_ID,sid,channel,bot,peer,scope,'collecting',1,seq,now,now,now+policy['quiet_ms']/1000,now+policy['max_wait_ms']/1000 if policy['max_wait_ms'] is not None else 0,None,None,encoded(policy),encoded(frozen),encoded(character),None,timestamp(),timestamp()))
                from .companion import freeze_auxiliary
                conn.execute('UPDATE input_batches SET observation_model_snapshot=? WHERE id=?',(encoded(freeze_auxiliary(cfg['character_selector'],policy['context'])),bid))
            conn.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES (?,?,?,?,'user',?,?)",(eid,sid,pid,LOCAL_USER_ID,text,timestamp()))
            conn.execute('INSERT INTO raw_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(rawid,pid,LOCAL_USER_ID,eid,channel,bot,peer,value.get('platform_message_id'),value.get('platform_time'),timestamp(),'normalized_payload',2 if 'reference' in value else 1,encoded(payload),fp))
            conn.execute('INSERT INTO ingress_receipts(id,source_key,request_id,persona_id,user_id,channel,bot,peer,session_id,fingerprint,raw_event_id,event_id,batch_id,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(receipt,source,rid,pid,LOCAL_USER_ID,channel,bot,peer,sid,fp,rawid,eid,bid,timestamp()))
            if 'reference' in value:
                from .archive import scope as archive_scope
                from .references import capture
                capture(conn,eid,archive_scope(pid,channel,bot,peer),value['reference'])
            order=conn.execute('SELECT count(*)+1 FROM input_batch_members WHERE batch_id=?',(bid,)).fetchone()[0]
            conn.execute('INSERT INTO input_batch_members VALUES (?,?,?,?,?,?)',(bid,pid,LOCAL_USER_ID,receipt,eid,order))
            from .delivery import DeliveryService
            DeliveryService(self.store).new_input(conn,eid,pid,sid,channel,{'bot':bot,'sender':peer},exclude_batch=bid)
            first_mono=self.clocks[bid][0] if batch else mono
            quiet=policy['quiet_ms']/1000
            maximum=policy['max_wait_ms']
            deadline_mono=mono+quiet if maximum is None else min(mono+quiet,first_mono+maximum/1000)
            max_epoch=batch['max_deadline_epoch'] if batch else (now+maximum/1000 if maximum is not None else 0)
            conn.execute('UPDATE input_batches SET last_epoch=?,deadline_epoch=?,revision=revision+1,updated_at=? WHERE id=?',(now,now+quiet if maximum is None else min(now+quiet,max_epoch),timestamp(),bid))
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
            from .companion import freeze_model,freeze_auxiliary
            accepted_context=json.loads(batch['policy_json']).get('context',{})
            conn.execute('UPDATE input_batches SET selector_model_snapshot=? WHERE id=?',(encoded(freeze_auxiliary(self.config.load()['character_selector'],accepted_context)),batch['id']))
            model=freeze_model(self.config.load()['model'],accepted_context)
            conn.execute("UPDATE input_batches SET state='sealed_waiting_capacity',sealed_at=?,seal_reason=?,model_snapshot=?,revision=revision+1,updated_at=? WHERE id=?",(timestamp(),reason,encoded(model),timestamp(),batch['id']))
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
            if policy.get('expression'):
                conn.execute('UPDATE turns SET expression_snapshot=? WHERE id=?',(encoded(policy['expression']),tid))
            if policy.get('context') is not None:
                conn.execute('UPDATE turns SET context_policy_snapshot=?,selector_model_snapshot=?,execution_revision=1 WHERE id=?',(encoded(policy['context']),b['selector_model_snapshot'],tid))
                conn.execute('UPDATE turns SET observation_model_snapshot=? WHERE id=?',(b.get('observation_model_snapshot'),tid))
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
