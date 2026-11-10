"""Workbench-owned recovery and maintenance. Never installs or survives the app."""
from collections import deque
import json
import threading
import time

from .config import atomic_json
from .models import timestamp

BACKOFF=(2,5,15,30,60)


class AppRuntime:
    def __init__(self,controller,*,clock=time.time):
        self.controller=controller;self.paths=controller.paths;self.clock=clock
        self.finished=threading.Event();self.thread=None;self.failures={};self.next={};self.last_maintenance=0
        self.file=self.paths.home/'runtime/application.json'
        self.operation=None;self.operation_lock=threading.Lock();self.last_errors={}
        self.expected_running={}
        try:
            saved=json.loads(self.file.read_text(encoding='utf-8'));self.restarts=saved.get('restarts',{})
            self.next=saved.get('next',{}) or {role:max(history)+BACKOFF[min(len(history)-1,4)] for role,history in self.restarts.items() if history}
            self.last_errors=saved.get('errors',{})
        except (OSError,ValueError):self.restarts={}

    def persist(self,states):
        atomic_json(self.file,{'at':timestamp(),'restarts':self.restarts,'next':self.next,
            'errors':dict(self.last_errors),'states':states,'owner':'workbench'})

    def start(self):
        self.thread=threading.Thread(target=self.run,name='workbench-runtime',daemon=True);self.thread.start()

    def stop(self):self.finished.set()

    def ready(self,role,state):
        # Account/session availability is independent of the bridge process.
        # Restarting a healthy bridge cannot repair a platform-expired login.
        return state['state']=='运行' or role=='astrbot' and self.controller._start_ready(role,state)

    def launch_operation(self,role,start):
        with self.operation_lock:
            if self.operation or self.finished.is_set() or self.controller.closing.is_set():return False
            self.operation={'role':role,'action':'start' if start else 'stop'}
        def work():
            try:
                with self.controller.lifecycle_lock:
                    if self.finished.is_set() or self.controller.closing.is_set():return
                    if start:
                        state=self.controller.health(role)
                        if self.ready(role,state):return
                        if self.controller.owned(role):self.controller.release_role(role)
                        self.controller.start(role,automatic=True)
                    else:self.controller.stop(role)
                self.last_errors.pop(role,None)
            except Exception as error:
                self.last_errors[role]=type(error).__name__
                try:self.persist({})
                except OSError:pass
            finally:
                with self.operation_lock:self.operation=None
        threading.Thread(target=work,name='owned-component-'+role,daemon=True).start()
        return True

    def run(self):
        while not self.finished.is_set() and not self.controller.closing.is_set():
            try:self.tick()
            except Exception as error:
                atomic_json(self.paths.home/'runtime/application-error.json',{'at':timestamp(),'reason':type(error).__name__})
            self.finished.wait(5)

    def tick(self):
        now=self.clock();self.controller.ensure_binding();cfg=self.controller.config.load();states={}
        for role,enabled in cfg['runtime']['components'].items():
            if self.finished.is_set() or self.controller.closing.is_set():return
            if role == 'napcat' and cfg.get('qq_backend', 'napcat') == 'snowluma':
                states[role] = self.controller.health(role)
                continue
            if role!='service' and not (cfg['owner_qq'] and cfg['bot_qq']):enabled=False
            if role!='service' and not cfg['runtime']['qq_persona_id']:enabled=False
            if role=='napcat' and (self.controller.console_lease or {}).get('login_confirmation_required'):
                states[role]=self.controller.health(role);continue
            state=self.controller.health(role);states[role]=state
            if not enabled:
                self.expected_running[role]=False
                if self.controller.owned(role):self.launch_operation(role,False)
                continue
            if state['state']=='外部程序占用':continue
            if self.ready(role,state):self.failures[role]=0;self.expected_running[role]=True;continue
            self.failures[role]=self.failures.get(role,0)+1
            initial=not self.expected_running.get(role,False)
            if self.failures[role]<3 and not (initial and state['state']=='已停止'):continue
            history=[v for v in self.restarts.get(role,[]) if now-v<1800]
            self.restarts[role]=history
            if len(history)>=5:
                state['recovery_reason']='restart_limit_requires_attention';continue
            if now<self.next.get(role,0):continue
            if self.operation:
                state['recovery_reason']='component_operation_in_progress';continue
            if role!='service' and not self.controller._start_ready('service',self.controller.health('service')):continue
            if role=='napcat' and not self.controller._start_ready('astrbot',self.controller.health('astrbot')):continue
            if not initial:
                history.append(now);self.next[role]=now+BACKOFF[min(len(history)-1,4)]
            # Persist before any restart, so a Workbench reopen cannot reset the limit.
            self.persist(states)
            self.expected_running[role]=True
            if not self.launch_operation(role,True) and initial:self.expected_running[role]=False
        self.persist(states)
        if now-self.last_maintenance>=60:
            self.last_maintenance=now
            from .maintenance import inspect_disk,automatic_backup
            disk=inspect_disk(self.paths)
            if not disk['blocked']:automatic_backup(self.paths,now)
