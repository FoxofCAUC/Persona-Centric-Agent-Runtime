"""Finite artificial release checks through the normal API and hidden Workbench."""
import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path

from plugins.background_acceptance import runner as host
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.character_embedding import private_directory
from persona_runtime.chat import ChatCompletionsClient
from persona_runtime.companion import choose
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import InstanceLock
from persona_runtime.models import generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.persona_management import PersonaManagement
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

VERSION='stage7-release-acceptance-v1'
ROOT=host.ROOT
TEMP=DEFAULT_HOME/'tmp/pcar-release-acceptance'
read,write_new,require,sha=host.read,host.write_new,host.require,host.sha
POSITIONS=[
    {'id':'D01','role':'created','text':'今天沿着河边散步，看见树叶落在水里，心情放松了些。'},
    {'id':'D02','role':'imported','text':'请写一个完整的 Python 函数，按日期汇总 CSV 中的金额，使用 Decimal，并给出输入和运行示例。'},
    {'id':'D03','role':'cloned','text':'更正我的人工设定：我现在的储物柜编号改为海棠七十三，不再是榆树二十六。请记住新编号，以这次为准。'},
    {'id':'D04','role':'cloned','text':'重新打开了。我的储物柜最新编号是什么？'},
    {'id':'D05','role':'created','text':'请写一个完整的 Python 命令行 CSV 校验工具，包含参数解析、完整代码、错误报告、二十种边界处理和运行示例。','cancel':True},
]


def code_hashes():
    return host.code_hashes()|{p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'plugins/release_acceptance').glob('*.py')}|{'experiments/companion_qq_acceptance.py':sha(ROOT/'experiments/companion_qq_acceptance.py')}


def verify(out):
    out=Path(out).resolve();owner=read(out/'owner.json');plan=read(out/'plan.json')
    home=Path(owner['home']).resolve()
    require(home.parent==TEMP.resolve(),'release_scope')
    require(read(home/'ownership.json')=={'version':VERSION,'out':str(out),'artificial_only':True},'release_owner')
    require(owner['code_hashes']==code_hashes() and owner['plan_hash']==fingerprint(plan),'release_freeze')
    require(all(Path(p).is_file() and sha(p)==h for p,h in owner['protected'].items()),'formal_changed')
    cfg=ConfigStore(RuntimePaths(home)).load()
    require(not cfg['bot_qq'] and not cfg['owner_qq'],'desktop_only')
    require(fingerprint(cfg['model'])==owner['model_hash'],'model_changed')
    return owner,plan


def prepare(out,simulated=False):
    out=Path(out).resolve()
    from persona_runtime.private_personas import safe
    safe(out,DEFAULT_HOME/'exports/acceptance')
    require(not out.exists(),'already_prepared')
    private_directory(DEFAULT_HOME,out)
    home=TEMP/identifier();private_directory(DEFAULT_HOME,home)
    paths=RuntimePaths(home);store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
    original=ConfigStore(RuntimePaths(DEFAULT_HOME)).load();value=cfg.load()
    require(original['model']['name']=='deepseek-flash' and original['model']['base_url'].rstrip('/') in ('https://api.deepseek.com','https://api.deepseek.com/v1'),'unapproved_provider')
    value['model']=deepcopy(original['model'])
    value['organization']=deepcopy(original['organization']);value['organization']['daily_enabled']=False
    # Only these six positions are admitted by the recording client.
    if simulated:
        value['model']['encrypted_key']=''
        value['organization']['model']['encrypted_key']=''
    atomic_json(cfg.file,value)
    service=RuntimeService(store);created=service.create_persona(generic_persona())['id'];service.use_persona(created)
    characters=CharacterService(store)
    package={'format':'pcar_character_v1','persona_spec':service.show_persona(created)['spec'],'character':characters.current(created)['data']}
    imported=characters.import_package(created,package,identifier())['id']
    management=PersonaManagement(store)
    cloned=management.clone(created,management.inspect(created)['versions'],created,identifier())['id']
    roles={'created':created,'imported':imported,'cloned':cloned}
    service.use_persona(cloned);session=service.create_session()
    event=service.append_event(session['id'],'user','人工设定：我的储物柜编号是榆树二十六。')
    memory=service.add_memory('user_fact','用户的储物柜编号是榆树二十六。',event['id'])
    service.use_persona(created)
    protected={str(p):sha(p) for folder in ('config','private','data/database') for p in (DEFAULT_HOME/folder).rglob('*') if p.is_file() and not p.name.endswith(('-shm','.lock'))}
    plan={'version':VERSION,'positions':POSITIONS,'simulated':simulated,'local_embeddings':False,
        'planned_main_replies':5,'planned_organization':1,'restart_after':'M01','no_outer_retry':True}
    write_new(home/'ownership.json',{'version':VERSION,'out':str(out),'artificial_only':True})
    write_new(out/'plan.json',plan)
    write_new(out/'owner.json',{'home':str(home),'persona_id':created,'roles':roles,'seed_memory_id':memory['id'],
        'code_hashes':code_hashes(),'plan_hash':fingerprint(plan),'protected':protected,'model_hash':fingerprint(value['model'])})
    return {'prepared':True,'simulated':simulated}


