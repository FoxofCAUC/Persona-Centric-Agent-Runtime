"""Resumable eight-primary-call acceptance; every dispatch is explicitly slotted."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.models import generic_persona
from persona_runtime.quotas import QuotaService


class Acceptance:
    def __init__(self):
        self.paths=RuntimePaths.from_environment();self.root=self.paths.home/'exports/acceptance/M7.5.4-R3-20261029'
        self.out=self.root/'integration';self.out.mkdir(exist_ok=True);self.file=self.out/'state.json'
        self.cfg=ConfigStore(self.paths);self.store=Store(self.paths);self.client=LocalClient(self.cfg)
        self.state=json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else None
    def save(self):atomic_json(self.file,self.state)
    def setup(self):
        if self.state:return
        if not json.loads((self.root/'independent/summary.json').read_text(encoding='utf-8'))['independent_passed']:raise RuntimeError('Quality gate')
        original=self.client.manage('persona.show')['id'];name='M7.5-R3独立验收-20261029'
        from persona_runtime.service import RuntimeService
        existing=[p for p in RuntimeService(self.store).list_personas() if p['name']==name]
        if len(existing)>1:raise RuntimeError('Ambiguous test identity')
        if existing:pid=existing[0]['id']
        else:
            spec=generic_persona().to_dict();spec['name']=name;spec['introduction']='独立人工测试角色，资料为创作者虚构设定。'
            pid=self.client.manage('persona.create',spec=spec)['id']
        self.state={'persona_id':pid,'original_persona_id':original,'calls':{},'created_at':time.time()};self.save()
        self.client.manage('persona.use',persona_id=pid)
        try:
            old=self.client.manage('character.current',persona_id=pid)
            data=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/m754r3_independent.json').read_text(encoding='utf-8'))['characters']['typesetter']
            version=self.client.manage('character.update',persona_id=pid,data=data,expected_version=old['id'],operation_id='r3-real-character')
            self.state['character_version']=version['id']
            self.mode('desktop','hybrid_llm');self.mode('qq','hybrid_llm',batch=True)
            self.prepare_index()
            self.state['setup_complete']=True;self.save()
        finally:self.client.manage('persona.use',persona_id=original)
    def mode(self,channel,mode,batch=False):
        old=self.client.manage('interaction.current',persona_id=self.state['persona_id'],channel=channel)
        requested='natural_batch' if batch else 'legacy_per_message'
        if (old['mode'],old['reply_mode'],old['character_retrieval']['mode'])==(requested,'natural_structured',mode):return
        self.client.manage('interaction.update',persona_id=self.state['persona_id'],channel=channel,mode=requested,reply_mode='natural_structured',context_mode='working_set',cross_session=False,character_retrieval_mode=mode,expected_version=old['id'],operation_id='r3-mode-'+channel+'-'+str(old['revision'])+'-'+mode+'-'+requested)
    def prepare_index(self):
        self.client.manage('character.index-prepare',persona_id=self.state['persona_id'],channel='desktop')
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            value=self.client.manage('character.index-status',persona_id=self.state['persona_id'],channel='desktop')
            if value['cache']['state']=='ready':return
            time.sleep(.2)
        raise RuntimeError('Index not ready')
    def reserve(self,slot,payload):
        if slot in self.state['calls']:raise RuntimeError('Already slotted: reconcile receipt, never replay')
        with self.store.connection() as db:
            main=db.execute('SELECT count(*) FROM attempts a JOIN turns t ON t.id=a.turn_id WHERE t.persona_id=?',(self.state['persona_id'],)).fetchone()[0]
            selection=db.execute('SELECT count(*) FROM character_selection_attempts WHERE persona_id=?',(self.state['persona_id'],)).fetchone()[0]
        if main>=10 or selection>=8:raise RuntimeError('Real integration call ceiling')
        self.state['calls'][slot]={'status':'reserved','payload':payload,'reserved_at':time.time()};self.save()
    def wait(self,payload):
        deadline=time.monotonic()+200;samples=[]
        while time.monotonic()<deadline:
            r=self.client.lookup(payload)
            if r and r.get('turn_id'):
                t=self.client.call('GET','/v1/requests/'+r['turn_id'])
                if t['status'] not in ('queued','running'):return t,samples
                started=time.monotonic();self.client.call('GET','/v1/health');samples.append(round((time.monotonic()-started)*1000,3))
            time.sleep(.3)
        raise RuntimeError('Outcome unknown: query original receipt only')
    def capture(self,slot,turn,samples=()):
        channel=turn['channel'];args={'persona_id':self.state['persona_id'],'channel':channel,'turn_id':turn['id']}
        if channel=='qq':args|={'bot':self.cfg.load()['bot_qq'],'peer':self.cfg.load()['owner_qq']}
        manifests=self.client.manage('context.inspect',**args)['manifests']
        with self.store.connection() as db:
            selected=[{k:v for k,v in dict(r).items() if k!='model_snapshot'} for r in db.execute('SELECT * FROM character_selection_attempts WHERE turn_id=?',(turn['id'],))]
            parts=[{k:v for k,v in dict(r).items() if k!='claim_token'} for r in db.execute('SELECT * FROM reply_delivery_parts WHERE turn_id=? ORDER BY part_index',(turn['id'],))]
            attempts=[dict(r) for r in db.execute('SELECT id,status,started_epoch,error_code,usage_json FROM attempts WHERE turn_id=?',(turn['id'],))]
        atomic_json(self.out/(slot+'.json'),{'turn':turn,'manifests':manifests,'selection_attempts':selected,'primary_attempts':attempts,'parts':parts,'health_samples_ms':list(samples),'amount':'unknown','user_read':'unknown'})
        self.state['calls'][slot].update(status=turn['status'],turn_id=turn['id'],session_id=turn['session_id']);self.save()
        if turn['status']!='completed' or turn['attempt_count']!=1:raise RuntimeError('Primary gate failed; no automatic retry')
        if len([m for m in manifests if m['task_type']=='dialogue'])!=1:raise RuntimeError('Missing main Manifest')
        return {'slot':slot,'status':turn['status'],'selection_calls':len(selected),'primary_calls':len(attempts)}
    def desktop(self):
        if not self.state.get('setup_complete'):raise RuntimeError('Setup first')
        original=self.client.manage('persona.show')['id'];quota=QuotaService(self.store,self.cfg)
        cases=[('R01-lexical','lexical','你平时校对排印稿，会先核对什么，发现漏字怎么标记？'),('R01-v2','hybrid_llm','你平时校对排印稿，会先核对什么，发现漏字怎么标记？'),('R02-lexical','lexical','印刷的事先聊到这里，我得去接孩子，之后有空再聊。'),('R02-v2','hybrid_llm','印刷的事先聊到这里，我得去接孩子，之后有空再聊。')]
        try:
            with quota.acceptance():
                self.client.manage('persona.use',persona_id=self.state['persona_id'])
                for slot,mode,text in cases:
                    if slot in self.state['calls']:
                        if self.state['calls'][slot]['status']!='completed':raise RuntimeError('Unresolved slot '+slot)
                        continue
                    self.mode('desktop',mode)
                    if mode=='hybrid_llm':self.prepare_index()
                    sid=self.client.manage('session.select',fresh=True)['session_id']
                    payload={'channel':'desktop','persona_id':self.state['persona_id'],'session_id':sid,'request_id':'m754r3-'+slot,'text':text}
                    self.reserve(slot,payload);receipt=self.client.submit_input(payload)
                    self.state['calls'][slot]['receipt']=receipt;self.save()
                    t,health=self.wait(payload);print(json.dumps(self.capture(slot,t,health)),flush=True)
        finally:self.client.manage('persona.use',persona_id=original)

    def online(self,required=True):
        from persona_runtime.lifecycle import ServiceController
        state=ServiceController(self.paths).health('astrbot')
        if (required and (state.get('login') is not True or state.get('onebot') is not True)) or (not state.get('owned') or state.get('login') is False or state.get('onebot') is False):
            raise RuntimeError('QQ interrupted or not verified; checkpoint retained')
        return state

    def final_desktop(self):
        from persona_runtime.character_embedding import CharacterEmbeddingProvider
        from persona_runtime.character_retrieval import load_config
        from persona_runtime.character_semantic_index import CharacterSemanticIndex
        original=self.client.manage('persona.show')['id'];quota=QuotaService(self.store,self.cfg)
        try:
            with quota.acceptance():
                self.client.manage('persona.use',persona_id=self.state['persona_id'])
                if 'R05' not in self.state['calls']:
                    self.mode('desktop','hybrid_llm');self.prepare_index()
                    version=self.client.manage('character.current',persona_id=self.state['persona_id'])
                    provider=CharacterEmbeddingProvider(self.paths,self.paths.home/'models/character-embedding'/load_config()['model_fingerprint'])
                    index=CharacterSemanticIndex(self.store,provider);file=index._path(index._spec(version));held=file.with_suffix('.acceptance-held')
                    root=(self.paths.home/'cache/character-semantic/v1').resolve()
                    if not file.resolve().is_relative_to(root) or not held.resolve().is_relative_to(root) or held.exists():raise RuntimeError('Unsafe cache fault target')
                    sid=self.client.manage('session.select',fresh=True)['session_id']
                    payload={'channel':'desktop','persona_id':self.state['persona_id'],'session_id':sid,'request_id':'m754r3-R05','text':'缓存验收：请用一句话确认收到，不需要提及角色背景。'}
                    self.reserve('R05',payload);file.rename(held)
                    try:
                        receipt=self.client.submit_input(payload);self.state['calls']['R05']['receipt']=receipt;self.save()
                        turn,health=self.wait(payload);print(json.dumps(self.capture('R05',turn,health)),flush=True)
                    finally:held.rename(file)
                    result=json.loads((self.out/'R05.json').read_text(encoding='utf-8'))
                    audit=next(m for m in result['manifests'] if m['task_type']=='dialogue')['character_retrieval']
                    if audit['effective_mode']!='lexical' or audit['degrade_reason']!='character_cache_missing' or result['selection_attempts']:raise RuntimeError('Cache degradation gate failed')
                if 'R06' not in self.state['calls']:
                    self.mode('desktop','hybrid_llm',batch=True);self.prepare_index()
                    old=self.client.manage('character.current',persona_id=self.state['persona_id'])
                    sid=self.client.manage('session.select',fresh=True)['session_id']
                    payload={'channel':'desktop','persona_id':self.state['persona_id'],'session_id':sid,'request_id':'m754r3-R06','text':'你当前设定的身高是多少厘米？请只回答已设置的数值。'}
                    self.reserve('R06',payload);receipt=self.client.submit_input(payload)
                    self.state['calls']['R06'].update(receipt=receipt,frozen_character_id=old['id']);self.save()
                    changed=old['data'];changed['appearance']['height_cm']=188
                    new=self.client.manage('character.update',persona_id=self.state['persona_id'],data=changed,expected_version=old['id'],operation_id='r3-real-height188')
                    self.state['calls']['R06']['new_character_id']=new['id'];self.save()
                    turn,health=self.wait(payload);print(json.dumps(self.capture('R06',turn,health)),flush=True)
                    preview=self.client.manage('character.preview',persona_id=self.state['persona_id'],text=payload['text'],version_id=new['id'],channel='desktop',mode='lexical')
                    atomic_json(self.out/'R06-new-version-preview.json',preview)
                    result=json.loads((self.out/'R06.json').read_text(encoding='utf-8'))
                    audit=next(m for m in result['manifests'] if m['task_type']=='dialogue')['character_retrieval']
                    if audit['version_id']!=old['id'] or '172' not in turn['reply'] or '188' not in preview['text']:raise RuntimeError('Frozen character gate failed')
        finally:self.client.manage('persona.use',persona_id=original)

    def arm(self,slot):
        self.online();self.client.manage('persona.use',persona_id=self.state['persona_id'])
        self.mode('qq','hybrid_llm',batch=True);self.prepare_index()
        if slot in self.state['calls']:raise RuntimeError('Slot already armed; inspect before resuming')
        if slot=='R04' and self.state['calls'].get('R03',{}).get('status')!='completed':raise RuntimeError('R03 incomplete')
        with self.store.connection() as db:seq=db.execute('SELECT coalesce(max(seq),0) FROM events').fetchone()[0]
        self.state['calls'][slot]={'status':'awaiting_platform_input','armed_after_seq':seq,'prefix':'M754-'+slot,'armed_at':time.time()};self.save()
        QuotaService(self.store,self.cfg).begin_acceptance()
        print(json.dumps({'armed':slot,'send_only_after_prompt':True}),flush=True)

    def monitor(self,slot):
        record=self.state['calls'][slot]
        if record['status']=='completed':raise RuntimeError('Already completed; never replay')
        quota=QuotaService(self.store,self.cfg);deadline=time.monotonic()+900;last_check=0
        try:
            with quota.acceptance():
                self.online();self.client.manage('persona.use',persona_id=self.state['persona_id'])
                while time.monotonic()<deadline:
                    if time.monotonic()-last_check>5:self.online(required=False);last_check=time.monotonic()
                    with self.store.connection() as db:
                        entries=[dict(r) for r in db.execute('''SELECT e.id,e.seq,e.text,r.id AS receipt_id,r.batch_id,r.accepted_seq,a.platform_message_id
                            FROM events e JOIN ingress_receipts r ON r.event_id=e.id JOIN raw_events a ON a.id=r.raw_event_id
                            WHERE e.persona_id=? AND e.seq>? AND r.channel='qq' ORDER BY e.seq''',(self.state['persona_id'],record['armed_after_seq'])) if r['text'].startswith(record['prefix'])]
                    if entries:
                        atomic_json(self.out/(slot+'-platform-inputs.json'),entries)
                        batches=list(dict.fromkeys(r['batch_id'] for r in entries))
                        if len(batches)>1:raise RuntimeError('QQ inputs did not form one batch; calls retained, no automatic resend')
                        batch=self.client.call('GET','/v2/batches/'+batches[0])
                        if batch.get('turn_id'):
                            turn=self.client.call('GET','/v1/requests/'+batch['turn_id'])
                            if turn['status'] not in ('queued','running'):
                                if slot=='R03' and len(entries)!=3:raise RuntimeError('R03 must have three real members')
                                with self.store.connection() as db:
                                    states=[r[0] for r in db.execute('SELECT state FROM reply_delivery_parts WHERE turn_id=?',(turn['id'],))]
                                if states and all(s=='sent' for s in states):
                                    print(json.dumps(self.capture(slot,turn)),flush=True);return
                                if any(s in ('failed','uncertain','skipped') for s in states):raise RuntimeError('Platform delivery requires disposition; do not replay')
                                if turn['status']!='completed':self.capture(slot,turn)
                    time.sleep(.3)
                raise RuntimeError('Platform wait ended; no replay, resume from receipt')
        finally:
            quota.end_acceptance()
            self.client.manage('persona.use',persona_id=self.state['original_persona_id'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('setup','desktop','final_desktop','arm','monitor'));parser.add_argument('--slot',choices=('R03','R04'));args=parser.parse_args();a=Acceptance()
    if args.action in ('arm','monitor'):
        if not args.slot:parser.error('--slot required')
        getattr(a,args.action)(args.slot)
    else:getattr(a,args.action)()
