"""Bounded real M7.4 quality run. Durable reservations never automatically replay."""
import argparse
import hashlib
import json
import random
import time
from pathlib import Path

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.storage import Store

CASES = [
    ('greeting','早呀，今天阳光不错，随便跟我打个招呼吧'),
    ('banter','我又把钥匙放冰箱里了，轻轻吐槽我一下，别太刻薄'),
    ('support','今天做汇报卡壳了，有点沮丧，陪我说两句，不用给计划'),
    ('celebrate','我终于完成拖了两周的课程作业啦，跟我庆祝一下'),
    ('acknowledge','我晚十分钟到，只需确认收到，不需要提问'),
    ('technical','用不超过150字解释数据库事务的原子性，再举一个转账失败回滚的例子'),
    ('code','给出完整可运行的Python函数safe_divide(a,b)，除数为0返回None，否则返回商；附两个assert测试。代码不能省略，不需要追问'),
    ('quote','仅原样引用这句人工样例：“窗边两把蓝椅，预算300元。” 不改字词、数字和标点。这不是我们的真实共同经历'),
    ('exact','请完整保留并复述版本v0.7.4、价格123.45元及网址https://example.org/a_b?x=12.345&v=0.7.4，不改数字、网址，不添加其他链接'),
    ('supplements','以下是同一次虚构需求的三次补充：①书店需要阅读角；②预算300元，偏好蓝色；③请结合全部信息给两个不超预算的建议，不声称这是共同经历'),
    ('character','你多高、什么职业、什么血型？请区分设定已有和未知资料，别猜血型'),
    ('optional','请先回答2+3等于多少，结尾可加一句完全可省略的轻松结束语；主要答案不能放在结束语里'),
]


def schedule():
    rows=[{'case':name,'text':text,'mode':mode} for name,text in CASES for mode in ('legacy_full','natural_structured')]
    random.Random(7402026).shuffle(rows)
    return [r|{'slot':f'Q{i+1:02d}','anonymous':f'A{i+1:02d}'} for i,r in enumerate(rows)]


