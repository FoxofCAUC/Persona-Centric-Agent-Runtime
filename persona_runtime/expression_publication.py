"""单一回复发布权：逐片段授权、回执和实际可见正文投影。"""
from dataclasses import asdict
import json

from .archive import fingerprint, turn_scope
from .expression_assets import ExpressionAssetError, text
from .expression_contract import (InteractionRef, CallReceipt, MainDraftReceipt, ExpressionArtifact,
    ProtectedSpan, BubbleRange, PresentationPlan, validate_plan, text_hash)
from .expression_sources import protected
from .models import identifier, timestamp, LOCAL_USER_ID, RuntimeErrorBase


class ExpressionPublication:
    def __init__(self, runtime):self.runtime,self.store=runtime,runtime.store

    def _direct_main(self, turn_id):
        with self.store.connection() as db:
            from .dialogue_generation import enabled
            if not enabled(db,turn_id):return None
            row=db.execute("SELECT id FROM expression_speech_units WHERE turn_id=? AND phase='main' AND source_kind='dialogue_reply'",(turn_id,)).fetchone()
            return row[0] if row else None

    def _publish(self, db, turn, draft, kind, producer_id, body, segments, reason, *, replace_unoffered=False):
        db.execute("UPDATE attempts SET status='completed',error_code=NULL,usage_json=? WHERE id=?",(draft['usage_json'],draft['logical_attempt_id']))
        current=db.execute('SELECT * FROM expression_publications WHERE turn_id=?',(turn['id'],)).fetchone()
        if current:
            if current['producer_kind']==kind and current['producer_id']==producer_id:return dict(current)
            offered=db.execute('SELECT 1 FROM expression_presentation_items WHERE publication_id=? AND lease_id IS NOT NULL LIMIT 1',(current['id'],)).fetchone()
            if not replace_unoffered or offered or current['state'] in ('closed','complete'):
                raise ExpressionAssetError('expression_publication_already_sealed')
            pub_id,revision=current['id'],current['revision']+1
            db.execute("UPDATE expression_presentation_items SET state='cancelled' WHERE publication_id=? AND state='pending'",(pub_id,))
        else:pub_id,revision=identifier(),1
        policy=json.loads(db.execute('SELECT policy_json FROM expression_runtime_turns WHERE turn_id=?',(turn['id'],)).fetchone()[0])
        ref=InteractionRef(turn['id'],turn['persona_id'],turn['session_id'],fingerprint(turn_scope(db,turn)),str(policy['revision']))
        spans=tuple(ProtectedSpan(**s) for s in protected(body))
        main_model=json.loads(turn['model_snapshot'])
        if kind=='draft':
            producer=MainDraftReceipt(ref,draft['id'],body,text_hash(body),draft['id'],CallReceipt(draft['physical_attempt_id'],'primary','deepseek_official',main_model['name'],'completed'),spans)
        else:
            run=db.execute('SELECT * FROM expression_runs WHERE id=?',(producer_id,)).fetchone()
            producer=ExpressionArtifact(ref,producer_id,draft['id'],draft['body_hash'],producer_id+':context',fingerprint(run['input_json']),
                        body,text_hash(body),CallReceipt(run['attempt_id'],'expression','deepseek_official',main_model['name'],'completed'),producer_id,spans)
        def make(items):
            return PresentationPlan(ref,pub_id+':'+str(revision),producer_id,text_hash(body),pub_id,'expression-e1-ready-time-v1',
                tuple(BubbleRange(pub_id+':'+str(revision)+':'+str(i),item['start'],item['end'],item.get('kind','chat'),bool(item.get('required',True)),item.get('delay_ms',0)) for i,item in enumerate(items)))
        plan=make(segments)
        try:validate_plan(producer,plan)
        except ValueError:
            plan=make([{'start':0,'end':len(body),'kind':'technical' if spans else 'chat','required':True,'delay_ms':0}]);validate_plan(producer,plan)
        if current:
            db.execute('UPDATE expression_publications SET revision=?,producer_kind=?,producer_id=?,state=\'pending\',reason=? WHERE id=?',(revision,kind,producer_id,reason,pub_id))
        else:
            db.execute('INSERT INTO expression_publications VALUES(?,?,?,?,?,?,?,?,?)',(pub_id,turn['id'],revision,kind,producer_id,'pending',reason,None,timestamp()))
        db.execute('INSERT INTO expression_publication_versions VALUES(?,?,?,?,?)',(pub_id,revision,kind,producer_id,json.dumps(asdict(plan),ensure_ascii=False)))
        for ordinal,bubble in enumerate(plan.bubbles):
            part=body[bubble.start:bubble.end]
            db.execute('''INSERT INTO expression_presentation_items
              (id,publication_id,revision,ordinal,text,content_hash,kind,required,delay_ms,state)
              VALUES(?,?,?,?,?,?,?,?,?,'pending')''',(bubble.utterance_id,pub_id,revision,ordinal,part,text_hash(part),bubble.kind,int(bubble.required),bubble.delay_ms))
        db.execute('UPDATE turns SET status=\'completed\',error_code=NULL,usage_json=?,updated_at=? WHERE id=?',(draft['usage_json'],timestamp(),turn['id']))
        from .progress import record
        record(db,turn['id'],'desktop_publication_changed')
        return dict(db.execute('SELECT * FROM expression_publications WHERE id=?',(pub_id,)).fetchone())

    def fallback(self, turn_id, reason):
        from .dialogue_generation import enabled
        with self.store.connection() as db:
            if enabled(db,turn_id):raise ExpressionAssetError('dialogue_direct_fallback_unavailable')
        try:
            with self.store.connection(write=True) as db:
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone())
                draft=self.runtime._draft(db,turn_id);self.runtime._validate_main(db,turn,draft)
                result=self._publish(db,turn,draft,'draft',draft['id'],draft['body'],
                         [{'start':0,'end':len(draft['body']),'kind':'chat','required':True,'delay_ms':0}],reason,replace_unoffered=True)
                return {'state':result['state'],'publication_id':result['id'],'fallback':True,'reason':reason}
        except ExpressionAssetError as error:
            with self.store.connection(write=True) as db:
                if not db.execute('SELECT 1 FROM expression_publications WHERE turn_id=?',(turn_id,)).fetchone():
                    db.execute('UPDATE expression_runtime_turns SET closed_at=? WHERE turn_id=?',(timestamp(),turn_id))
            return {'state':'blocked','reason':str(error),'new_call':False}

    def plan(self, persona_id, session_id, turn_id):
        uid=self._direct_main(turn_id)
        if uid:return self.runtime.outlet.direct.publication.plan(persona_id,session_id,uid)
        with self.store.connection() as db:
            turn=self._owner(db,persona_id,session_id,turn_id)
            row=db.execute('SELECT * FROM expression_publications WHERE turn_id=?',(turn_id,)).fetchone()
            if not row:return None
            items=[{k:r[k] for k in ('id','ordinal','kind','required','delay_ms','state')} for r in db.execute('SELECT * FROM expression_presentation_items WHERE publication_id=? AND revision=? ORDER BY ordinal',(row['id'],row['revision']))]
            return {k:row[k] for k in ('id','revision','state','producer_kind','reason')}|{'items':items}

    @staticmethod
    def _owner(db,pid,session,tid):
        row=db.execute('SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=? AND channel=\'desktop\'',(tid,pid,session)).fetchone()
        if not row:raise ExpressionAssetError('expression_presentation_scope_mismatch')
        return dict(row)

    def offer(self, persona_id, session_id, turn_id, publication_id, revision, ordinal, presenter_id):
        uid=self._direct_main(turn_id)
        if uid:return self.runtime.outlet.direct.publication.offer(persona_id,session_id,uid,publication_id,revision,ordinal,presenter_id)
        text(presenter_id,'presenter_id',200)
        with self.store.connection(write=True) as db:
            turn=self._owner(db,persona_id,session_id,turn_id)
            if self.store.current(db)!=persona_id:raise ExpressionAssetError('expression_presentation_persona_not_selected')
            pub=db.execute('SELECT * FROM expression_publications WHERE id=? AND turn_id=?',(publication_id,turn_id)).fetchone()
            if not pub or pub['revision']!=revision or pub['state'] in ('closed','complete'):raise ExpressionAssetError('expression_presentation_plan_changed')
            draft=self.runtime._draft(db,turn_id)
            try:self.runtime._validate_main(db,turn,draft)
            except ValueError as error:
                self._cancel_pending(db,pub,'source_or_permission_changed')
                return {'state':'stopped','reason':str(error),'new_call':False}
            if pub['producer_kind']=='artifact':
                run=db.execute('SELECT * FROM expression_runs WHERE id=?',(pub['producer_id'],)).fetchone()
                try:
                    current_policy=db.execute('SELECT data_json FROM expression_runtime_policies WHERE persona_id=?',(persona_id,)).fetchone()
                    if not current_policy or not json.loads(current_policy[0])['enabled']:raise ExpressionAssetError('expression_runtime_disabled')
                    self.runtime._candidate_check(db,run)
                    from .private_personas import guard_cloud_dispatch
                    guard_cloud_dispatch(self.store,persona_id,'expression',json.loads(turn['model_snapshot']))
                except ValueError:
                    offered=db.execute('SELECT 1 FROM expression_presentation_items WHERE publication_id=? AND lease_id IS NOT NULL',(publication_id,)).fetchone()
                    if not offered:
                        self._publish(db,turn,draft,'draft',draft['id'],draft['body'],[{'start':0,'end':len(draft['body']),'required':True}],
                                      'expression_source_changed_before_offer',replace_unoffered=True)
                        return {'state':'plan_changed','new_call':False}
                    self._cancel_pending(db,pub,'source_changed')
                    return {'state':'stopped','reason':'source_changed'}
            item=db.execute('SELECT * FROM expression_presentation_items WHERE publication_id=? AND revision=? AND ordinal=?',(publication_id,revision,ordinal)).fetchone()
            if not item:raise ExpressionAssetError('expression_utterance_missing')
            if item['state']!='pending':return {'id':item['id'],'state':item['state'],'new_offer':False}
            if db.execute("SELECT 1 FROM expression_presentation_items WHERE publication_id=? AND revision=? AND ordinal<? AND state NOT IN ('presented','cancelled','not_presented')",(publication_id,revision,ordinal)).fetchone():
                raise ExpressionAssetError('expression_presentation_order_gap')
            lease=identifier()
            db.execute("UPDATE expression_presentation_items SET state='offered',lease_id=?,presenter_id=?,offered_at=? WHERE id=?",(lease,presenter_id,timestamp(),item['id']))
            return {'id':item['id'],'lease_id':lease,'state':'offered','body':item['text'],'body_hash':item['content_hash'],
                    'kind':item['kind'],'required':bool(item['required']),'delay_ms':item['delay_ms']}

    def acknowledge(self, persona_id, session_id, turn_id, utterance_id, lease_id, presenter_id, receipt_id, body_hash, state='presented',
                    *,webview=False,publication_id=None,revision=None,ordinal=None):
        uid=self._direct_main(turn_id)
        if uid:return self.runtime.outlet.direct.publication.acknowledge(persona_id,session_id,uid,utterance_id,lease_id,presenter_id,receipt_id,body_hash,state)
        if state not in ('presented','not_presented','unknown'):raise ExpressionAssetError('expression_receipt_state_invalid')
        text(receipt_id,'receipt_id',200)
        if webview and presenter_id!="webview-v2."+receipt_id:
            raise ExpressionAssetError('expression_receipt_identity_mismatch')
        with self.store.connection(write=True) as db:
            self._owner(db,persona_id,session_id,turn_id)
            previous=db.execute('SELECT * FROM expression_presentation_receipts WHERE id=?',(receipt_id,)).fetchone()
            if previous:
                belongs=db.execute("""SELECT i.publication_id,i.revision,i.ordinal FROM expression_presentation_items i
                    JOIN expression_publications p ON p.id=i.publication_id WHERE i.id=? AND p.turn_id=?""",
                    (utterance_id,turn_id)).fetchone()
                if not belongs or webview and tuple(belongs)!=(publication_id,revision,ordinal):
                    raise ExpressionAssetError("expression_receipt_identity_mismatch")
                if (previous['utterance_id'],previous['lease_id'],previous['presenter_id'],previous['body_hash'],previous['state'])==(utterance_id,lease_id,presenter_id,body_hash,state):
                    return {'state':state,'already_applied':True}
                raise ExpressionAssetError('expression_receipt_conflict')
            item=db.execute('SELECT i.* FROM expression_presentation_items i JOIN expression_publications p ON p.id=i.publication_id WHERE i.id=? AND p.turn_id=?',(utterance_id,turn_id)).fetchone()
            if not item or (item['lease_id'],item['presenter_id'],item['content_hash'])!=(lease_id,presenter_id,body_hash):
                raise ExpressionAssetError('expression_receipt_identity_mismatch')
            publication=db.execute('SELECT state,revision FROM expression_publications WHERE id=?',(item['publication_id'],)).fetchone()
            if webview and ((publication_id,revision,ordinal)!=(item['publication_id'],item['revision'],item['ordinal']) or revision!=publication["revision"]):
                raise ExpressionAssetError('expression_receipt_identity_mismatch')
            if publication[0] in ('closed','complete'):raise ExpressionAssetError('expression_publication_closed')
            if item['receipt_id']:
                if item['receipt_id']==receipt_id and item['state']==state:return {'state':state,'already_applied':True}
                if item['state']!='unknown':raise ExpressionAssetError('expression_receipt_conflict')
            if webview and state=='presented':
                turn = self._owner(db,persona_id,session_id,turn_id)
                try:
                    self.runtime._validate_main(db,turn,self.runtime._draft(db,turn_id))
                except RuntimeErrorBase:
                    pub=db.execute("SELECT * FROM expression_publications WHERE id=?",(publication_id,)).fetchone()
                    self._cancel_pending(db,pub,"source_or_permission_changed")
                    if db.execute("SELECT state FROM expression_publications WHERE id=?",(publication_id,)).fetchone()[0]=="unknown":
                        self._close(db,pub,"desktop_presentation_unknown")
                    return {"state":"closed","already_applied":False}
            now=timestamp()
            db.execute('INSERT INTO expression_presentation_receipts VALUES(?,?,?,?,?,?,?)',(receipt_id,utterance_id,lease_id,presenter_id,body_hash,state,now))
            db.execute('UPDATE expression_presentation_items SET state=?,receipt_id=?,acknowledged_at=? WHERE id=?',(state,receipt_id,now,utterance_id))
            if state=='presented':
                db.execute('INSERT INTO expression_utterances VALUES(?,?,?,?,?,?,?,?)',(utterance_id,turn_id,item['publication_id'],item['revision'],item['ordinal'],item['text'],receipt_id,now))
            pub=db.execute('SELECT * FROM expression_publications WHERE id=?',(item['publication_id'],)).fetchone()
            if webview and state=='not_presented' and pub['reason'] not in ('cancelled_by_user','source_changed','source_or_permission_changed'):
                db.execute("""UPDATE expression_presentation_items SET state='pending',receipt_id=NULL,lease_id=NULL,
                    presenter_id=NULL,offered_at=NULL,acknowledged_at=NULL WHERE id=?""",(utterance_id,))
                db.execute("UPDATE expression_publications SET state=? WHERE id=?",('partial' if item['ordinal'] else 'pending',pub['id']))
            elif webview and state=='unknown':
                self._cancel_pending(db,pub,'desktop_presentation_unknown')
                self._close(db,pub,'desktop_presentation_unknown')
            elif state=='unknown':db.execute('UPDATE expression_publications SET state=\'unknown\' WHERE id=?',(pub['id'],))
            elif db.execute("SELECT 1 FROM expression_presentation_items WHERE publication_id=? AND revision=? AND state IN ('pending','offered','unknown')",(pub['id'],pub['revision'])).fetchone():
                db.execute('UPDATE expression_publications SET state=\'partial\' WHERE id=?',(pub['id'],))
            else:self._close(db,pub,pub['reason'] if pub['reason'] in ('source_changed','source_or_permission_changed','cancelled_by_user') else 'complete')
            return {'state':state,'already_applied':False}

    def _close(self, db, pub, reason):
        rows=db.execute('SELECT text FROM expression_utterances WHERE publication_id=? ORDER BY ordinal',(pub['id'],)).fetchall()
        body=''.join(r[0] for r in rows)
        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(pub['turn_id'],)).fetchone())
        event=pub['canonical_event_id']
        if body and not event:
            event=identifier()
            db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,'persona',?,?)",(event,turn['session_id'],turn['persona_id'],LOCAL_USER_ID,body,timestamp()))
            db.execute("UPDATE turns SET reply_event_id=?,status='completed',cancelled_at=NULL,updated_at=? WHERE id=?",(event,timestamp(),turn['id']))
            draft=self.runtime._draft(db,turn['id']);context=json.loads(draft['context_json'])
            from .recall_runtime import queue_feedback
            queue_feedback(db,turn,draft['logical_attempt_id'],'main',context['details'].get('recall_support'))
        if not body:db.execute("UPDATE turns SET status='interrupted',error_code=? WHERE id=?",(reason,turn['id']))
        db.execute('UPDATE expression_publications SET state=?,reason=?,canonical_event_id=? WHERE id=?',('complete' if reason=='complete' else 'closed',reason,event,pub['id']))

    def _cancel_pending(self, db, pub, reason):
        db.execute("UPDATE expression_presentation_items SET state='cancelled' WHERE publication_id=? AND revision=? AND state='pending'",(pub['id'],pub['revision']))
        db.execute("UPDATE expression_presentation_items SET state='unknown' WHERE publication_id=? AND revision=? AND state='offered'",(pub['id'],pub['revision']))
        unknown=db.execute("SELECT 1 FROM expression_presentation_items WHERE publication_id=? AND revision=? AND state='unknown'",(pub['id'],pub['revision'])).fetchone()
        if unknown:db.execute("UPDATE expression_publications SET state='unknown',reason=? WHERE id=?",(reason,pub['id']))
        else:self._close(db,pub,reason)

    def cancel(self, turn_id, reason='cancelled_by_user'):
        uid=self._direct_main(turn_id)
        if uid:return self.runtime.outlet.direct.publication.cancel(uid,reason)
        from .dialogue_generation import enabled
        with self.store.connection() as db:direct=enabled(db,turn_id)
        if direct:
            self.runtime.outlet.direct.generation.cancel_root(turn_id,reason)
            self.runtime.outlet.cancel_root(turn_id,reason)
            with self.store.connection(write=True) as db:
                db.execute("UPDATE turns SET cancelled_at=?,error_code=?,status='interrupted',updated_at=? WHERE id=? AND status!='completed'",(timestamp(),reason,timestamp(),turn_id))
            return {'state':'cancelled','new_call':False}
        self.runtime.outlet.cancel_root(turn_id,reason)
        with self.store.connection(write=True) as db:
            mode=db.execute('SELECT * FROM expression_runtime_turns WHERE turn_id=?',(turn_id,)).fetchone()
            if not mode:raise ExpressionAssetError('expression_turn_missing')
            completed=db.execute('SELECT state FROM expression_publications WHERE turn_id=?',(turn_id,)).fetchone()
            if completed and completed[0] in ('closed','complete'):return {'state':completed[0],'already_done':True,'new_call':False}
            db.execute('UPDATE expression_runtime_turns SET cancelled_at=? WHERE turn_id=?',(timestamp(),turn_id))
            db.execute("UPDATE expression_runs SET state='cancelled',error_code=? WHERE turn_id=?",(reason,turn_id))
            pub=db.execute('SELECT * FROM expression_publications WHERE turn_id=?',(turn_id,)).fetchone()
            if pub and pub['state'] not in ('closed','complete'):self._cancel_pending(db,pub,reason)
            elif not pub:db.execute("UPDATE turns SET cancelled_at=?,error_code=? WHERE id=?",(timestamp(),reason,turn_id))
            return {'state':'cancelled','new_call':False}

    def visible(self, persona_id, session_id, turn_id):
        uid=self._direct_main(turn_id)
        if uid:return self.runtime.outlet.direct.publication.visible(persona_id,session_id,uid)
        with self.store.connection() as db:
            self._owner(db,persona_id,session_id,turn_id)
            return [dict(r) for r in db.execute('SELECT id,ordinal,text,receipt_id,presented_at FROM expression_utterances WHERE turn_id=? ORDER BY ordinal',(turn_id,))]

    def pending_receipts(self, persona_id, session_id, turn_id, presenter_id):
        uid=self._direct_main(turn_id)
        if uid:return self.runtime.outlet.direct.publication.pending_receipts(persona_id,session_id,uid,presenter_id)
        """重启呈现端核对原租约；只返回身份和状态，不再次交付正文。"""
        text(presenter_id,'presenter_id',200)
        with self.store.connection() as db:
            self._owner(db,persona_id,session_id,turn_id)
            return [dict(r) for r in db.execute('''SELECT i.id,i.publication_id,i.revision,i.ordinal,
                i.lease_id,i.presenter_id,i.content_hash AS body_hash,i.state,i.offered_at
                FROM expression_presentation_items i JOIN expression_publications p ON p.id=i.publication_id
                WHERE p.turn_id=? AND p.state NOT IN ('complete','closed') AND i.presenter_id=?
                AND i.state IN ('offered','unknown') ORDER BY i.ordinal''',(turn_id,presenter_id))]
