"""Explicit, finite acceptance controls. Product owns execution and recovery."""
import argparse
import copy
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

from .runtime import ROOT,EVIDENCE,core_hashes,read,COMMANDS,SYNTHETIC
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.models import generic_persona,identifier,timestamp
from persona_runtime.security import protect,unprotect
from persona_runtime.character_embedding import private_directory

HOME=DEFAULT_HOME/'tmp/pcar-m8e-acceptance/real-1/中文 空格运行'
SDK=DEFAULT_HOME/'components/astrbot/.venv/Scripts/python.exe'


def prepare(resume=False):
    from persona_runtime.lifecycle import ServiceController
    if HOME.exists():
        if not resume or (EVIDENCE/'real-1/owner.json').exists():raise RuntimeError('Acceptance root exists; never replace or replay')
        db=sqlite3.connect(HOME/'data/database/runtime.sqlite3')
        try:
            if db.execute('SELECT count(*) FROM personas').fetchone()[0]:raise RuntimeError('Initialization already has a role; inspect instead of replay')
        finally:db.close()
    if ServiceController(RuntimePaths(DEFAULT_HOME)).has_resident_processes():raise RuntimeError('Formal components must remain stopped')
    private_directory(DEFAULT_HOME,HOME);paths=RuntimePaths(HOME);config=ConfigStore(paths);cfg=config.initialize()
    formal=ConfigStore(RuntimePaths(DEFAULT_HOME)).load();test=read(DEFAULT_HOME/'private/qq-testing.json')
    if test['state']!='inactive' or test['version']!=2:raise RuntimeError('Dedicated test identity not available')
    for key in ('model','organization','character_selector'):cfg[key]=copy.deepcopy(formal[key])
    cfg['organization'].update(hourly_limit=60,daily_limit=240,daily_enabled=False)
    cfg['character_selector']['enabled']=False
    cfg.update(service_port=28765,napcat_port=26399,astrbot_port=26285,onebot_port=26299,
        bot_qq=test['test_bot'],owner_qq=test['test_peer'])
    cfg['runtime']['components']={'service':True,'astrbot':False,'napcat':False}
    atomic_json(config.file,cfg)
    from persona_runtime.storage import Store
    from persona_runtime.service import RuntimeService
    from persona_runtime.chat import ChatEngine
    from persona_runtime.organization import OrganizationEngine
    store=Store(paths);store.initialize();service=RuntimeService(store)
    spec=replace(generic_persona(),name='M8.E 人工验收角色',introduction='仅用于 M8.E 隔离验收，不代表真实用户或私人角色。')
    persona=service.create_persona(spec);pid=persona['id'];service.use_persona(pid);sid=service.create_session()['id']
    cfg['runtime']['qq_persona_id']=pid;atomic_json(config.file,cfg)
    from tests.m71_fixtures import seed_turn
    source='m8e-artificial-I1';day=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    with store.connection(write=True) as db:
        seed_turn(db,pid,sid,source,['人工验收事实：我把蓝色记录本放在书架第二层。'],day=day,reply='已登记这条人工测试事实。')
    engine=ChatEngine(store,config);org=OrganizationEngine(store,config,engine.client,engine)
    policy=org.managed.current_policy(pid)
    org.managed.update_policy(pid,policy['data']|{'memory_automation':True,'memory_backfill':True},policy['id'],pid,'m8e-enable-artificial-memory')
    test.update(state='active',persona_id=pid,original_persona=pid)
    atomic_json(HOME/'private/qq-testing.json',test)
    weights=DEFAULT_HOME/'models/character-embedding'
    if weights.exists():shutil.copytree(weights,HOME/'models/character-embedding')
    os.environ['PCAR_ASTRBOT_PYTHON']=str(SDK)
    from persona_runtime.deployment import prepare as provision
    provision(paths,install=False,source=DEFAULT_HOME/'components/napcat')
    assets=DEFAULT_HOME/'components/astrbot/bot/data/dist'
    if assets.exists():shutil.copytree(assets,HOME/'components/astrbot/bot/data/dist')
    atomic_json(EVIDENCE/'real-1/owner.json',{'home':str(HOME),'instance_id':cfg['instance_id'],'persona_id':pid,
        'source_id':source,'source_kind':'explicit_initialization_artificial_fixture','initialization_only':True,
        'core_hashes':None,'at':timestamp()})
    models=[cfg['model'],cfg['organization']['model']]
    atomic_json(EVIDENCE/'models.dpapi.json',{'data':protect(json.dumps(models))})
    atomic_json(EVIDENCE/'phase.json',{'id':'I1'})
    print(json.dumps({'prepared':True,'home':str(HOME),'schema':24,'source_rounds':1,'model_calls':0}))