class RecordedClient:
    def __init__(self,out,simulated):self.out=Path(out);self.simulated=simulated;self.inner=ChatCompletionsClient()
    async def complete(self,model,messages):
        owner,plan=verify(self.out);active=read(self.out/'active.json');key=active['id']
        require(key in [p['id'] for p in POSITIONS]+['M01'],'unexpected_position')
        require(not (self.out/'results'/(key+'.json')).exists(),'no_success_replay')
        calls=list((self.out/'calls'/key).glob('*.json'))
        require(len(calls)<(1 if key=='M01' else 4),'retry_limit')
        require(all(read(p)['status']=='failed' for p in calls),'no_unknown_replay')
        path=self.out/'calls'/key/(identifier()+'.json')
        row={'position':key,'at':timestamp(),'status':'reserved','messages_hash':fingerprint(messages),
            'wire_hash':fingerprint(build_request(model,messages)[0]),'messages':messages}
        write_new(path,row)
        try:
            if self.simulated:
                if key=='D05':await asyncio.sleep(60)
                if key=='M01':
                    payload=json.loads(messages[-1]['content']);event=next(e for e in payload['events'] if e['role']=='user')
                    answer=json.dumps({'format_version':1,'summary':[], 'candidates':[{'category':'user_fact','text':'用户的储物柜编号是海棠七十三。','sources':[{'event_id':event['id'],'quote':event['text']}],'conflicts':[owner['seed_memory_id']]}]},ensure_ascii=False)
                else:answer='人工模拟回复：海棠七十三。'
                result=(answer,{'total_tokens':1,'mock':True})
            else:result=await self.inner.complete(model,messages)
            row.update(status='completed',reply=result[0],usage=result[1]);atomic_json(path,row)
            return result
        except BaseException as error:
            row.update(status='cancelled' if isinstance(error,asyncio.CancelledError) else 'failed',error=type(error).__name__,usage=getattr(error,'usage',None))
            atomic_json(path,row);raise


class Observer:
    def __init__(self,out):self.out=Path(out)
    def get(self,*args):return None
    def before_dispatch(self,engine,turn,prepared):
        owner,plan=verify(self.out);active=read(self.out/'active.json')
        position=next(p for p in POSITIONS if p['id']==active['id'])
        require(turn['persona_id']==owner['roles'][position['role']],'wrong_persona')
        from persona_runtime.companion import read as profile
        require(profile(turn)==choose('minimal_im'),'default_not_v8')
        require(engine.dialogue_pipeline is None,'experimental_pipeline')
        atomic_json(self.out/'requests'/(active['id']+'.json'),{'turn_id':turn['id'],'manifest':prepared['manifest'],'messages_hash':fingerprint(prepared['messages'])})


