"""Workbench-owned supervision and maintenance. All workers stop with their console."""
import json
import math
import threading
import time

from .config import atomic_json
from .local_repair import LocalRepair
from .models import RuntimeErrorBase,timestamp

BACKOFF=(2,5,15,30,60)


class AppRuntime:
    def __init__(self,controller,*,clock=time.time):
        self.controller=controller;self.paths=controller.paths;self.clock=clock
        self.finished=threading.Event();self.thread=None
        self.file=self.paths.home/'runtime/application.json'
        self.budget_file=self.paths.home/'runtime/component-restarts.json'
        self.console_token=(getattr(controller,'console_lease',None) or {}).get('token')
        self.instance=(getattr(controller,'console_lease',None) or {}).get('instance_id')
        self.operation=None;self.operation_lock=threading.Lock()
        self.state_lock=threading.RLock()
        self.failures={};self.expected_running={};self.states={}
        self.jobs={};self.due={};self.job_failures={}
        self.repair=LocalRepair(controller,clock)
        self.repair.on_recovered=self._wake_accepted
        try:
            saved=json.loads(self.file.read_text(encoding='utf-8'))
            if self.instance and saved.get('instance') not in (None,self.instance):raise ValueError()
            self.restarts=saved.get('restarts',{})
            self.next=saved.get('next',{}) or {role:max(history)+BACKOFF[min(len(history)-1,4)] for role,history in self.restarts.items() if history}
            self.last_errors=saved.get('errors',{})
        except (OSError,ValueError,TypeError,AttributeError):
            self.restarts={};self.next={};self.last_errors={}
        self.budget_valid=True
        if self.budget_file.exists():
            try:
                budget=json.loads(self.budget_file.read_text(encoding='utf-8'))
                if budget['instance']==self.instance:
                    if (budget['version']!=1 or not isinstance(budget['restarts'],dict) or not isinstance(budget['next'],dict)
                            or any(not isinstance(v,list) or len(v)>5 or any(type(at) not in (int,float) or not math.isfinite(at) for at in v) for v in budget['restarts'].values())
                            or any(type(v) not in (float,int) or not math.isfinite(v) for v in budget['next'].values())):raise ValueError()
                    self.restarts=budget['restarts'];self.next=budget['next']
            except (OSError,ValueError,KeyError,TypeError):
                self.budget_valid=False
                stamp=self.clock()
                self.restarts={r:[stamp]*5 for r in ('service','astrbot','napcat')}
                self.next={r:stamp+1800 for r in self.restarts}

    def persist(self,states):
        with self.state_lock:
            self.states.update(states)
            atomic_json(self.file,{'at':timestamp(),'restarts':self.restarts,'next':self.next,
                'errors':dict(self.last_errors),'states':self.states,'owner':'workbench','instance':self.instance})

    def _persist_budget(self):
        atomic_json(self.budget_file,{'version':1,'instance':self.instance,'restarts':self.restarts,'next':self.next},durable=True)

    def _wake_accepted(self):
        if self.stopping():return
        port=self.controller.config.load()['service_port']
        if self.controller.service_endpoint_state(port)=='online':
            try:self.controller.client.call('POST','/v1/manage',{'action':'desktop.network.wake','args':{}},timeout=2)
            except RuntimeErrorBase:
                with self.state_lock:self.last_errors['wake']='resume_check_deferred'

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.controller.runtime_supervisor=self
        self.thread=threading.Thread(target=self.run,name='workbench-runtime',daemon=True);self.thread.start()

    def stop(self):self.finished.set()

    def stopping(self):
        return self.finished.is_set() or self.controller.closing.is_set()

    def join(self,timeout=3):
        end=time.monotonic()+timeout
        with self.state_lock:workers=list(self.jobs.values())
        if self.thread:workers.append(self.thread)
        for worker in workers:
            if worker!=threading.current_thread():worker.join(max(0,end-time.monotonic()))
        return not any(w.is_alive() for w in workers)

    def pending_maintenance(self):
        with self.state_lock:return sorted(self.jobs)

    def _background(self,name,interval,operation):
        with self.state_lock:
            if self.stopping() or name in self.jobs or self.clock()<self.due.get(name,0):return
            def work():
                try:
                    if self.stopping():return
                    operation()
                    with self.state_lock:
                        self.job_failures.pop(name,None);self.last_errors.pop(name,None)
                except Exception as error:
                    with self.state_lock:
                        self.last_errors[name]=type(error).__name__
                        self.job_failures[name]=min(6,self.job_failures.get(name,0)+1)
                finally:
                    with self.state_lock:
                        failures=self.job_failures.get(name,0)
                        self.due[name]=self.clock()+(min(60,2**failures) if failures else interval)
                        self.jobs.pop(name,None)
            thread=threading.Thread(target=work,name='workbench-'+name,daemon=True)
            self.jobs[name]=thread;thread.start()

    def ready(self,role,state):
        return state['state']=='运行' or role=='astrbot' and self.controller._start_ready(role,state)

    def reserve_restart(self,role):
        with self.state_lock:
            now=self.clock()
            history=[v for v in self.restarts.get(role,[]) if now-v<1800]
            if self.stopping() or len(history)>=5 or now<self.next.get(role,0):return False
            history.append(now);self.restarts[role]=history
            self.next[role]=now+BACKOFF[min(len(history)-1,4)]
            try:self._persist_budget()
            except OSError:
                history.pop();self.next[role]=now+2
                self.last_errors['supervisor']='restart_budget_unwritable'
                return False
            return True

    def launch_operation(self,role,start,*,fault=False):
        with self.operation_lock:
            if self.operation or self.stopping():return False
            self.operation={'role':role,'action':'start' if start else 'stop'}
        def work():
            try:
                with self.controller.lifecycle_lock:
                    if self.stopping():return
                    from .lifecycle import require_console
                    require_console(self.paths,self.console_token)
                    def verify():
                        state=self.controller.health(role)
                        if state['state']=='外部程序占用':raise RuntimeErrorBase('component_owner_conflict')
                        return self.ready(role,state)
                    def start_owned():
                        state=self.controller.health(role)
                        if self.ready(role,state):return
                        if state['state']=='外部程序占用' or self.stopping():raise RuntimeErrorBase('component_owner_conflict')
                        if self.controller.owned(role):
                            if not self.controller.can_release_owned(role):raise RuntimeErrorBase('component_owner_conflict')
                            self.controller.release_role(role)
                        if not self.stopping():self.controller.start(role,automatic=True)
                    if start and role=='service' and fault:
                        self.repair.run('restart_service',verify,start_owned,self.stopping,admit=lambda:self.reserve_restart(role))
                    elif start:
                        if not fault or self.reserve_restart(role):start_owned()
                    else:self.controller.stop(role)
                with self.state_lock:self.last_errors.pop(role,None)
            except Exception as error:
                with self.state_lock:self.last_errors[role]=type(error).__name__
            finally:
                with self.operation_lock:self.operation=None
        threading.Thread(target=work,name='owned-component-'+role,daemon=True).start()
        return True

    def _disk(self):
        from .maintenance import inspect_disk,automatic_backup,require_writable_storage,storage_fault_pending
        record=self.repair.outstanding('check_storage')
        def verify():
            try:
                require_writable_storage(self.paths)
                return not self.controller.owned('service') or self.ready('service',self.controller.health('service'))
            except (RuntimeErrorBase,OSError):return False
        if storage_fault_pending(self.paths) or record and record['phase']!='resolved':
            if not self.repair.run('check_storage',verify,lambda:inspect_disk(self.paths),self.stopping):return
        else:
            if self.clock()<getattr(self,'next_disk_check',0):return
            if inspect_disk(self.paths)['blocked']:
                self.repair.run('check_storage',verify,lambda:inspect_disk(self.paths),self.stopping)
                return
            self.next_disk_check=self.clock()+60
        self._background('backup',60,lambda:automatic_backup(self.paths,self.clock(),self.stopping))

    def run(self):
        while not self.stopping():
            try:self.tick(('service',))
            except Exception as error:
                with self.state_lock:self.last_errors['supervisor']=type(error).__name__
                try:atomic_json(self.paths.home/'runtime/application-error.json',{'at':timestamp(),'reason':type(error).__name__})
                except OSError:pass
            self._background('binding',10,self.controller.ensure_binding)
            self._background('channels',5,lambda:self.tick(('astrbot','napcat')))
            self._background('disk',5,self._disk)
            self.finished.wait(2)

    def tick(self,roles=None):
        now=self.clock();cfg=self.controller.config.load();states={}
        for role,enabled in cfg['runtime']['components'].items():
            if self.stopping():return
            if roles is not None and role not in roles:continue
            try:state=self.controller.health(role)
            except (OSError,RuntimeErrorBase,TimeoutError) as error:
                with self.state_lock:self.last_errors[role]=type(error).__name__
                states[role]={'state':'等待验证'};continue
            states[role]=state
            if role=='napcat' and cfg.get('qq_backend','napcat')=='snowluma':continue
            if role!='service' and not (cfg['owner_qq'] and cfg['bot_qq'] and cfg['runtime']['qq_persona_id']):enabled=False
            if role=='napcat' and (self.controller.console_lease or {}).get('login_confirmation_required'):continue
            if not enabled:
                self.expected_running[role]=False
                if self.controller.owned(role):self.launch_operation(role,False)
                continue
            if state['state']=='外部程序占用':
                if role=='service' and self.console_token:self.repair.external_service(self.stopping)
                continue
            if state['state']=='排空中':continue
            repair=self.repair.outstanding('restart_service') if role=='service' else None
            if self.ready(role,state):
                self.failures[role]=0;self.expected_running[role]=True
                if repair and repair['phase']!='resolved' and not self.operation:
                    self.launch_operation(role,True,fault=True)
                continue
            self.failures[role]=self.failures.get(role,0)+1
            initial=not self.expected_running.get(role,False)
            if self.failures[role]<3 and not (initial and state['state']=='已停止'):continue
            if role!='service' and not self.controller._start_ready('service',self.controller.health('service')):continue
            if role=='napcat' and not self.controller._start_ready('astrbot',self.controller.health('astrbot')):continue
            with self.state_lock:
                if not self.budget_valid:
                    try:self._persist_budget()
                    except OSError:
                        state['recovery_reason']='restart_budget_unverified';continue
                    self.budget_valid=True
                history=[v for v in self.restarts.get(role,[]) if now-v<1800]
                self.restarts[role]=history
                if len(history)>=5:
                    state.update(recovery_reason='restart_cooldown',next_check_epoch=min(history)+1800);continue
                if now<self.next.get(role,0) or self.operation:continue
                if repair and now<repair.get('next_epoch',0):continue
                fault=not initial or bool(repair and repair['phase']!='resolved')
                self.expected_running[role]=True
                if not self.launch_operation(role,True,fault=fault) and initial:self.expected_running[role]=False
        try:self.persist(states)
        except OSError:
            with self.state_lock:self.last_errors['supervisor']='diagnostic_unwritable'
