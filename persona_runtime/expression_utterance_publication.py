"""按人物发言单元授权呈现；正文只在首次租约中交付。"""
import json
from contextlib import ExitStack

from .archive import fingerprint
from .expression_assets import ExpressionAssetError,text
from .expression_contract import text_hash
from .expression_utterance import encoded
from .models import identifier,timestamp,LOCAL_USER_ID,RuntimeErrorBase


def project_notice(db,unit,body,complete):
    """仅由确认呈现或离线迁移投影实际前缀，原稿仍留在独立回执中。"""
    event=identifier()
    db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,'persona',?,?)",
        (event,unit['session_id'],unit['persona_id'],LOCAL_USER_ID,body,timestamp()))
    context=json.loads(unit['context_json'])
    if unit['source_kind'] in ('dialogue_reply','scene_reply') and unit['phase']=='main':
        root=dict(db.execute('SELECT * FROM turns WHERE id=?',(unit['turn_id'],)).fetchone())
        db.execute("UPDATE turns SET reply_event_id=?,status='completed',cancelled_at=NULL,updated_at=? WHERE id=?",(event,timestamp(),root['id']))
        from .recall_runtime import queue_feedback
        queue_feedback(db,root,context['logical_attempt_id'],'main',context['details'].get('recall_support'))
    if unit['phase']=='search_notice':
        task=db.execute('SELECT payload_json FROM organization_tasks WHERE id=?',(unit['task_id'],)).fetchone()
        payload=json.loads(task[0]);root=dict(db.execute('SELECT * FROM turns WHERE id=?',(unit['turn_id'],)).fetchone())
        value={'event_id':event,'task_id':unit['task_id'],'channel':root['channel'],
            'version':payload['knowledge_processing']['input']['notice_profile'],'response_hash':context.get('notice_response_hash',unit['source_hash']),
            'utterance_id':unit['id'],'final_body_hash':text_hash(body),'fully_presented':complete}
        db.execute('INSERT INTO knowledge_control VALUES(?,?)',('knowledge.search_notice.turn.'+unit['turn_id'],encoded({'revision':0,'data':value})))
        from .recall_runtime import queue_feedback
        queue_feedback(db,root,context.get('logical_attempt_id',unit['source_id']),'notice',context['details'].get('recall_support'))
        from .progress import record
        record(db,root['id'],'search_notice_published')
    return event