def organize(client,out,owner,session):
    atomic_json(out/'active.json',{'id':'M01'})
    preview=client.manage('organization.preview',session_id=session,mode='candidates')
    require(len(preview['rows'])==1,'unexpected_organization_scope')
    intent={'request_id':identifier(),'preview':preview};write_new(out/'intents/M01.json',intent)
    task=client.manage('organization.submit',**intent)
    def done():
        value=client.manage('organization.show',task_id=task['id'])
        return value if value['status'] not in ('queued','running') else None
    result=host.wait_until(done,1200);write_new(out/'results/M01.json',result)
    require(result['status']=='completed','organization_failed')
    candidates=[client.manage('candidate.show',candidate_id=c['id']) for c in client.manage('candidate.list',status='pending')]
    matches=[c for c in candidates if c['conflicts']==[owner['seed_memory_id']] and '海棠七十三' in c['text'] and any('更正' in e['text'] and e['role']=='user' for e in c['evidence'])]
    write_new(out/'pending-candidates.json',candidates)
    require(len(matches)==1,'correction_candidate_missing_or_ambiguous')
    old=client.manage('memory.show',memory_id=owner['seed_memory_id'])
    args={'candidate_id':matches[0]['id'],'operation_id':identifier(),'target_memory_id':old['id'],'expected_revision':old['revision']}
    write_new(out/'review-intent.json',args)
    accepted=client.manage('candidate.accept',**args)
    write_new(out/'review-result.json',accepted)
    require(client.manage('memory.show',memory_id=old['id'])['status']=='superseded','old_memory_still_active')


def execute(out):
    out=Path(out).resolve();owner,plan=verify(out);paths=RuntimePaths(Path(owner['home']))
    if not plan['simulated']:
        proofs=[read(p) for p in out.parent.glob('desktop-simulation-*/summary.json')]
        require(any(p['passed'] and p['simulated'] and p['code_hashes']==code_hashes() for p in proofs),'offline_release_required')
    with closing(InstanceLock(out/'supervisor.lock')):
        write_new(out/'started.json',{'at':timestamp()})
        process=None;opening=1
        try:
            process,ready=host.spawn_worker(out,opening,module='plugins.release_acceptance')
            client=LocalClient(ConfigStore(paths))
            for position in POSITIONS:
                key=position['id'];pid=owner['roles'][position['role']]
                verify(out);client.manage('persona.use',persona_id=pid)
                for channel in ('desktop','qq'):
                    policy=client.manage('interaction.current',persona_id=pid,channel=channel)
                    require(policy.get('companion')==choose('minimal_im') and policy['delivery']['source']=='default','new_identity_default')
                atomic_json(out/'active.json',{'id':key})
                payload={'channel':'desktop','persona_id':pid,'request_id':identifier(),'text':position['text']}
                write_new(out/'intents'/(key+'.json'),payload)
                receipt=client.submit_input(payload)
                batch=client.call('GET','/v2/batches/'+receipt['batch_id'])
                if batch['state']=='collecting':client.call('POST','/v2/batches/'+batch['id']+'/seal',{'persona_id':pid,'expected_revision':batch['revision'],'operation_id':identifier()})
                def turn():
                    b=client.call('GET','/v2/batches/'+batch['id'])
                    return client.call('GET','/v1/requests/'+b['turn_id']) if b.get('turn_id') else None
                if position.get('cancel'):
                    host.wait_until(lambda: True if list((out/'calls'/key).glob('*.json')) else None)
                    client.cancel_request(turn())
                def done():
                    t=turn();return t if t and t['status'] not in ('queued','running') else None
                result=host.wait_until(done,1500);write_new(out/'results'/(key+'.json'),result)
                require(result['status']==('interrupted' if position.get('cancel') else 'completed'),'unexpected_chat_result')
                if position.get('cancel'):require(result['error_code']=='cancelled_by_user','cancel_not_confirmed')
                if key=='D03':
                    organize(client,out,owner,result['session_id'])
                    before=host.close_worker(out,opening,process,paths);process=None;opening+=1
                    process,ready=host.spawn_worker(out,opening,module='plugins.release_acceptance')
                    require(ready['snapshot']==before,'restart_changed_history')
                if key=='D04':require('海棠七十三' in result['reply'],'recall_failed')
            write_new(out/'summary.json',{'desktop_positions':5,'organization':1,'restart':1,'simulated':plan['simulated'],'passed':True,'code_hashes':code_hashes()})
        finally:
            if process is not None and process.poll() is None:host.close_worker(out,opening,process,paths)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run','worker'));p.add_argument('--out',type=Path,required=True);p.add_argument('--simulated',action='store_true');p.add_argument('--opening',type=int,default=1);args=p.parse_args()
    if args.action=='prepare':print(json.dumps(prepare(args.out,args.simulated)))
    elif args.action=='run':execute(args.out)
    else:host.worker(args.out,args.opening,verifier=verify,client_factory=RecordedClient,observer_factory=Observer)