class Acceptance:
    def __init__(self, output):
        self.paths=RuntimePaths.from_environment();self.out=output.resolve()
        if not self.out.is_relative_to(self.paths.home/'exports/acceptance'):raise ValueError('Private evidence directory required')
        self.cfg=ConfigStore(self.paths);self.store=Store(self.paths);self.client=LocalClient(self.cfg)
        self.file=self.out/'real-checkpoint.json'
        self.state=json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else None

    def save(self):atomic_json(self.file,self.state)

    def setup(self):
        if self.state:return
        original=self.client.manage('persona.show')['id']
        spec=generic_persona().to_dict();spec['name']='M7.4独立验收';spec['introduction']='独立虚构测试角色，用于自然表达验收。'
        pid=self.client.manage('persona.create',spec=spec)['id']
        self.state={'persona_id':pid,'original_persona_id':original,'schedule':schedule(),'calls':{},'seed':7402026,'call_limit':30}
        self.save()
        try:
            self.client.manage('persona.use',persona_id=pid)
            char=self.client.manage('character.current',persona_id=pid);data=char['data']
            data['appearance']['height_cm']=172;data['biography']['occupation']='书店店员'
            new=self.client.manage('character.update',persona_id=pid,data=data,expected_version=char['id'],operation_id='m74-character')
            self.state['character_version']=new['id'];self.save()
            for channel in ('desktop','qq'):
                policy=self.client.manage('interaction.current',persona_id=pid,channel=channel)
                self.client.manage('interaction.update',persona_id=pid,channel=channel,mode='legacy_per_message',
                    expected_version=policy['id'],operation_id='m74-setup-'+channel,context_mode='working_set',cross_session=False)
            self.state['model_hash']=self.model_hash();self.state['setup_complete']=True;self.save()
        finally:self.client.manage('persona.use',persona_id=original)

    def model_hash(self):
        model=dict(self.cfg.load()['model']);model.pop('hourly_limit')
        return hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest()

    def online(self):
        h=ServiceController(self.paths).health('astrbot')
        if h.get('login') is not True or h.get('onebot') is not True:raise RuntimeError('QQ unavailable; checkpoint saved, no further real calls')

    def reserve(self,slot,kind,request=None):
        if slot in self.state['calls']:raise RuntimeError('Already reserved; inspect original receipt, do not replay')
        with self.store.connection() as db:
            actual=db.execute('SELECT count(*) FROM attempts a JOIN turns t ON t.id=a.turn_id WHERE t.persona_id=?',(self.state['persona_id'],)).fetchone()[0]
        if actual>=30 or len(self.state['calls'])>=30:raise RuntimeError('Thirty-call hard limit reached')
        if self.model_hash()!=self.state['model_hash']:raise RuntimeError('Model configuration changed')
        self.state['calls'][slot]={'status':'reserved','kind':kind,'request':request,'reserved_at':time.time()};self.save()

    def mode(self,channel,mode,slot):
        pid=self.state['persona_id'];old=self.client.manage('reply.current',persona_id=pid,channel=channel)
        if old['reply_mode']!=mode:
            self.client.manage('reply.update',persona_id=pid,channel=channel,reply_mode=mode,
                               expected_version=old['id'],operation_id='m74-mode-'+slot)

    def record(self,slot,turn,channel):
        args={'persona_id':self.state['persona_id'],'channel':channel,'turn_id':turn['id']}
        if channel=='qq':
            cfg=self.cfg.load();args|={'bot':cfg['bot_qq'],'peer':cfg['owner_qq']}
        manifests=self.client.manage('context.inspect',**args)['manifests']
        with self.store.connection() as db:
            attempts=[dict(r) for r in db.execute('SELECT id,started_epoch,status,usage_json FROM attempts WHERE turn_id=?',(turn['id'],))]
            parts=[{k:r[k] for k in ('id','text','semantic_index','part_index','state','attempt','platform_message_id','sent_at','wait_started_at','wait_ms','cancel_reason','trigger_event_id','created_at','updated_at')} for r in db.execute('SELECT * FROM reply_delivery_parts WHERE turn_id=? ORDER BY part_index',(turn['id'],))]
        atomic_json(self.out/(slot+'.json'),{'turn':turn,'manifests':manifests,'attempts':attempts,'parts':parts,'amount':'unknown','user_read':'unknown'})
        self.state['calls'][slot].update(status=turn['status'],turn_id=turn['id'],session_id=turn['session_id'],usage=turn['usage'],attempt_count=turn['attempt_count']);self.save()
        if turn['status']!='completed' or turn['attempt_count']!=1 or len(manifests)!=1:raise RuntimeError('Acceptance gate failed; no automatic retry')

    def quality(self):
        if not self.state or not self.state.get('setup_complete'):raise RuntimeError('Run setup first')
        self.online()
        try:
            with QuotaService(self.store,self.cfg).acceptance():
                self.client.manage('persona.use',persona_id=self.state['persona_id'])
                for case in self.state['schedule']:
                    slot=case['slot']
                    if slot in self.state['calls']:
                        if self.state['calls'][slot]['status']!='completed':raise RuntimeError('Unresolved reserved slot: '+slot)
                        continue
                    self.online();self.mode('desktop',case['mode'],slot)
                    sid=self.client.manage('session.select',fresh=True)['session_id']
                    payload={'channel':'desktop','persona_id':self.state['persona_id'],'session_id':sid,'request_id':'m74-real-'+slot,'text':case['text']}
                    self.reserve(slot,'quality',payload)
                    receipt=self.client.submit_input(payload);self.state['calls'][slot]['receipt']=receipt;self.save()
                    deadline=time.monotonic()+180
                    while time.monotonic()<deadline:
                        receipt=self.client.lookup(payload)
                        if receipt and receipt.get('turn_id'):
                            turn=self.client.call('GET','/v1/requests/'+receipt['turn_id'])
                            if turn['status'] not in ('queued','running'):break
                        time.sleep(.3)
                    else:raise RuntimeError('Outcome unknown; only query original receipt')
                    self.record(slot,turn,'desktop')
                    atomic_json(self.out/(case['anonymous']+'-blind.json'),{'anonymous':case['anonymous'],'input':case['text'],'output':turn['reply']})
                    print(json.dumps({'slot':slot,'status':turn['status'],'calls_completed':len(self.state['calls'])}),flush=True)
        finally:self.client.manage('persona.use',persona_id=self.state['original_persona_id'])

    def qq(self):
        """Observe two real platform inputs. No synthetic ingress or automatic resends."""
        self.online();pid=self.state['persona_id']
        with self.store.connection() as db:cutoff=db.execute('SELECT coalesce(max(rowid),0) FROM turns').fetchone()[0]
        for slot in ('QQ01','QQ02'):self.reserve(slot,'actual_qq')
        try:
            with QuotaService(self.store,self.cfg).acceptance():
                self.client.manage('persona.use',persona_id=pid);self.mode('qq','natural_structured','qq-pair')
                atomic_json(self.out/'QQ-ready.json',{'ready':True,'markers':['M74-QQ1','M74-QQ2'],'synthetic':False,'cutoff':cutoff})
                end=time.monotonic()+900;next_health=0;observed={}
                while time.monotonic()<end:
                    if time.monotonic()>next_health:self.online();next_health=time.monotonic()+10
                    with self.store.connection() as db:
                        rows=[dict(r) for r in db.execute("SELECT t.id,e.text FROM turns t JOIN events e ON e.id=t.user_event_id WHERE t.persona_id=? AND t.channel='qq' AND t.rowid>? ORDER BY t.rowid",(pid,cutoff))]
                    if len(rows)>2:raise RuntimeError('Unexpected additional real input; preserve scene and stop')
                    for row in rows:
                        slot='QQ01' if row['text'].startswith('M74-QQ1') else 'QQ02' if row['text'].startswith('M74-QQ2') else None
                        if not slot:raise RuntimeError('Unexpected real QQ marker')
                        if slot in observed:continue
                        turn=self.client.call('GET','/v1/requests/'+row['id'])
                        if turn['status'] in ('queued','running'):continue
                        with self.store.connection() as db:
                            parts=[dict(p) for p in db.execute('SELECT state,attempt,cancel_reason,trigger_event_id,platform_message_id FROM reply_delivery_parts WHERE turn_id=?',(turn['id'],))]
                            raw=[dict(r) for r in db.execute('SELECT channel,bot,peer,platform_message_id FROM raw_events WHERE event_id=?',(turn['user_event_id'],))]
                        if any(p['state'] in ('pending','sending') for p in parts):continue
                        self.record(slot,turn,'qq');atomic_json(self.out/(slot+'-platform.json'),{'raw':raw,'parts':parts,'user_read':'unknown','readback':'not_performed'})
                        if not raw or not raw[0]['platform_message_id']:raise RuntimeError('Missing actual QQ platform source')
                        if not parts or any(p['state'] not in ('sent','skipped') for p in parts):raise RuntimeError('Delivery unresolved')
                        if any(p['state']=='sent' and (p['attempt']!=1 or not p['platform_message_id']) for p in parts):raise RuntimeError('Delivery identity/attempt gate failed')
                        observed[slot]=turn['id']
                    if len(observed)==2:
                        with self.store.connection() as db:
                            a=db.execute('SELECT session_id FROM turns WHERE id=?',(observed['QQ01'],)).fetchone()[0]
                            b=db.execute('SELECT session_id,user_event_id FROM turns WHERE id=?',(observed['QQ02'],)).fetchone()
                            cancelled=db.execute("SELECT count(*) FROM reply_delivery_parts WHERE turn_id=? AND state='skipped' AND cancel_reason='new_input' AND attempt=0 AND trigger_event_id=?",(observed['QQ01'],b[1])).fetchone()[0]
                        result={'same_session':a==b[0],'optional_cancelled_by_second_real_event':cancelled>0,'passed':a==b[0] and cancelled>0}
                        atomic_json(self.out/'QQ-pair-result.json',result)
                        if not result['passed']:raise RuntimeError('Cancellation window missed; keep evidence, do not claim passed or automatically repeat')
                        return
                    time.sleep(.1)
                raise RuntimeError('Real QQ pair incomplete; do not automatically replay')
        finally:self.client.manage('persona.use',persona_id=self.state['original_persona_id'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--phase',choices=['setup','quality','qq'],required=True)
    args=p.parse_args();getattr(Acceptance(args.output),args.phase)()
