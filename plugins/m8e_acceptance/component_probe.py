"""Explicit native-only compatibility probe: no model or message transport."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .runtime import ROOT,EVIDENCE,read,core_hashes
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.lifecycle import ServiceController,read_console,port_open
from persona_runtime.instance import process_identity_alive

ATTEMPT=os.environ.get('PCAR_M8E_COMPONENT_ATTEMPT','v1')
if ATTEMPT not in ('v1','v2'):raise RuntimeError('Unregistered native probe attempt')
HOME=DEFAULT_HOME/('tmp/pcar-m8e-component-probe-'+ATTEMPT)
OUT=EVIDENCE/('component-probe-'+ATTEMPT)
SDK=DEFAULT_HOME/'components/astrbot/.venv/Scripts/python.exe'


def validate():
    paths=RuntimePaths(HOME);cfg=ConfigStore(paths).load();owner=read(OUT/'owner.json')
    if cfg['instance_id']!=owner['instance_id'] or cfg['runtime']['qq_persona_id']!=owner['persona_id']:
        raise RuntimeError('Probe instance changed')
    policy=read(DEFAULT_HOME/'private/qq-testing.json')
    if cfg['bot_qq']!=policy['test_bot'] or cfg['owner_qq']!=policy['test_peer']:
        raise RuntimeError('Dedicated test account mismatch')
    if any(model.get(key) for model in (cfg['model'],cfg['organization']['model'],cfg['character_selector'])
            for key in ('base_url','name','encrypted_key')) or cfg['character_selector']['enabled']:
        raise RuntimeError('Cloud configuration prohibited in native-only probe')
    if cfg['organization']['daily_enabled']:raise RuntimeError('Automatic organization prohibited in probe')
    if cfg['runtime']['components']['service'] or cfg['runtime']['components']['astrbot']:
        raise RuntimeError('Model service and message bridge prohibited in probe')
    if owner['core_hashes']!=core_hashes():raise RuntimeError('Product source changed')
    if hashlib.sha256((HOME/'components/napcat/napcat.mjs').read_bytes()).hexdigest()!=owner['candidate_main_sha256']:
        raise RuntimeError('Probe candidate changed')
    onebot=read(HOME/'components/napcat/config'/('onebot11_'+cfg['bot_qq']+'.json'))
    if any(onebot['network'].values()):raise RuntimeError('Message transport prohibited in probe')
    return paths,cfg,owner


def prepare():
    if HOME.exists():raise RuntimeError('Fresh probe root required; never overwrite')
    formal=ConfigStore(RuntimePaths(DEFAULT_HOME)).load()
    if formal['runtime']['components']['napcat'] or ServiceController(RuntimePaths(DEFAULT_HOME)).owned('napcat'):
        raise RuntimeError('Formal QQ must stay paused')
    candidate=read(EVIDENCE/'component-preview-ready.json');source=Path(candidate['bundle'])
    if hashlib.sha256((source/'napcat.mjs').read_bytes()).hexdigest()!=candidate['main_sha256']:
        raise RuntimeError('Candidate source changed')
    ports=(38765,36399,36285,36299) if ATTEMPT=='v1' else (48765,46399,46285,46299)
    for port in ports:
        if port_open(port):raise RuntimeError('Probe port occupied; no takeover')
    from persona_runtime.character_embedding import private_directory
    private_directory(DEFAULT_HOME,HOME);paths=RuntimePaths(HOME);config=ConfigStore(paths);cfg=config.initialize()
    policy=read(DEFAULT_HOME/'private/qq-testing.json')
    cfg.update(service_port=ports[0],napcat_port=ports[1],astrbot_port=ports[2],onebot_port=ports[3],
        bot_qq=policy['test_bot'],owner_qq=policy['test_peer'])
    cfg['organization']['daily_enabled']=False
    cfg['runtime']['components']={'service':False,'astrbot':False,'napcat':False}
    atomic_json(config.file,cfg)
    from persona_runtime.storage import Store
    from persona_runtime.service import RuntimeService
    from persona_runtime.models import generic_persona
    store=Store(paths);store.initialize();service=RuntimeService(store)
    role=service.create_persona(replace(generic_persona(),name='M8.E 原生组件隔离角色',
        introduction='人工初始化，零聊天、零模型调用；仅核对 QQ 组件路径和生命周期。'))
    service.use_persona(role['id']);cfg=config.load();cfg['runtime']['qq_persona_id']=role['id'];atomic_json(config.file,cfg)
    from persona_runtime.deployment import prepare as provision,render_components
    os.environ['PCAR_ASTRBOT_PYTHON']=str(SDK)
    provision(paths,install=False,source=source);render_components(paths)
    file=HOME/'components/napcat/config'/('onebot11_'+cfg['bot_qq']+'.json');onebot=read(file)
    onebot['network']={key:[] for key in onebot['network']};atomic_json(file,onebot)
    private_directory(HOME,HOME/'data/qq')
    atomic_json(OUT/'owner.json',{'home':str(HOME),'instance_id':cfg['instance_id'],'persona_id':role['id'],
        'core_hashes':core_hashes(),'candidate_main_sha256':candidate['main_sha256'],'model_calls':0,'qq_messages':0})
    validate();print(json.dumps({'prepared':True,'new_home':str(HOME),'model_calls':0,'qq_messages':0}))


def launch():
    paths,cfg,owner=validate()
    if ServiceController(paths).has_resident_processes():raise RuntimeError('Probe already running')
    from persona_runtime.deployment import child_environment
    env=child_environment(paths);env.update(PCAR_PYTHON=str(DEFAULT_HOME/'.venv/Scripts/python.exe'),
        PCAR_ASTRBOT_PYTHON=str(SDK),NAPCAT_QQ_DATA_DIR=str(HOME/'data/qq'),
        NAPCAT_WORKDIR=str(HOME/'components/napcat'),PCAR_M8E_COMPONENT_ATTEMPT=ATTEMPT)
    with (OUT/'workbench.log').open('ab') as log:
        process=subprocess.Popen([str(DEFAULT_HOME/'.venv/Scripts/pythonw.exe'),'-B','-m',
            'plugins.m8e_acceptance.component_probe','window'],cwd=ROOT,env=env,stdout=log,stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW)
    atomic_json(OUT/'launch.json',{'pid':process.pid,'at':time.time()})
    print(json.dumps({'launched':True,'formal_qq_unchanged':True,'model_calls':0}))


def window():
    paths,cfg,owner=validate()
    if (Path(os.environ.get('NAPCAT_QQ_DATA_DIR','')).resolve()!=(HOME/'data/qq').resolve()
            or Path(os.environ.get('NAPCAT_WORKDIR','')).resolve()!=(HOME/'components/napcat').resolve()):
        raise RuntimeError('Independent QQ data/work roots required')
    from persona_runtime import desktop
    base=desktop.Workbench
    class ProbeWindow(base):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.title('PCAR · 原生组件隔离 '+ATTEMPT+' · 测试Bot尾号 '+cfg['bot_qq'][-4:]+' · 零聊天')
            for button,_ in self.buttons:
                button.companion_disabled=True;button.configure(state='disabled')
            self.notice.set('仅专用测试 Bot（尾号'+cfg['bot_qq'][-4:]+'）；零模型、零消息。请先核对手机账号。')
            self.after(800,lambda:self.run_task('启动原生隔离组件',{'lifecycle'},lambda _:self.controller.start('napcat')))
            self.after(1200,self.capture)
        def capture(self):
            if self.closed:return
            try:
                native=read(HOME/'runtime/application.json').get('states',{}).get('napcat',{})
                if native.get('account_matches') is False or native.get('login_reason')=='session_expired':
                    atomic_json(OUT/'scope-auto-stop.json',{'at':time.time(),
                        'reason':native.get('login_reason'),'model_calls':0,'qq_messages':0})
                    self.close_window();return
            except (OSError,ValueError):pass
            atomic_json(OUT/'window.json',{'at':time.time(),'pid':os.getpid(),'exists':bool(self.winfo_exists()),
                'tray_active':bool(self.tray and self.tray.active),'withdrawn':self.state()=='withdrawn'})
            self.after(2000,self.capture)
    desktop.Workbench=ProbeWindow;desktop.run(paths)


def status():
    paths,cfg,owner=validate();controller=ServiceController(paths);lease=read_console(paths)
    state=controller.health('napcat')
    from persona_runtime.storage import Store
    with Store(paths).connection() as db:
        counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]
            for table in ('turns','attempts','organization_tasks','reply_delivery_parts')}
    assert not any(counts.values())
    report={'at':time.time(),'native':state,'workbench_alive':bool(lease and process_identity_alive(lease['process'])),
        'message_transport_disabled':True,'model_service_disabled':True,'business_counts':counts,
        'qq_data_directory':str(HOME/'data/qq'),'qq_data_entries':sum(1 for _ in (HOME/'data/qq').rglob('*'))}
    atomic_json(OUT/'status.json',report);print(json.dumps(report,ensure_ascii=False))


def observe():
    """One authorized session, up to two active hours, then normal app exit."""
    from .processes import owned_processes
    from persona_runtime.runtime_control import exit_application
    from persona_runtime.models import RuntimeErrorBase
    paths,cfg,owner=validate();controller=ServiceController(paths);lease=read_console(paths)
    if not lease or not process_identity_alive(lease['process']):raise RuntimeError('Probe workbench not alive')
    identity=lease['process'];initial=controller.health('napcat')
    if not initial.get('account_logged_in') or not initial.get('account_matches'):
        raise RuntimeError('Dedicated test Bot login not confirmed; no retry')
    from persona_runtime.qq_session import session_request
    capability=session_request(paths,controller.record('napcat'),'capability')
    if not (capability.get('offline') or (capability.get('sessionOffline') and capability.get('capturedSessionConfig'))):
        raise RuntimeError('Scoped logout unavailable; stop for review')
    atomic_json(OUT/'V2-result.json',{'at':time.time(),'passed':True,'account_matches':True,
        'native_session_initialized':bool(capability.get('capturedSessionConfig')),'model_calls':0,'qq_messages':0})
    start=last=time.time();active=0.;gaps=[];unknown=0;success=False;reason=None
    deadline=time.monotonic()+8*3600
    while process_identity_alive(identity) and time.monotonic()<deadline:
        now=time.time();gap=now-last;last=now
        if gap>25:gaps.append({'at':now,'seconds':gap})
        validate();state=controller.health('napcat')
        if not controller.owned('napcat'):
            reason='native_component_stopped';break
        if state.get('account_logged_in') is False:
            reason=state.get('login_reason','login_required');break
        if state.get('account_logged_in') is None:
            unknown+=1
            if unknown>=3:reason='native_login_status_unconfirmed';break
        else:
            unknown=0
            if gap<=25:active+=gap
        report={'at':now,'started':start,'active_observed_seconds':active,'gaps':gaps,'native':state,
            'model_calls':0,'qq_messages':0,'formal_promotion_authorized':False}
        atomic_json(OUT/'progress.json',report)
        if active>=7200:success=True;break
        time.sleep(10)
    if not success and reason is None:reason='workbench_exited_or_observation_window_ended'
    atomic_json(OUT/'V4-result.json',{'at':time.time(),'passed':success,'reason':reason,'active_observed_seconds':active,
        'sleep_or_observation_gaps':gaps,'model_calls':0,'qq_messages':0,'replaces_formal_N1':False})
    # Same session: observe before normal logout, avoiding an extra scan.
    processes=owned_processes(paths,identity) if process_identity_alive(identity) else []
    retained=[]
    from persona_runtime.instance import ProcessIdentityHandle
    for process in processes:
        try:
            handle=ProcessIdentityHandle(process.pid)
            try:retained.append(handle.identity)
            finally:handle.close()
        except RuntimeErrorBase:
            if process.is_running():raise
    before_exit=controller.health('napcat') if process_identity_alive(identity) else {}
    online_before_exit=bool(before_exit.get('account_logged_in') and before_exit.get('account_matches'))
    if process_identity_alive(identity):
        exit_application(paths,cfg['instance_id'],'m8e-component-probe-V3-close')
        limit=time.monotonic()+55
        while process_identity_alive(identity) and time.monotonic()<limit:time.sleep(.5)
    exit=read(HOME/'runtime/last-exit.json')
    released=(not process_identity_alive(identity) and not controller.has_resident_processes()
        and all(not process_identity_alive(item) for item in retained))
    exit_passed=bool(online_before_exit and released and exit.get('console_token')==lease['token'] and exit.get('logout_confirmed'))
    passed=success and exit_passed
    atomic_json(OUT/'V3-result.json',{'at':time.time(),'passed':exit_passed,'processes_released':released,
        'exit':exit,'online_before_exit':online_before_exit,'model_calls':0,'qq_messages':0})
    with __import__('persona_runtime.storage',fromlist=['Store']).Store(paths).connection() as db:
        counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0]
            for table in ('turns','attempts','organization_tasks','reply_delivery_parts')}
    assert not any(counts.values())
    atomic_json(OUT/'result.json',{'at':time.time(),'passed':passed,'native_stability_passed':success,
        'exit_passed':exit_passed,'business_counts':counts,'model_calls':0,'qq_messages':0,'formal_N1_passed':False,
        'formal_component_replaced':False,'reason':reason})
    print(json.dumps({'isolated_probe_passed':passed,'stability':success,'exit_released':released,
        'model_calls':0,'qq_messages':0,'formal_N1_passed':False}))


def guarded_observe():
    paths,cfg,_=validate();lease=read_console(paths)
    try:observe()
    except Exception as error:
        atomic_json(OUT/'observer-error.json',{'at':time.time(),'error_type':type(error).__name__,
            'probe_passed':False,'model_calls':0,'qq_messages':0})
        current=read_console(paths)
        if lease and current and current['token']==lease['token'] and process_identity_alive(lease['process']):
            from persona_runtime.runtime_control import exit_application
            exit_application(paths,cfg['instance_id'],'m8e-component-probe-error-close')
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','launch','window','status','observe'));args=parser.parse_args()
    {'prepare':prepare,'launch':launch,'window':window,'status':status,'observe':guarded_observe}[args.action]()
