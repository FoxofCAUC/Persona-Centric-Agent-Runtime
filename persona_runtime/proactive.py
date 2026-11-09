"""Explicit appointments only. Coordination uses the existing managed executor."""
from datetime import datetime,timedelta,timezone
import json
import time

from .models import LOCAL_USER_ID,RuntimeErrorBase,identifier,timestamp
from .organization import encoded,digest
from .private_personas import provider_identity,guard_cloud_dispatch

LOCAL=timezone(timedelta(hours=8))


class ProactiveService:
    def __init__(self,engine):self.engine=engine;self.store=engine.store;self.config=engine.config;self.clock=time.time

    def authorize(self,persona_id,expected_selected,expected_provider,operation_id):
        from .private_personas import PrivatePersonas
        with self.store.connection() as db:
            if self.store.current(db)!=expected_selected or persona_id!=expected_selected:raise RuntimeErrorBase('persona_selection_conflict')
        private=PrivatePersonas(self.store.paths)
        if private.record_for_persona(persona_id) is None:return {'required':False,'private_grant_created':False}
        return private.authorize_model(self.store,persona_id,'proactive',expected_provider,operation_id)

    def policy(self,db,pid):
        row=db.execute('SELECT * FROM proactive_policies WHERE persona_id=? ORDER BY revision DESC LIMIT 1',(pid,)).fetchone()
        if row:return dict(row)|{'data':json.loads(row['data_json'])}
        cfg=self.config.load()
        return {'id':None,'persona_id':pid,'revision':0,'data':{'enabled':False,
            'scope':{'persona_id':pid,'user_id':LOCAL_USER_ID,'channel':'qq','bot':cfg['bot_qq'],'peer':cfg['owner_qq']},
            'provider':provider_identity(cfg['model']),'quiet_start':23,'quiet_end':8,
            'daily_limit':2,'minimum_gap_seconds':3600,'grace_seconds':600,'timezone':'Asia/Shanghai'}}

    def current(self,persona_id=None):
        with self.store.connection() as db:return self.policy(db,persona_id or self.store.current(db))

    @staticmethod
    def operation(db,oid,request):
        if not isinstance(oid,str) or not oid:raise RuntimeErrorBase('operation_id_required')
        row=db.execute('SELECT * FROM proactive_operations WHERE operation_id=?',(oid,)).fetchone()
        if row:
            if row['request_hash']!=digest(request):raise RuntimeErrorBase('operation_conflict')
            return json.loads(row['result_json'])

    @staticmethod
    def record(db,oid,request,result):
        db.execute('INSERT INTO proactive_operations VALUES(?,?,?,?)',(oid,digest(request),encoded(result),timestamp()))
        return result

    def update(self,persona_id,data,expected_version,expected_selected,operation_id):
        request=[persona_id,data,expected_version,expected_selected]
        with self.store.connection(write=True) as db:
            if self.store.current(db)!=expected_selected or expected_selected!=persona_id:raise RuntimeErrorBase('persona_selection_conflict')
            old=self.operation(db,operation_id,request)
            if old is not None:return old
            current=self.policy(db,persona_id)
            if current['revision']!=expected_version:raise RuntimeErrorBase('version_conflict')
            if not isinstance(data,dict) or not set(data)<=set(current['data']):raise RuntimeErrorBase('proactive_policy_invalid')
            value=current['data']|data
            cfg=self.config.load();scope=value['scope']
            if (type(value['enabled']) is not bool or value['timezone']!='Asia/Shanghai' or
                scope!={'persona_id':persona_id,'user_id':LOCAL_USER_ID,'channel':'qq','bot':cfg['bot_qq'],'peer':cfg['owner_qq']} or
                value['enabled'] and not (cfg['bot_qq'] and cfg['owner_qq']) or value['provider']!=provider_identity(cfg['model'])):
                raise RuntimeErrorBase('proactive_policy_scope_invalid')
            if value['enabled'] and cfg['runtime']['qq_persona_id'] and cfg['runtime']['qq_persona_id']!=persona_id:
                raise RuntimeErrorBase('proactive_qq_persona_binding_mismatch')
            for field,low,high in [('quiet_start',0,23),('quiet_end',0,23),('daily_limit',1,2),('minimum_gap_seconds',3600,86400),('grace_seconds',1,600)]:
                if type(value[field]) is not int or not low<=value[field]<=high:raise RuntimeErrorBase('proactive_policy_limit_invalid')
            ident=identifier()
            db.execute('INSERT INTO proactive_policies VALUES(?,?,?,?,?)',(ident,persona_id,current['revision']+1,encoded(value),timestamp()))
            return self.record(db,operation_id,request,self.policy(db,persona_id))

    def rules(self,persona_id=None):
        with self.store.connection() as db:
            pid=persona_id or self.store.current(db)
            return [dict(r)|{'data':json.loads(r['data_json'])} for r in db.execute('SELECT * FROM proactive_rules r WHERE persona_id=? AND revision=(SELECT max(revision) FROM proactive_rules x WHERE x.rule_id=r.rule_id)',(pid,))]

    def update_rule(self,persona_id,data,expected_version,expected_selected,operation_id,rule_id=None):
        request=[persona_id,data,expected_version,expected_selected,rule_id]
        with self.store.connection(write=True) as db:
            if self.store.current(db)!=expected_selected or expected_selected!=persona_id:raise RuntimeErrorBase('persona_selection_conflict')
            old=self.operation(db,operation_id,request)
            if old is not None:return old
            row=db.execute('SELECT * FROM proactive_rules WHERE rule_id=? AND persona_id=? ORDER BY revision DESC LIMIT 1',(rule_id,persona_id)).fetchone()
            if (row['revision'] if row else 0)!=expected_version:raise RuntimeErrorBase('version_conflict')
            if (not isinstance(data,dict) or set(data)!={'enabled','kind','mode','content','at','time'} or
                type(data['enabled']) is not bool or data['kind'] not in ('once','daily') or data['mode'] not in ('fixed','model') or
                not isinstance(data['content'],str) or not 1<=len(data['content'])<=2000):raise RuntimeErrorBase('proactive_rule_invalid')
            try:
                if data['kind']=='once':
                    scheduled=datetime.fromisoformat(data['at'])
                    if scheduled.tzinfo is None:raise ValueError
                else:datetime.strptime(data['time'],'%H:%M')
            except (ValueError,TypeError):raise RuntimeErrorBase('proactive_rule_time_invalid') from None
            root=rule_id or identifier();ident=identifier()
            db.execute('INSERT INTO proactive_rules VALUES(?,?,?,?,?,?)',(ident,root,persona_id,expected_version+1,encoded(data),timestamp()))
            return self.record(db,operation_id,request,{'id':ident,'rule_id':root,'revision':expected_version+1,'data':data})

    @staticmethod
    def quiet(policy,now):
        hour=datetime.fromtimestamp(now,LOCAL).hour;a,b=policy['quiet_start'],policy['quiet_end']
        return a<=hour<b if a<b else hour>=a or hour<b if a>b else False

    def context(self,db,pid,content,*,guard_only=False):
        from .profile import snapshot,source_blocked
        from .service import RuntimeService
        from .companion_im import INSTRUCTION
        profiles=snapshot(db,pid)
        visible={k:v for k,v in profiles.items() if k in ('core','relationship') and not source_blocked(db,v['source_ids'])}
        spec=RuntimeService._persona(db,pid)['spec']
        from .character import head as character_head
        character=character_head(db,pid)
        guard=digest([spec,{k:[v['id'],v['source_ids']] for k,v in visible.items()},character['id']])
        if guard_only:return {'guard':guard}
        from .character_policy import choose
        selection=self.engine.chat.cognitive.characters.selection(character,choose('hybrid'),[content],limit=2)
        from .character_context import render_parts
        selected_parts=[p for p in selection['parts'] if p['kind']!='example']
        selection=selection|{'parts':selected_parts,'text':render_parts(selected_parts),'example_ids':[]}
        # Same v8 text guidance, no expression examples, no autonomous recall or writes.
        messages=[{'role':'system','content':INSTRUCTION+encoded({'persona':spec,'profiles':{k:v['data'] for k,v in visible.items()},
                'current_character':selection['text']})},
            {'role':'user','content':'这是用户明确约定的提醒或问候。只返回这次要发送的消息正文，不创建任务，不声称已经发送，不加说明或结构包装。约定：'+content}]
        if sum(len(m['content']) for m in messages)>self.config.load()['model']['max_input_chars']:
            raise RuntimeErrorBase('proactive_context_budget_exceeded')
        return {'messages':messages,'profile_refs':[{'kind':'profile','id':v['id'],'revision':v['revision']} for v in visible.values()]+
            [{'kind':'character','id':character['id'],'revision':character['revision']}],
            'character_selection':{k:v for k,v in selection.items() if k not in ('parts','text','candidate_preview')},
            'guidance':'companion-im-instruction-v1','examples':False,'guard':guard}

    def check(self,db,task,payload):
        from .persona_management import active
        active(db,task['persona_id']);p=payload['proactive']
        policy=self.policy(db,task['persona_id'])
        if policy['id']!=p['policy_id'] or not policy['data']['enabled']:raise RuntimeErrorBase('proactive_authorization_changed')
        rule=db.execute('SELECT id FROM proactive_rules WHERE rule_id=? ORDER BY revision DESC LIMIT 1',(p['rule_id'],)).fetchone()
        if not rule or rule['id']!=p['rule_version']:raise RuntimeErrorBase('proactive_rule_changed')
        if self.clock()>p['expires_epoch'] or self.quiet(policy['data'],self.clock()):raise RuntimeErrorBase('proactive_expired')
        cfg=self.config.load()
        if cfg['runtime']['qq_persona_id']!=task['persona_id']:raise RuntimeErrorBase('proactive_qq_persona_binding_mismatch')
        if (policy['data']['scope']!=payload['scope'] or (cfg['bot_qq'],cfg['owner_qq'])!=(payload['scope']['bot'],payload['scope']['peer']) or
            provider_identity(json.loads(task['model_snapshot']))!=policy['data']['provider'] or provider_identity(cfg['model'])!=policy['data']['provider']):
            raise RuntimeErrorBase('proactive_scope_or_model_changed')
        guard_cloud_dispatch(self.store,task['persona_id'],'proactive',json.loads(task['model_snapshot']))
        if self.context(db,task['persona_id'],p['content'],guard_only=True)['guard']!=p['context']['guard']:raise RuntimeErrorBase('source_expired')

    def tick(self,now=None):
        now=time.time() if now is None else now
        from .lifecycle import read_console
        lease=read_console(self.store.paths)
        if not lease or lease['state']!='active' or self.engine.draining or self.engine.chat.draining:return
        opened=datetime.fromisoformat(lease['at']).timestamp()
        with self.store.connection(write=True) as db:
            rules=db.execute('SELECT * FROM proactive_rules r WHERE revision=(SELECT max(revision) FROM proactive_rules x WHERE x.rule_id=r.rule_id)').fetchall()
            for rule in rules:
                data=json.loads(rule['data_json']);policy=self.policy(db,rule['persona_id']);p=policy['data']
                if not data['enabled'] or not p['enabled']:continue
                from .persona_management import active
                try:active(db,rule['persona_id'])
                except RuntimeErrorBase:continue
                local=datetime.fromtimestamp(now,LOCAL)
                if data['kind']=='once':due=datetime.fromisoformat(data['at']).timestamp()
                else:
                    h,m=map(int,data['time'].split(':'));due=local.replace(hour=h,minute=m,second=0,microsecond=0).timestamp()
                if due>now or due<datetime.fromisoformat(rule['created_at']).timestamp():continue
                if db.execute('SELECT 1 FROM proactive_occurrences WHERE rule_id=? AND scheduled_epoch=?',(rule['rule_id'],due)).fetchone():continue
                start=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
                previous=db.execute("SELECT count(*),max(scheduled_epoch) FROM proactive_occurrences WHERE persona_id=? AND scope_json=? AND state NOT IN ('NO_ACTION','expired') AND scheduled_epoch>=?",(rule['persona_id'],encoded(p['scope']),start)).fetchone()
                last=db.execute("SELECT max(scheduled_epoch) FROM proactive_occurrences WHERE persona_id=? AND scope_json=? AND state NOT IN ('NO_ACTION','expired')",(rule['persona_id'],encoded(p['scope']))).fetchone()[0]
                reason='missed_while_closed' if due<opened else 'expired' if now-due>p['grace_seconds'] else 'quiet_hours' if self.quiet(p,now) else 'frequency_limit' if previous[0]>=p['daily_limit'] or last is not None and due-last<p['minimum_gap_seconds'] else None
                ident=identifier();expires=due+p['grace_seconds']
                db.execute('INSERT INTO proactive_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ident,rule['rule_id'],rule['id'],rule['persona_id'],LOCAL_USER_ID,encoded(p['scope']),due,expires,
                     'NO_ACTION' if reason else 'accepted',None,None,encoded({'reason':reason,'policy_id':policy['id']}),now))
                if reason:continue
                if data['mode']=='fixed':self.enqueue(db,ident,data['content']);continue
                try:
                    guard_cloud_dispatch(self.store,rule['persona_id'],'proactive',self.config.load()['model'])
                    context=self.context(db,rule['persona_id'],data['content'])
                    payload={'mode':'proactive','persona_id':rule['persona_id'],'user_id':LOCAL_USER_ID,'channel':'qq',
                        'scope':p['scope'],'events':[],'rows':[],'proactive':{'occurrence_id':ident,'rule_id':rule['rule_id'],
                        'rule_version':rule['id'],'policy_id':policy['id'],'expires_epoch':expires,'content':data['content'],'context':context}}
                    payload['fingerprint']=digest(payload)
                    task=self.engine.managed.register(db,payload,'proactive:'+ident,automatic=3,trigger='scheduled')
                    db.execute("UPDATE proactive_occurrences SET task_id=?,state='queued' WHERE id=?",(task['id'],ident))
                except RuntimeErrorBase as error:
                    db.execute("UPDATE proactive_occurrences SET state='NO_ACTION',decision_json=? WHERE id=?",(encoded({'reason':str(error)}),ident))

    @staticmethod
    def enqueue(db,occurrence_id,text):
        occurrence=db.execute('SELECT * FROM proactive_occurrences WHERE id=?',(occurrence_id,)).fetchone()
        if db.execute('SELECT 1 FROM reply_batches WHERE outbound_id=?',(occurrence_id,)).fetchone():return
        from .delivery import DeliveryService
        scope=json.loads(occurrence['scope_json']);batch=identifier();pid=occurrence['persona_id']
        db.execute('INSERT INTO reply_batches (id,turn_id,attempt_id,persona_id,user_id,schema_version,mode,raw_fingerprint,segment_count,total_chars,created_at,outbound_id) VALUES(?,NULL,NULL,?,?,1,\'context_full\',?,1,?,?,?)',
            (batch,pid,LOCAL_USER_ID,digest(text),len(text),timestamp(),occurrence_id))
        db.execute('INSERT INTO reply_segments VALUES(?,?,?,?,?,?,?,?,?)',(batch,pid,LOCAL_USER_ID,1,text,'plain','chat',0,0))
        DeliveryService.create_for_batch(None,db,batch,{'id':None,'channel':'qq','persona_id':pid,'outbound_id':occurrence_id,
            'origin_json':encoded({'bot':scope['bot'],'sender':scope['peer']})})
        db.execute("UPDATE proactive_occurrences SET state='enqueued',text=? WHERE id=?",(text,occurrence_id))

    def publish(self,db,task,payload,reply):
        if not isinstance(reply,str) or not 1<=len(reply.strip())<=2000:raise RuntimeErrorBase('proactive_output_invalid')
        self.enqueue(db,payload['proactive']['occurrence_id'],reply.strip())

    @staticmethod
    def check_delivery(db,batch_id,now,store=None):
        row=db.execute('SELECT o.* FROM proactive_occurrences o JOIN reply_batches b ON b.outbound_id=o.id WHERE b.id=?',(batch_id,)).fetchone()
        if not row or now>row['expires_epoch']:raise RuntimeErrorBase('proactive_expired')
        from .persona_management import active
        active(db,row['persona_id'])
        policy=db.execute('SELECT * FROM proactive_policies WHERE persona_id=? ORDER BY revision DESC LIMIT 1',(row['persona_id'],)).fetchone()
        if not policy:raise RuntimeErrorBase('proactive_authorization_changed')
        data=json.loads(policy['data_json']);decision=json.loads(row['decision_json'])
        if not data['enabled'] or policy['id']!=decision['policy_id'] or ProactiveService.quiet(data,now):raise RuntimeErrorBase('proactive_authorization_changed')
        rule=db.execute('SELECT * FROM proactive_rules WHERE rule_id=? ORDER BY revision DESC LIMIT 1',(row['rule_id'],)).fetchone()
        if rule['id']!=row['rule_version'] or not json.loads(rule['data_json'])['enabled']:raise RuntimeErrorBase('proactive_rule_changed')
        if store is not None:
            from .config import ConfigStore
            cfg=ConfigStore(store.paths).load();scope=json.loads(row['scope_json'])
            if cfg['runtime']['qq_persona_id']!=row['persona_id']:raise RuntimeErrorBase('proactive_qq_persona_binding_mismatch')
            if (cfg['bot_qq'],cfg['owner_qq'])!=(scope['bot'],scope['peer']):raise RuntimeErrorBase('proactive_scope_or_model_changed')
            if row['task_id']:
                from types import SimpleNamespace
                service=ProactiveService(SimpleNamespace(store=store,config=ConfigStore(store.paths)))
                task=db.execute('SELECT * FROM organization_tasks WHERE id=?',(row['task_id'],)).fetchone()
                service.check(db,task,json.loads(task['payload_json']))

    def occurrences(self,persona_id=None):
        with self.store.connection() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM proactive_occurrences WHERE persona_id=? ORDER BY scheduled_epoch DESC LIMIT 100',(persona_id or self.store.current(db),))]
            for row in rows:
                row['decision']=json.loads(row.pop('decision_json'))
                task=db.execute('SELECT status,error_code FROM organization_tasks WHERE id=?',(row['task_id'],)).fetchone()
                row['task']=dict(task) if task else None
                row['delivery']=[dict(p) for p in db.execute('SELECT p.part_index,p.state,p.isolated FROM reply_delivery_parts p JOIN reply_batches b ON b.id=p.reply_batch_id WHERE b.outbound_id=? ORDER BY p.part_index',(row['id'],))]
            return rows