def freeze():
    owner=read(EVIDENCE/'real-1/owner.json')
    if list((EVIDENCE/'calls').glob('*.json')):raise RuntimeError('Paid source already frozen')
    owner['core_hashes']=core_hashes();owner['frozen_at']=timestamp();atomic_json(EVIDENCE/'real-1/owner.json',owner)
    with zipfile.ZipFile(EVIDENCE/'source.zip','w',zipfile.ZIP_DEFLATED) as z:
        files=[ROOT/p for p in core_hashes()]+list((ROOT/'plugins/m8e_acceptance').glob('*.py'))
        for p in files:z.write(p,p.relative_to(ROOT))
    atomic_json(EVIDENCE/'registered-positions.json',{'at':timestamp(),'caps':{'background_physical':24,'main_physical':12},
        'I1':{'background':2,'fault':'exit owned service after durable review receipt; Workbench stays alive'},
        'I2':{'main':1,'background':1,'purpose':'tray QQ and explicitly authorized generated greeting'},
        'I3A':{'background':0,'main':0,'synthetic_provider':True,'command':COMMANDS['I3A'],'fault':'PCAR acknowledgement response lost'},
        'I3B':{'background':0,'main':1,'synthetic_provider':True,'command':COMMANDS['I3B'],'fault':'third platform response unknown; then normal chat'},
        'L1':{'model_calls':0,'purpose':'normal close logout/release, reopen and login verification'},
        'P1':{'main':1,'purpose':'formal tray QQ user-visible reply'},
        'N1':{'purpose':'real midnight, maintenance, backlog; all physical calls counted'}})
    print('Frozen product source and finite positions; no model calls')


def launch(formal=False,relogin=False):
    paths=RuntimePaths(DEFAULT_HOME if formal else HOME)
    if not formal and read(EVIDENCE/'real-1/owner.json')['core_hashes']!=core_hashes():raise RuntimeError('Freeze required')
    folder=EVIDENCE/('formal-window' if formal else 'test-window')
    (folder/'close.request').unlink(missing_ok=True)
    from persona_runtime.deployment import child_environment
    env=child_environment(paths);env.update(PCAR_M8E_EVIDENCE=str(EVIDENCE),PCAR_ASTRBOT_PYTHON=str(SDK),
        PCAR_PYTHON=str(DEFAULT_HOME/'.venv/Scripts/python.exe'))
    if relogin:env['PCAR_M8E_REOPEN_LOGIN']='1'
    pythonw=DEFAULT_HOME/'.venv/Scripts/pythonw.exe'
    log=EVIDENCE/('formal-host.log' if formal else 'real-1/window.log');log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('ab') as handle:
        p=subprocess.Popen([str(pythonw),'-B','-c','from plugins.m8e_acceptance.runtime import window;window()'],
            cwd=ROOT,env=env,stdout=handle,stderr=handle,creationflags=subprocess.CREATE_NO_WINDOW)
    atomic_json(folder/'launch.json',{'pid':p.pid,'at':timestamp()});print(json.dumps({'launched_pid':p.pid,'formal':formal}))


def status():
    path=HOME/'data/database/runtime.sqlite3'
    db=sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        result={'tasks':[dict(r) for r in db.execute('SELECT id,mode,status,error_code FROM organization_tasks')],
            'attempts':[dict(r) for r in db.execute('SELECT id,task_id,status FROM organization_attempts')],
            'receipts':db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],
            'active_memories':db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],
            'main_attempts':[dict(r) for r in db.execute('SELECT id,status FROM attempts')],
            'delivery':[dict(r) for r in db.execute('SELECT id,part_index,state,attempt,isolated FROM reply_delivery_parts ORDER BY rowid')]}
    finally:db.close()
    calls=[read(p) for p in (EVIDENCE/'calls').glob('*.json')]
    result['paid_calls']={kind:sum(c['kind']==kind for c in calls) for kind in ('background','main')}
    for name,file in [('window','test-window/ready.json'),('recovery','I1-receipt-crash.json')]:
        result[name]=read(EVIDENCE/file) if (EVIDENCE/file).exists() else None
    # Source hashes are recorded separately, avoid long console output.
    if result['window']:result['window'].pop('core_hashes',None)
    print(json.dumps(result,ensure_ascii=False))


