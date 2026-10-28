"""Bounded M7.3 real acceptance with durable per-slot intent; never replays a slot."""
import argparse
import json
import re
import time
from pathlib import Path
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.lifecycle import ServiceController
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.storage import Store


class Acceptance:
    def __init__(self,output):
        self.paths=RuntimePaths.from_environment();self.out=output.resolve()
        if not self.out.is_relative_to(self.paths.home/'exports/acceptance'):raise ValueError('Use private acceptance output')
        self.cfg=ConfigStore(self.paths);self.store=Store(self.paths);self.client=LocalClient(self.cfg)
        self.file=self.out/'real-checkpoint.json';self.state=json.loads(self.file.read_text(encoding='utf-8'))
        self.pid=self.state['persona_id'];self.original=self.state['original_persona_id']
    def save(self):atomic_json(self.file,self.state)
    def use(self,pid):self.client.manage('persona.use',persona_id=pid)
    def scope(self,channel='desktop'):
        result={'persona_id':self.pid,'channel':channel}
        if channel=='qq':
            c=self.cfg.load();result|={'bot':c['bot_qq'],'peer':c['owner_qq']}
        return result
    def online(self):
        h=ServiceController(self.paths).health('astrbot')
        if h.get('login') is not True or h.get('onebot') is not True:raise RuntimeError('QQ unavailable; stop and preserve checkpoint')
    def count_calls(self):
        with self.store.connection() as db:
            return sum(db.execute(sql,(self.pid,)).fetchone()[0] for sql in
                       ('SELECT count(*) FROM attempts a JOIN turns t ON t.id=a.turn_id WHERE t.persona_id=?',
                        'SELECT count(*) FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE t.persona_id=?'))
    def reserve(self,slot,kind,request=None):
        if slot in self.state['calls']:raise RuntimeError('Slot already reserved: inspect stable receipt, never automatically replay '+slot)
        if self.count_calls()>=9:raise RuntimeError('Nine-call hard limit reached')
        self.state['calls'][slot]={'status':'reserved','kind':kind,'request':request};self.save()
    def seal(self,sid,slot,channel='desktop'):
        args=self.scope(channel)|{'session_id':sid}
        row=self.client.manage('session.show',**args)
        if row['state']=='sealed':return row
        result=self.client.manage('session.seal',**args,expected_revision=row['revision'],operation_id='m73-'+slot+'-seal')
        if result['state']!='sealed':raise RuntimeError('Unresolved seal blockers')
        package=self.client.manage('session.package',**args)
        atomic_json(self.out/(slot+'-package.json'),package);return result
    def send(self,slot,text,sid,reference=None,members=None):
        self.online();request={'channel':'desktop','persona_id':self.pid,'session_id':sid,'request_id':'m73-real-'+slot,'text':text}
        if reference:request['reference']=reference
        self.reserve(slot,'major',request)
        for n,value in enumerate(members or [text]):
            payload=request if not members else request|{'text':value,'request_id':request['request_id']+'-'+str(n)}
            if n:time.sleep(1)
            receipt=self.client.submit_input(payload);self.state['calls'][slot].setdefault('receipts',[]).append(receipt);self.save()
        end=time.monotonic()+180
        while time.monotonic()<end:
            receipt=self.client.lookup(payload)
            if receipt and receipt.get('turn_id'):
                turn=self.client.call('GET','/v1/requests/'+receipt['turn_id'])
                if turn['status'] not in ('queued','running'):return self.record_turn(slot,turn,'desktop')
            time.sleep(.4)
        raise RuntimeError('Outcome unconfirmed; preserve receipt')
    def record_turn(self,slot,turn,channel):
        manifests=self.client.manage('context.inspect',**self.scope(channel),turn_id=turn['id'])['manifests']
        result={'turn':turn,'manifests':manifests,'amount':'unknown','user_read':'unknown'}
        atomic_json(self.out/(slot+'.json'),result)
        self.state['calls'][slot].update(status=turn['status'],turn_id=turn['id'],session_id=turn['session_id'],attempts=turn['attempt_count'],usage=turn['usage']);self.save()
        if turn['status']!='completed' or turn['attempt_count']!=1 or len(manifests)!=1:raise RuntimeError('Request gate failed; no automatic retry')
        return result
    def desktop(self):
        self.online()
        try:
            with QuotaService(self.store,self.cfg).acceptance():
                self.use(self.pid)
                if 'R01' not in self.state['calls']:
                    sid=self.client.manage('session.select')['session_id'];self.state['desktop_seed']=sid;self.save()
                    old=self.client.manage('interaction.current',**self.scope())
                    self.client.manage('interaction.update',**self.scope(),mode='natural_batch',expected_version=old['id'],operation_id='m73-desktop-natural')
                    result=self.send('R01','',sid,members=['M73-R01：虚构青禾书店的阅读角。','预算三百元，偏好蓝色。','请确认收到三条资料，不复述内容。'])
                    self.state['seed_turn']=result['turn']['id'];self.save()
                sid=self.state['desktop_seed'];sealed=self.seal(sid,'R01')
                if 'continued_session' not in self.state:
                    self.state['continued_session']=self.client.manage('session.reopen',**self.scope(),session_id=sid,expected_revision=sealed['revision'],operation_id='m73-reopen')['session_id'];self.save()
                new=self.state['continued_session']
                if 'R02' not in self.state['calls']:
                    self.send('R02','M73-R02：旧会话里虚构书店叫什么，预算和偏好颜色是多少？当前没有资料就说不知道，不要猜。',new)
                if 'R03' not in self.state['calls']:
                    self.send('R03','M73-R03：请依据明确引用的完整回合，说出书店名称、预算和颜色。',new,{'kind':'turn','id':self.state['seed_turn']})
                self.seal(new,'R03')
        finally:self.use(self.original)
    def organize(self,channel_filter=None):
        self.online()
        try:
            with QuotaService(self.store,self.cfg).acceptance():
                self.use(self.pid)
                for slot,sid,channel in [('R06-desktop',self.state['desktop_seed'],'desktop'),('R06-qq',self.state['calls']['R04']['session_id'],'qq')]:
                    if channel_filter and channel_filter!=channel:continue
                    if slot in self.state['calls']:continue
                    args=self.scope(channel);preview=self.client.manage('session.organization.preview',**args,session_id=sid)
                    self.reserve(slot,'organization')
                    task=self.client.manage('session.organization.submit',**args,preview=preview,expected_revision=preview['lifecycle_revision'],operation_id='m73-'+slot)
                    self.state['calls'][slot]['task_id']=task['id'];self.save();end=time.monotonic()+180
                    while time.monotonic()<end:
                        detail=self.client.manage('organization.show',task_id=task['id'])
                        if detail['status'] not in ('queued','running','waiting_quota'):break
                        time.sleep(.4)
                    manifests=self.client.manage('context.inspect',**args,task_id=task['id'])['manifests']
                    atomic_json(self.out/(slot+'.json'),{'task':detail,'manifests':manifests,'amount':'unknown'})
                    self.state['calls'][slot].update(status=detail['status']);self.save()
                    if detail['status']!='completed' or len(detail['attempts'])!=1 or len(manifests)!=1:raise RuntimeError('Organization gate failed; preserve scene')
        finally:self.use(self.original)
    def qq(self,slot):
        self.online();self.reserve(slot,'actual_qq');marker='M73-'+slot
        with self.store.connection() as db:cutoff=db.execute('SELECT coalesce(max(rowid),0) FROM turns').fetchone()[0]
        try:
            with QuotaService(self.store,self.cfg).acceptance():
                self.use(self.pid);atomic_json(self.out/(slot+'-ready.json'),{'ready':True,'marker':marker,'synthetic':False})
                end=time.monotonic()+600;next_health=time.monotonic()+10
                while time.monotonic()<end:
                    if time.monotonic()>next_health:self.online();next_health=time.monotonic()+10
                    with self.store.connection() as db:
                        row=db.execute("SELECT t.id FROM turns t JOIN events e ON t.user_event_id=e.id WHERE t.persona_id=? AND t.channel='qq' AND t.rowid>? AND e.text LIKE ? ORDER BY t.rowid LIMIT 1",(self.pid,cutoff,marker+'%')).fetchone()
                    if not row:time.sleep(.4);continue
                    turn=self.client.call('GET','/v1/requests/'+row[0])
                    if turn['status'] in ('queued','running'):time.sleep(.4);continue
                    if turn['status']!='completed':self.record_turn(slot,turn,'qq')
                    with self.store.connection() as db:
                        parts=[dict(p) for p in db.execute('SELECT id,text,state,platform_message_id,sent_at FROM reply_delivery_parts WHERE turn_id=? ORDER BY part_index',(turn['id'],))]
                        raw=[dict(p) for p in db.execute('SELECT channel,bot,peer,platform_message_id FROM raw_events WHERE event_id=?',(turn['user_event_id'],))]
                    if any(p['state'] in ('pending','sending') for p in parts):time.sleep(.4);continue
                    if not parts or not all(p['state']=='sent' and p['platform_message_id'] and not re.search(r'\*\*|__|```|(?m:^\s{0,3}#{1,6}\s)',p['text']) for p in parts):raise RuntimeError('Delivery or plain text gate failed')
                    if not raw or not raw[0]['platform_message_id']:raise RuntimeError('Missing actual platform source')
                    self.record_turn(slot,turn,'qq');atomic_json(self.out/(slot+'-platform.json'),{'parts':parts,'raw':raw,'readback':'not_performed','user_read':'unknown'})
                    if slot=='R04':
                        self.state['idle_observation']={'session_id':turn['session_id'],'sent_at':parts[-1]['sent_at'],'started_monotonic':time.monotonic()};self.save()
                    elif turn['session_id']==self.state['calls']['R04']['session_id']:raise RuntimeError('Sealed session reused')
                    return
                raise RuntimeError('Actual QQ input not observed; do not synthesize')
        finally:self.use(self.original)
    def idle(self):
        sid=self.state['calls']['R04']['session_id'];args=self.scope('qq')|{'session_id':sid};records=[]
        deadline=time.monotonic()+1830
        try:
            while time.monotonic()<deadline:
                if (self.out/'stop-real-idle-observation.json').exists():return
                self.online();row=self.client.manage('session.show',**args)
                records.append({'observed_monotonic':time.monotonic(),'state':row['state'],'idle_seconds':row['idle_seconds'],'last_activity_at':row['last_activity_at'],'blockers':row['blockers']})
                atomic_json(self.out/'idle-observation.json',{'samples':records,'threshold_modified':False})
                if row['state']=='sealed':
                    from datetime import datetime
                    seconds=(datetime.fromisoformat(row['sealed_at'])-datetime.fromisoformat(self.state['idle_observation']['sent_at'])).total_seconds()
                    package=self.client.manage('session.package',**args)
                    atomic_json(self.out/'R04-package.json',package)
                    result={'real_idle_seconds':seconds,'passed':1800<=seconds<=1810,'samples':len(records),'package_id':package['id']}
                    atomic_json(self.out/'R04-idle-result.json',result)
                    if not result['passed']:raise RuntimeError('Idle timing outside hard gate')
                    return
                time.sleep(5)
        finally:
            # Desktop acceptance may have advanced the checkpoint while this
            # passive observer ran. Never overwrite its completed call slots.
            latest=json.loads(self.file.read_text(encoding='utf-8'))
            latest['idle_samples']=len(records);atomic_json(self.file,latest)
        raise RuntimeError('Idle sealing not completed')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--phase',choices=['desktop','organize','organize-desktop','R04','R05','idle'],required=True)
    a=p.parse_args();d=Acceptance(a.output)
    if a.phase in ('R04','R05'):d.qq(a.phase)
    elif a.phase=='organize-desktop':d.organize('desktop')
    else:getattr(d,a.phase)()
