"""Three genuine QQ positions on the dedicated test Bot, artificial persona only."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import socket
import time

from experiments import companion_qq_acceptance as transport
from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.deployment import prepare as components
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import generic_persona, identifier, timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths
from persona_runtime.private_personas import safe
from persona_runtime.qq_testing import QQTesting
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .runner import code_hashes, read, write_new, require, sha

VERSION='stage7-artificial-qq-v1'
ROOT=DEFAULT_HOME/'tmp/pcar-release-qq'


def cases():
    values=transport.cases()
    return [values[0],values[1],values[3]|{'id':'Q03'}]


def position(out):
    return next((c for c in cases() if not (Path(out)/'positions'/(c['id']+'.json')).exists()),None)


def prepare(out):
    out=Path(out).resolve();safe(out,DEFAULT_HOME/'exports/acceptance');require(not out.exists(),'qq_already_prepared')
    private_directory(DEFAULT_HOME,out)
    paths=RuntimePaths(ROOT/identifier());private_directory(DEFAULT_HOME,paths.home)
    store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize()
    source=ConfigStore(RuntimePaths(DEFAULT_HOME)).load();test=QQTesting(RuntimePaths(DEFAULT_HOME)).load(True)
    require(test['state']=='inactive' and test['daily_bot']!=test['test_bot'],'qq_identity')
    value=cfg.load();value['model']=deepcopy(source['model']);value['organization']['daily_enabled']=False
    value.update(bot_qq=test['daily_bot'],owner_qq=test['daily_peer'])
    sockets=[]
    try:
        for key in ('service_port','napcat_port','astrbot_port','onebot_port'):
            sock=socket.socket();sock.bind(('127.0.0.1',0));sockets.append(sock);value[key]=sock.getsockname()[1]
    finally:
        for sock in sockets:sock.close()
    atomic_json(cfg.file,value)
    service=RuntimeService(store);pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
    testing=QQTesting(paths);testing.configure(test['daily_peer'],test['test_peer'],daily_bot=test['daily_bot'],test_bot=test['test_bot']);testing.enter(pid)
    write_new(out/'preparing.json',{'home':str(paths.home),'version':VERSION,'artificial_only':True})
    shutil.copytree(DEFAULT_HOME/'components/astrbot/.venv',paths.home/'components/astrbot/.venv',ignore=shutil.ignore_patterns('__pycache__'))
    components(paths,install=False,source=DEFAULT_HOME/'components/napcat');transport.stage_launcher(paths)
    protected={str(p):sha(p) for folder in ('config','private','data/database') for p in (DEFAULT_HOME/folder).rglob('*') if p.is_file() and not p.name.endswith(('-shm','.lock'))}
    owner={'version':VERSION,'home':str(paths.home),'persona_id':pid,'private':False,'real':True,
        'artificial_only':True,'cases':cases(),'code_hashes':code_hashes(),'config_hash':fingerprint(cfg.load()),'protected':protected}
    write_new(paths.home/'release-qq-owner.json',{'version':VERSION,'out':str(out),'persona_id':pid})
    write_new(out/'owner.json',owner)
    return {'prepared':True,'positions':3,'source_copied':False}


def verify(out):
    out=Path(out).resolve();owner=read(out/'owner.json');home=safe(owner['home'],ROOT)
    require(home.parent==ROOT and owner['version']==VERSION,'qq_scope')
    require(read(home/'release-qq-owner.json')=={'version':VERSION,'out':str(out),'persona_id':owner['persona_id']},'qq_owner')
    require(owner['code_hashes']==code_hashes() and owner['cases']==cases(),'qq_source_changed')
    require(all(Path(p).is_file() and sha(p)==v for p,v in owner['protected'].items()),'qq_formal_changed')
    paths=RuntimePaths(home);require(fingerprint(ConfigStore(paths).load())==owner['config_hash'],'qq_config_changed')
    QQTesting(paths).require_ready()
    with Store(paths).connection() as db:require(Store.current(db)==owner['persona_id'],'qq_persona_changed')
    return owner,paths


def listen(out):
    out=Path(out).resolve();owner,paths=verify(out)
    proof=read(out.parent/'desktop-real-v1/summary.json')
    require(proof['passed'] and not proof['simulated'],'desktop_real_required')
    write_new(out/'started.json',{'pid':os.getpid(),'at':timestamp()})
    controller=ServiceController(paths);runtime=transport.Runtime(out,verifier=verify,position=position)
    require(not controller.has_resident_processes(),'qq_existing_processes')
    controller.start_console()
    try:
        runtime.open()
        for role in ('astrbot','napcat'):controller.start(role)
        while position(out) is not None and not (out/'stop.request').exists():
            verify(out)
            if runtime.recorder.blocked:raise RuntimeError('QQ verification stopped; inspect saved evidence')
            health=paths.home/'runtime/astrbot-health.json'
            h=read(health) if health.exists() else {}
            online=bool(h.get('verified') and h.get('online') and time.time()-h.get('at',0)<15)
            atomic_json(out/'listening.json',{'online':online,'next':position(out)['id'],'at':timestamp(),'home':str(paths.home)})
            time.sleep(1)
        if position(out) is None:
            rows=[read(out/'positions'/(c['id']+'.json')) for c in cases()]
            require(all(r['coverage_passed'] for r in rows),'qq_coverage')
            write_new(out/'summary.json',{'positions':rows,'passed':True,'user_visible':'pending','code_hashes':code_hashes()})
    finally:
        # The existing controller only stops its recorded processes.
        controller.stop_all(include_qq=True);runtime.close();controller.end_console()
        write_new(out/'closed.json',{'at':timestamp(),'resident':controller.has_resident_processes()})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','listen'));p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    if args.action=='prepare':print(json.dumps(prepare(args.out)))
    else:listen(args.out)