def components(enable):
    from persona_runtime.runtime_control import update
    cfg=ConfigStore(RuntimePaths(HOME)).load()
    result=update(RuntimePaths(HOME),{'astrbot':enable,'napcat':enable},cfg['runtime']['revision'],cfg['instance_id'],identifier())
    print(json.dumps({'components':result['components']}))


def phase(position):
    if position not in ('I1','I2','I3A','I3B','I3B-follow','L1','P1','N1'):raise RuntimeError('Unregistered position')
    cfg=ConfigStore(RuntimePaths(HOME)).load()
    if position in COMMANDS:cfg['model'].update(base_url='http://127.0.0.1:1/v1',name=SYNTHETIC,encrypted_key='')
    else:cfg['model']=read_models()[0]
    # Only the configured local provider changes; no task/receipt/budget state is written.
    atomic_json(ConfigStore(RuntimePaths(HOME)).file,cfg);atomic_json(EVIDENCE/'phase.json',{'id':position,'at':timestamp()})
    print(json.dumps({'phase':position,'model_network_calls_for_fixed_fixture':0 if position in COMMANDS else None},ensure_ascii=False))


def read_models():return json.loads(unprotect(read(EVIDENCE/'models.dpapi.json')['data']))


def observe(formal=False):
    paths=RuntimePaths(DEFAULT_HOME if formal else HOME)
    from persona_runtime.deployment import child_environment
    env=child_environment(paths);env['PCAR_M8E_EVIDENCE']=str(EVIDENCE)
    log=EVIDENCE/('formal-observation' if formal else 'isolated-observation')/'observer.log'
    log.parent.mkdir(parents=True,exist_ok=True)
    launch_file=log.with_name('launch.json')
    from persona_runtime.instance import process_identity_alive,current_process_identity,ProcessIdentityHandle
    if launch_file.exists() and process_identity_alive(read(launch_file)['identity']):raise RuntimeError('Observer already active')
    with log.open('ab') as handle:
        process=subprocess.Popen([str(SDK.with_name('pythonw.exe')),'-B','-m','plugins.m8e_acceptance.observe'],
            cwd=ROOT,env=env,stdout=handle,stderr=handle,creationflags=subprocess.CREATE_NO_WINDOW)
    with_handle=ProcessIdentityHandle(process.pid)
    try:identity=with_handle.identity
    finally:with_handle.close()
    atomic_json(launch_file,{'identity':identity,'at':timestamp(),'read_only':True})
    print('Started read-only observer; it stops when this Workbench exits')


def greeting():
    from persona_runtime.desktop_client import LocalClient
    paths=RuntimePaths(HOME);cfg=ConfigStore(paths).load();client=LocalClient(ConfigStore(paths));pid=read(EVIDENCE/'real-1/owner.json')['persona_id']
    policy=client.manage('proactive.policy.current',persona_id=pid)
    client.manage('proactive.policy.update',persona_id=pid,expected_selected=pid,expected_version=policy['revision'],operation_id='m8e-I2-greeting-policy',data={'enabled':True})
    due=datetime.now(timezone.utc)+timedelta(seconds=20)
    result=client.manage('proactive.rule.update',persona_id=pid,expected_selected=pid,expected_version=0,operation_id='m8e-I2-greeting-rule',
        data={'enabled':True,'kind':'once','mode':'model','content':'用一句自然的中文问候，提醒我休息一下眼睛。不编造共同经历或额外安排。','at':due.isoformat(),'time':'09:00'})
    atomic_json(EVIDENCE/'I2-greeting-rule.json',{'rule_id':result['rule_id'],'at':due.isoformat(),'artificial_role_only':True})
    print(json.dumps({'scheduled':True,'at':due.isoformat()}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','freeze','launch','status','qq-on','phase','greeting','observe'))
    parser.add_argument('--position');parser.add_argument('--formal',action='store_true');parser.add_argument('--resume',action='store_true');parser.add_argument('--relogin',action='store_true');args=parser.parse_args()
    if args.action=='prepare':prepare(args.resume)
    elif args.action=='freeze':freeze()
    elif args.action=='launch':launch(args.formal,args.relogin)
    elif args.action=='status':status()
    elif args.action=='qq-on':components(True)
    elif args.action=='phase':phase(args.position)
    elif args.action=='greeting':greeting()
    elif args.action=='observe':observe(args.formal)
