"""Opt-in real acceptance; stable IDs and private D-drive checkpoints, no retry."""
import argparse
import json
import time
from pathlib import Path

from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import InstanceLock
from persona_runtime.lifecycle import ServiceController
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.storage import Store


class Acceptance:
    def __init__(self,output):
        self.out=output.resolve();self.paths=RuntimePaths.from_environment()
        if not self.out.is_relative_to(self.paths.home/'exports/acceptance'):
            raise ValueError('Private evidence must stay in runtime exports/acceptance')
        self.file=self.out/'real-checkpoint.json';self.state=json.loads(self.file.read_text(encoding='utf-8'))
        self.cfg=ConfigStore(self.paths);self.store=Store(self.paths);self.client=LocalClient(self.cfg)

    def save(self):atomic_json(self.file,self.state)

    def online(self):
        h=ServiceController(self.paths).health('astrbot')
        if h.get('login') is not True or h.get('onebot') is not True:raise RuntimeError('QQ is not verified online; stop real acceptance')

    def use(self,pid):return self.client.manage('persona.use',persona_id=pid)

    def session(self):return self.client.manage('session.select',fresh=True)['session_id']

    def policy(self,pid,**fields):
        old=self.client.manage('interaction.current',persona_id=pid,channel='desktop')
        return self.client.manage('interaction.update',persona_id=pid,channel='desktop',mode=fields.pop('mode',old['mode']),
            expected_version=old['id'],operation_id='m72-real-policy-'+str(time.time_ns()),**fields)

    def result(self,slot,turn):
        manifests=self.client.manage('context.inspect',persona_id=turn['persona_id'],channel='desktop',turn_id=turn['id'])['manifests']
        output={'slot':slot,'turn':turn,'manifests':manifests,'amount':'unknown'}
        atomic_json(self.out/(slot+'.json'),output)
        item=self.state['calls'][slot];item.update(status=turn['status'],turn_id=turn['id'],attempts=turn['attempt_count'],usage=turn['usage'],error=turn['error_code'])
        self.save()
        if turn['status']!='completed' or turn['attempt_count']!=1:raise RuntimeError(slot+' did not complete exactly once')
        if len(manifests)!=1 or not manifests[0].get('input_snapshot_hash'):raise RuntimeError('Missing pre-call manifest')
        return output

    def send(self,slot,text,sid,reference=None,members=None):
        self.online()
        if slot in self.state['calls']:raise RuntimeError('Slot already reserved; inspect checkpoint instead of resubmitting '+slot)
        with self.store.connection() as db:
            marks=','.join('?' for _ in self.state['personas'])
            count=db.execute(f'SELECT count(*) FROM attempts a JOIN turns t ON t.id=a.turn_id WHERE t.persona_id IN ({marks})',self.state['personas']).fetchone()[0]
        if count>=(11 if '-F' in slot else 8):raise RuntimeError('Major-call budget exhausted')
        request={'channel':'desktop','request_id':'m72-real-'+slot,'text':text,'session_id':sid}
        if reference:request['reference']=reference
        self.state['calls'][slot]={'status':'reserved','request':request};self.save()
        for i,value in enumerate(members or [text]):
            payload=request if not members else request|{'text':value,'request_id':request['request_id']+f'-m{i+1}'}
            if members and i:time.sleep(1)
            receipt=self.client.submit_input(payload)
            self.state['calls'][slot].setdefault('receipts',[]).append(receipt);self.save()
        deadline=time.monotonic()+160
        while time.monotonic()<deadline:
            found=self.client.lookup(payload)
            if found and found.get('turn_id'):
                turn=self.client.call('GET','/v1/requests/'+found['turn_id'])
                if turn['status'] not in ('queued','running'):return self.result(slot,turn)
            time.sleep(.3)
        raise RuntimeError('Result still unconfirmed; do not resubmit')

    def desktop(self):
        self.online();a,b=self.state['personas'];q=QuotaService(self.store,self.cfg)
        if any(v['status']!='completed' for v in self.state['calls'].values()):
            raise RuntimeError('An earlier slot is unresolved; inspect its stable receipt before more calls')
        original=json.loads((self.out/'formal-upgrade.json').read_text(encoding='utf-8'))['original_persona_id']
        try:
            with q.acceptance():
                self.use(a)
                if 'R01' not in self.state['calls']:
                    self.send('R01','工程验收：根据本会话人工档案，虚构青禾书店阅读角的预算和偏好颜色是什么？没有资料不要猜。',self.state['long_session'])
                if 'R02' not in self.state['calls']:
                    sid=self.session();self.state['batch_session']=sid;self.save();self.policy(a,mode='natural_batch')
                    self.send('R02','',sid,members=['M72连续补充：虚构清泉书店需要阅读角。','预算四百元，偏好绿色。','请合并前两条，说明书店名、预算和颜色。'])
                    self.policy(a,mode='legacy_per_message')
                if 'R03' not in self.state['calls']:
                    self.send('R03','你的血型是什么？如果资料未设定，请明确说未知或未设定，不要猜。',self.session())
                if 'R04' not in self.state['calls']:
                    self.use(b);self.send('R04','我之前说过的虚构青禾书店预算是多少？请只据你可用的资料回答，没有资料不要猜。',self.session());self.use(a)
                if 'R05' not in self.state['calls']:
                    self.send('R05','请根据明确引用的完整回合，说明虚构远山书店的预算与偏好颜色。',self.state['long_session'],{'kind':'event','id':'m72-real-outside-e3'})
                if 'R06' not in self.state['calls']:
                    from tests.m71_fixtures import seed_turn
                    sid=self.session()
                    with self.store.connection(write=True) as db:
                        seed_turn(db,a,sid,'m72-legacy-book',['M7.2人工样例：虚构花溪书店的预算为六百元，偏好橙色。'])
                        for n in range(20):seed_turn(db,a,sid,'m72-legacy-filler-'+str(n),['人工无关 filler_'+str(n)])
                    self.policy(a,context_mode='legacy_recent')
                    self.send('R06','虚构花溪书店的预算与偏好颜色是什么？仅据当前可用资料回答，没有资料不要猜。',sid)
                    self.policy(a,context_mode='working_set')
                if 'R07' not in self.state['calls']:
                    sid=self.session();lock=InstanceLock(self.paths.home/'cache/archive-search/v1/maintenance.lock')
                    try:self.send('R07','M72检索故障演练：只回复“基础聊天仍可用”。',sid)
                    finally:lock.close()
                if 'R09' not in self.state['calls']:
                    self.online()
                    preview=self.client.manage('organization.preview',session_id=self.state['batch_session'])
                    self.state['calls']['R09']={'status':'reserved','kind':'organization'};self.save()
                    task=self.client.manage('organization.submit',preview=preview,request_id='m72-real-R09')
                    self.state['calls']['R09']['task_id']=task['id'];self.save()
                    deadline=time.monotonic()+160
                    while time.monotonic()<deadline:
                        result=self.client.manage('organization.show',task_id=task['id'])
                        if result['status'] not in ('queued','running','waiting_quota'):break
                        time.sleep(.4)
                    manifests=self.client.manage('context.inspect',persona_id=a,channel='desktop',task_id=task['id'])['manifests']
                    atomic_json(self.out/'R09.json',{'task':result,'manifests':manifests,'amount':'unknown'})
                    self.state['calls']['R09'].update(status=result['status']);self.save()
                    if result['status']!='completed' or len(manifests)!=1:raise RuntimeError('Organization acceptance failed; do not retry automatically')
        finally:
            self.use(original)
        print(json.dumps({'completed_slots':{k:v['status'] for k,v in self.state['calls'].items()},'quota_restored':not q.current()['acceptance_active']},ensure_ascii=False))

    def repair_r01(self):
        self.online();a=self.state['personas'][0]
        original=json.loads((self.out/'formal-upgrade.json').read_text(encoding='utf-8'))['original_persona_id']
        from tests.m71_fixtures import seed_turn
        try:
            with QuotaService(self.store,self.cfg).acceptance():
                self.use(a);sid=self.session()
                with self.store.connection(write=True) as db:
                    for n in range(56):
                        texts=['M7.2人工历史样例：虚构青禾书店需要阅读角。','预算三百元，偏好蓝色。','请合并处理。'] if n==11 else [f'M7.2人工历史填充 filler_{n}']
                        seed_turn(db,a,sid,'m72-r01-fix-'+str(n),texts)
                result=self.send('R01-F1','工程验收：根据本会话人工档案，虚构青禾书店阅读角的预算和偏好颜色是什么？没有资料不要猜。',sid)
                reply=result['turn']['reply'];groups=result['turn']['context']['history_groups']
                passed=('蓝' in reply and ('三百' in reply or '300' in reply) and any(g['turn_id']=='m72-r01-fix-11' for g in groups))
                self.state['calls']['R01-F1']['quality_passed']=passed;self.save()
                if not passed:raise RuntimeError('R01 fix did not satisfy the source and answer gate; stop')
                print(json.dumps({'slot':'R01-F1','quality_passed':passed,'attempts':result['turn']['attempt_count']}))
        finally:self.use(original)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--phase',choices=('desktop','repair-r01'),required=True)
    args=parser.parse_args();driver=Acceptance(args.output)
    if args.phase=='desktop':driver.desktop()
    else:driver.repair_r01()