class SpeechPublication:
    def __init__(self,outlet):self.outlet,self.store=outlet,outlet.store

    def _owner(self,db,persona_id,session_id,utterance_id):
        unit=self.outlet.unit(db,utterance_id)
        if (unit['persona_id'],unit['session_id'])!=(persona_id,session_id):raise ExpressionAssetError('speech_presentation_scope_mismatch')
        return unit

    def _legacy(self):
        from .expression_publication import ExpressionPublication
        return ExpressionPublication(self.outlet.runtime)

    def list(self,persona_id,session_id,turn_id=None):
        with self.store.connection() as db:
            values=(persona_id,session_id) if turn_id is None else (persona_id,session_id,turn_id)
            query='SELECT id,turn_id,phase,source_kind,created_at,cancelled_at,closed_at FROM expression_speech_units WHERE persona_id=? AND session_id=?'
            if turn_id is not None:query+=' AND turn_id=?'
            return [dict(r)|{'utterance_id':r['id']} for r in db.execute(query+' ORDER BY rowid',values)]

    def status(self,persona_id,session_id,utterance_id):
        with self.store.connection() as db:
            unit=self._owner(db,persona_id,session_id,utterance_id)
            run=db.execute('SELECT id,state,attempt_id,usage_json,error_code FROM expression_speech_runs WHERE utterance_id=?',(utterance_id,)).fetchone()
            source_usage=db.execute('SELECT usage_json FROM organization_attempts WHERE id=?',(unit['source_id'],)).fetchone() if unit['phase']!='main' else None
        if unit['source_kind']=='main_receipt':return {'utterance_id':utterance_id,'phase':'main','state':self.outlet.runtime.inspect(unit['turn_id'])}
        return {'utterance_id':utterance_id,'phase':unit['phase'],'source':{'kind':unit['source_kind'],'id':unit['source_id'],'body_hash':unit['body_hash']},
                'source_usage':json.loads(source_usage[0]) if source_usage and source_usage[0] is not None else None,
                'run':dict(run) if run else None,'publication':self.plan(persona_id,session_id,utterance_id)}

    def publish(self,db,unit,kind,body,segments,reason,*,replace_unoffered=False):
        old=db.execute('SELECT * FROM expression_speech_publications WHERE utterance_id=?',(unit['id'],)).fetchone()
        if old:
            if not replace_unoffered:return dict(old)
            if old['state'] in ('closed','complete') or db.execute('SELECT 1 FROM expression_speech_items WHERE publication_id=? AND lease_id IS NOT NULL',(old['id'],)).fetchone():
                raise ExpressionAssetError('speech_publication_already_started')
        self.outlet.validate(db,unit)
        # 从原文连续切片，失败时整块呈现，禁止重新生成正文或拼接其他稿件。
        cursor=0;pieces=[]
        from .expression_sources import protected
        spans=protected(body)
        try:
            for s in segments:
                if s['start']!=cursor or s['end']<=cursor or s['end']>len(body):raise ValueError()
                if any(p['start']<s['end']<p['end'] for p in spans):raise ValueError()
                pieces.append(s);cursor=s['end']
            if cursor!=len(body):raise ValueError()
        except (ValueError,KeyError,TypeError):pieces=[{'start':0,'end':len(body),'kind':'chat','required':True,'delay_ms':0}]
        pid=old['id'] if old else identifier();revision=old['revision']+1 if old else 1
        if old:
            db.execute("UPDATE expression_speech_items SET state='cancelled' WHERE publication_id=? AND state='pending'",(pid,))
            db.execute("UPDATE expression_speech_publications SET revision=?,producer_kind=?,body=?,body_hash=?,state='pending',reason=? WHERE id=?",
                (revision,kind,body,text_hash(body),reason,pid))
        else:db.execute('INSERT INTO expression_speech_publications VALUES(?,?,?,?,?,?,?,?,?,?)',
            (pid,unit['id'],revision,kind,body,text_hash(body),'pending',reason,None,timestamp()))
        plan={'utterance_id':unit['id'],'body_hash':text_hash(body),'bubbles':pieces}
        db.execute('INSERT INTO expression_speech_versions VALUES(?,?,?)',(pid,revision,encoded(plan)))
        for i,s in enumerate(pieces):
            part=body[s['start']:s['end']]
            db.execute('''INSERT INTO expression_speech_items(id,publication_id,revision,ordinal,text,body_hash,kind,required,delay_ms,state)
               VALUES(?,?,?,?,?,?,?,?,?,'pending')''',(pid+':'+str(revision)+':'+str(i),pid,revision,i,part,text_hash(part),s.get('kind','chat'),int(s.get('required',True)),
               0 if unit['phase']=='search_notice' else s.get('delay_ms',0)))
        return dict(db.execute('SELECT * FROM expression_speech_publications WHERE id=?',(pid,)).fetchone())

    def fallback(self,utterance_id,reason):
        try:
            with self.store.connection(write=True) as db:
                unit=self.outlet.unit(db,utterance_id)
                old=db.execute('SELECT state FROM expression_speech_publications WHERE utterance_id=?',(utterance_id,)).fetchone()
                if old:return {'state':old[0],'new_call':False}
                pub=self.publish(db,unit,'draft',unit['body'],[{'start':0,'end':len(unit['body']),'required':True}],reason)
                return {'state':pub['state'],'fallback':True,'new_call':False}
        except RuntimeErrorBase as error:
            with self.store.connection(write=True) as db:
                db.execute('UPDATE expression_speech_units SET closed_at=? WHERE id=?',(timestamp(),utterance_id))
            return {'state':'blocked','reason':str(error),'new_call':False}

    def plan(self,persona_id,session_id,utterance_id):
        with self.store.connection() as db:
            unit=self._owner(db,persona_id,session_id,utterance_id)
            if unit['source_kind']!='main_receipt':
                pub=db.execute('SELECT id,revision,state,producer_kind,reason FROM expression_speech_publications WHERE utterance_id=?',(utterance_id,)).fetchone()
                if not pub:return None
                items=[dict(r) for r in db.execute('SELECT id,ordinal,kind,required,delay_ms,state FROM expression_speech_items WHERE publication_id=? AND revision=? ORDER BY ordinal',(pub['id'],pub['revision']))]
                return dict(pub)|{'items':items}
        return self._legacy().plan(persona_id,session_id,unit['turn_id'])

    def offer(self,persona_id,session_id,utterance_id,publication_id,revision,ordinal,presenter_id):
        text(presenter_id,'presenter_id',200)
        with self.store.connection() as db:unit=self._owner(db,persona_id,session_id,utterance_id)
        if unit['source_kind']=='main_receipt':return self._legacy().offer(persona_id,session_id,unit['turn_id'],publication_id,revision,ordinal,presenter_id)
        with ExitStack() as stack:
            if unit['source_kind']=='dialogue_reply':
                try:stack.enter_context(self.outlet.direct.authority_window(json.loads(unit['context_json'])['generation_unit_id']))
                except (OSError,RuntimeErrorBase) as error:
                    self.cancel(utterance_id,'source_or_permission_changed')
                    return {'state':'stopped','reason':str(error),'new_offer':False}
            db=stack.enter_context(self.store.connection(write=True))
            unit=self._owner(db,persona_id,session_id,utterance_id)
            if self.store.current(db)!=persona_id:raise ExpressionAssetError('speech_persona_not_selected')
            pub=db.execute('SELECT * FROM expression_speech_publications WHERE id=? AND utterance_id=?',(publication_id,utterance_id)).fetchone()
            if not pub or pub['revision']!=revision or pub['state'] in ('closed','complete'):raise ExpressionAssetError('speech_plan_changed')
            try:
                turn=self.outlet.validate(db,unit)
            except RuntimeErrorBase as error:
                self._stop(db,unit,pub,'source_or_permission_changed')
                return {'state':'stopped','reason':str(error),'new_offer':False}
            if pub['producer_kind']=='artifact':
                try:
                    self.outlet._policy_check(db,unit)
                    from .private_personas import guard_cloud_dispatch
                    guard_cloud_dispatch(self.store,persona_id,'expression',json.loads(turn['model_snapshot']))
                except RuntimeErrorBase:
                    if not db.execute('SELECT 1 FROM expression_speech_items WHERE publication_id=? AND lease_id IS NOT NULL',(publication_id,)).fetchone():
                        self.publish(db,unit,'draft',unit['body'],[{'start':0,'end':len(unit['body'])}],
                            'expression_permission_changed_before_offer',replace_unoffered=True)
                        return {'state':'plan_changed','new_offer':False}
                    self._stop(db,unit,pub,'expression_permission_changed')
                    return {'state':'stopped','new_offer':False}
            item=db.execute('SELECT * FROM expression_speech_items WHERE publication_id=? AND revision=? AND ordinal=?',(publication_id,revision,ordinal)).fetchone()
            if not item:raise ExpressionAssetError('speech_fragment_missing')
            if item['state']!='pending':return {'id':item['id'],'state':item['state'],'new_offer':False}
            if db.execute("SELECT 1 FROM expression_speech_items WHERE publication_id=? AND revision=? AND ordinal<? AND state!='presented'",(publication_id,revision,ordinal)).fetchone():
                raise ExpressionAssetError('speech_presentation_order_gap')
            lease=identifier()
            db.execute("UPDATE expression_speech_items SET state='offered',lease_id=?,presenter_id=?,offered_at=? WHERE id=?",(lease,presenter_id,timestamp(),item['id']))
            return {'id':item['id'],'lease_id':lease,'body':item['text'],'body_hash':item['body_hash'],'state':'offered',
                    'kind':item['kind'],'required':bool(item['required']),'delay_ms':item['delay_ms']}

    def acknowledge(self,persona_id,session_id,utterance_id,fragment_id,lease_id,presenter_id,receipt_id,body_hash,state='presented'):
        text(receipt_id,'receipt_id',200)
        if state not in ('presented','not_presented','unknown'):raise ExpressionAssetError('speech_receipt_state_invalid')
        with self.store.connection() as db:unit=self._owner(db,persona_id,session_id,utterance_id)
        if unit['source_kind']=='main_receipt':return self._legacy().acknowledge(persona_id,session_id,unit['turn_id'],fragment_id,lease_id,presenter_id,receipt_id,body_hash,state)
        with self.store.connection(write=True) as db:
            previous=db.execute('SELECT * FROM expression_speech_receipts WHERE id=?',(receipt_id,)).fetchone()
            if previous:
                belongs=db.execute('''SELECT 1 FROM expression_speech_items i JOIN expression_speech_publications p
                    ON p.id=i.publication_id WHERE i.id=? AND p.utterance_id=?''',(fragment_id,utterance_id)).fetchone()
                if not belongs:raise ExpressionAssetError('speech_receipt_identity_mismatch')
                if tuple(previous)[1:6]!=(fragment_id,lease_id,presenter_id,body_hash,state):raise ExpressionAssetError('speech_receipt_conflict')
                return {'state':state,'already_applied':True}
            item=db.execute('''SELECT i.* FROM expression_speech_items i JOIN expression_speech_publications p ON p.id=i.publication_id
                WHERE i.id=? AND p.utterance_id=?''',(fragment_id,utterance_id)).fetchone()
            if not item or (item['lease_id'],item['presenter_id'],item['body_hash'])!=(lease_id,presenter_id,body_hash):raise ExpressionAssetError('speech_receipt_identity_mismatch')
            pub=db.execute('SELECT * FROM expression_speech_publications WHERE id=?',(item['publication_id'],)).fetchone()
            if pub['state'] in ('closed','complete'):raise ExpressionAssetError('speech_publication_closed')
            if item['state'] not in ('offered','unknown'):raise ExpressionAssetError('speech_receipt_conflict')
            now=timestamp()
            db.execute('INSERT INTO expression_speech_receipts VALUES(?,?,?,?,?,?,?)',(receipt_id,fragment_id,lease_id,presenter_id,body_hash,state,now))
            db.execute('UPDATE expression_speech_items SET state=?,receipt_id=? WHERE id=?',(state,receipt_id,fragment_id))
            if state=='presented':
                db.execute('INSERT INTO expression_speech_visible VALUES(?,?,?,?,?,?,?)',(fragment_id,utterance_id,pub['id'],item['ordinal'],item['text'],receipt_id,now))
                if unit['source_kind']=='scene_reply':
                    from .progress import record
                    record(db,unit['turn_id'],'first_output' if item['ordinal']==0 else 'scene_bubble_presented',
                           json.loads(unit['context_json'])['logical_attempt_id'])
            remaining=db.execute("SELECT state FROM expression_speech_items WHERE publication_id=? AND revision=? AND state IN ('pending','offered','unknown')",(pub['id'],pub['revision'])).fetchall()
            if state=='not_presented':
                self._stop(db,unit,pub,'fragment_not_presented')
            elif remaining:db.execute('UPDATE expression_speech_publications SET state=? WHERE id=?',('unknown' if any(r[0]=='unknown' for r in remaining) else 'partial',pub['id']))
            else:self._close(db,unit,pub,'complete' if not unit['cancelled_at'] else 'cancelled_by_user')
            return {'state':state,'already_applied':False}

    def _close(self,db,unit,pub,reason):
        rows=db.execute('SELECT ordinal,text FROM expression_speech_visible WHERE publication_id=? ORDER BY ordinal',(pub['id'],)).fetchall()
        # 历史只接纳连续的已确认前缀。未知片段之前的内容不能越过未确认边界。
        visible=[]
        for index,row in enumerate(rows):
            if row['ordinal']!=index:break
            visible.append(row['text'])
        body=''.join(visible)
        all_presented=not db.execute("SELECT 1 FROM expression_speech_items WHERE publication_id=? AND revision=? AND state!='presented'",(pub['id'],pub['revision'])).fetchone()
        complete=reason=='complete' and all_presented
        event=pub['canonical_event_id']
        if body and not event:
            event=project_notice(db,unit,body,complete)
        db.execute('UPDATE expression_speech_publications SET state=?,reason=?,canonical_event_id=? WHERE id=?',
            ('complete' if complete else 'closed',reason,event,pub['id']))
        if unit['source_kind'] in ('dialogue_reply','scene_reply') and unit['phase']=='main':
            db.execute('UPDATE turns SET status=?,error_code=?,updated_at=? WHERE id=?',
                ('completed' if event else 'interrupted',None if complete else reason,timestamp(),unit['turn_id']))
        db.execute('UPDATE expression_speech_units SET closed_at=? WHERE id=?',(timestamp(),unit['id']))
        if unit['source_kind']=='scene_reply':
            from .progress import record
            record(db,unit['turn_id'],'persisted' if event else 'interrupted',
                   json.loads(unit['context_json'])['logical_attempt_id'])

    def _stop(self,db,unit,pub,reason):
        db.execute('UPDATE expression_speech_units SET cancelled_at=COALESCE(cancelled_at,?) WHERE id=?',(timestamp(),unit['id']))
        db.execute("UPDATE expression_speech_items SET state='cancelled' WHERE publication_id=? AND state='pending'",(pub['id'],))
        db.execute("UPDATE expression_speech_items SET state='unknown' WHERE publication_id=? AND state='offered'",(pub['id'],))
        if db.execute("SELECT 1 FROM expression_speech_items WHERE publication_id=? AND state='unknown'",(pub['id'],)).fetchone():
            db.execute("UPDATE expression_speech_publications SET state='unknown',reason=? WHERE id=?",(reason,pub['id']))
        else:self._close(db,unit,pub,reason)

    def cancel(self,utterance_id,reason='cancelled_by_user'):
        with self.store.connection() as db:unit=self.outlet.unit(db,utterance_id)
        if unit['source_kind']=='main_receipt':return self._legacy().cancel(unit['turn_id'],reason)
        with self.store.connection(write=True) as db:
            unit=self.outlet.unit(db,utterance_id)
            if unit['closed_at']:return {'state':'closed','new_call':False}
            db.execute('UPDATE expression_speech_units SET cancelled_at=? WHERE id=?',(timestamp(),utterance_id))
            db.execute("UPDATE expression_speech_runs SET state='cancelled',error_code=? WHERE utterance_id=?",(reason,utterance_id))
            pub=db.execute('SELECT * FROM expression_speech_publications WHERE utterance_id=?',(utterance_id,)).fetchone()
            if pub:self._stop(db,unit,pub,reason)
            else:db.execute('UPDATE expression_speech_units SET closed_at=? WHERE id=?',(timestamp(),utterance_id))
        return {'state':'cancelled','new_call':False}

    def visible(self,persona_id,session_id,utterance_id):
        with self.store.connection() as db:
            unit=self._owner(db,persona_id,session_id,utterance_id)
            if unit['source_kind']!='main_receipt':return [dict(r) for r in db.execute('SELECT fragment_id AS id,ordinal,text,receipt_id,presented_at FROM expression_speech_visible WHERE utterance_id=? ORDER BY ordinal',(utterance_id,))]
        return self._legacy().visible(persona_id,session_id,unit['turn_id'])

    def pending_receipts(self,persona_id,session_id,utterance_id,presenter_id):
        with self.store.connection() as db:
            unit=self._owner(db,persona_id,session_id,utterance_id)
            if unit['source_kind']!='main_receipt':return [dict(r) for r in db.execute('''SELECT i.id,i.publication_id,i.revision,i.ordinal,i.lease_id,i.presenter_id,i.body_hash,i.state,i.offered_at
                FROM expression_speech_items i JOIN expression_speech_publications p ON p.id=i.publication_id
                WHERE p.utterance_id=? AND p.state NOT IN ('complete','closed') AND i.presenter_id=? AND i.state IN ('offered','unknown') ORDER BY i.ordinal''',(utterance_id,presenter_id))]
        return self._legacy().pending_receipts(persona_id,session_id,unit['turn_id'],presenter_id)
