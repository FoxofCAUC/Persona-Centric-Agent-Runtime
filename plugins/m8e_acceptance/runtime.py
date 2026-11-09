"""Finite SDK/model observation and declared fault injection. No task-state edits."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.models import identifier,timestamp
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.security import unprotect

ROOT=Path(__file__).resolve().parents[2]
EVIDENCE=DEFAULT_HOME/'exports/acceptance/M8.E-20261108'
SYNTHETIC='m8e-fixed-transport'
COMMANDS={'I3A':'执行 M8.E I3A 固定分片验证。','I3B':'执行 M8.E I3B 固定分片验证。'}


def core_hashes():
    files=list((ROOT/'persona_runtime').glob('*.py'))+list((ROOT/'integrations').rglob('*.py'))+list((ROOT/'integrations').rglob('*.cjs'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def owner():
    paths=RuntimePaths.from_environment();out=Path(os.environ['PCAR_M8E_EVIDENCE']).resolve()
    if out!=EVIDENCE.resolve():raise RuntimeError('M8.E evidence identity mismatch')
    if paths.home.resolve()==DEFAULT_HOME.resolve():
        if not read(out/'formal-deployment.json')['applied']:raise RuntimeError('Formal gate not passed')
        value=read(out/'formal-owner.json')
    else:
        value=read(out/'real-1/owner.json')
    if Path(value['home']).resolve()!=paths.home.resolve():raise RuntimeError('M8.E runtime identity mismatch')
    if value['core_hashes']!=core_hashes():raise RuntimeError('Frozen product source changed')
    if ConfigStore(paths).load()['instance_id']!=value['instance_id']:raise RuntimeError('M8.E instance changed')
    return paths,out,value


def fixed_text(position):
    from persona_runtime.reply_transport import render_parts
    wanted=3 if position=='I3A' else 4
    for count in range(15,80):
        text=f'【M8.E {position} 固定传输夹具】'+''.join(f'记录{n:02d}：编号与数值3.14保持完整。' for n in range(1,count+1))
        if len(render_parts(text,'chat')[1])==wanted:return text
    raise RuntimeError('Cannot construct registered fixed transport fixture')


def dispatch_identity(paths,messages):
    from persona_runtime.storage import Store
    key=fingerprint(messages)
    with Store(paths).connection() as db:
        for kind,table,foreign in [('background','organization_attempts','organization_attempt_id'),('main','attempts','attempt_id')]:
            rows=db.execute('SELECT a.*,m.manifest_json FROM '+table+' a JOIN context_manifests m ON m.'+foreign+'=a.id WHERE a.status=\'running\' ORDER BY a.rowid DESC').fetchall()
            for row in rows:
                manifest=json.loads(row['manifest_json'])
                if manifest.get('input_snapshot_hash')!=key:continue
                if kind=='background':
                    task=dict(db.execute('SELECT * FROM organization_tasks WHERE id=?',(row['task_id'],)).fetchone())
                    return kind,task['id'],row['id'],task['persona_id'],json.loads(task['payload_json'])
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(row['turn_id'],)).fetchone())
                from persona_runtime.turn_input import load_turn_input
                return kind,turn['id'],row['id'],turn['persona_id'],{'turn':turn,'members':load_turn_input(db,turn)}
    raise RuntimeError('No frozen request manifest for SDK dispatch')


def install_service():
    paths,out,expected=owner()
    from persona_runtime.chat import ChatCompletionsClient,ModelError,ModelCompletion
    original=ChatCompletionsClient.complete
    async def complete(client,model,messages):
        try:
            _,_,current=owner();kind,logical,attempt,pid,payload=dispatch_identity(paths,messages)
            if pid!=current['persona_id']:raise RuntimeError('Unregistered role')
            phase=read(out/'phase.json')['id']
            if model['name']==SYNTHETIC:
                if paths.home==DEFAULT_HOME or phase not in COMMANDS:raise RuntimeError('Unregistered synthetic provider')
                texts=[m['text'].strip() for m in payload['members']]
                if kind!='main' or texts!=[COMMANDS[phase]]:raise RuntimeError('Synthetic command mismatch')
                text=fixed_text(phase)
                atomic_json(out/'fixtures'/(attempt+'.json'),{'position':phase,'logical_id':logical,'attempt_id':attempt,
                    'simulated_provider':True,'real_platform_delivery':True,'model_network_calls':0,'reply_hash':fingerprint(text),'at':timestamp()})
                return ModelCompletion(text,{'total_tokens':0,'synthetic':True},{'simulated_provider':True,'finish_reason':'stop'})
            if model['name']!='deepseek-flash' or model['base_url'].rstrip('/') not in ('https://api.deepseek.com','https://api.deepseek.com/v1'):
                raise RuntimeError('Unapproved provider')
            approved=json.loads(unprotect(read(out/'models.dpapi.json')['data']))
            key=unprotect(model['encrypted_key'])
            if not any(key==unprotect(m['encrypted_key']) for m in approved if m.get('encrypted_key')):raise RuntimeError('Saved key changed')
            if kind=='background':
                automatic=payload.get('automatic_memory')
                position='I2-greeting' if payload.get('proactive') else 'I1' if paths.home!=DEFAULT_HOME else 'N1-backlog'
                stage=automatic['stage'] if automatic else 'scheduled_reply' if payload.get('proactive') else 'existing_organization'
                if position=='I1' and not automatic:raise RuntimeError('Unregistered isolated work')
            else:position=phase;stage='main'
            with closing(InstanceLock(out/'finite-dispatch.lock')):
                calls=[read(p) for p in (out/'calls').glob('*.json')]
                same=[c for c in calls if c['logical_id']==logical]
                if len(same)>=4 or any(c['status']!='failed_safe' for c in same):raise RuntimeError('Success or unknown replay')
                if sum(c['kind']==kind for c in calls)>=(24 if kind=='background' else 12):
                    atomic_json(out/'budget-exhausted.json',{'kind':kind,'at':timestamp()})
                    from persona_runtime.runtime_control import exit_application
                    exit_application(paths,expected['instance_id'],'acceptance-budget-stop')
                    raise RuntimeError('Finite acceptance budget exhausted')
                file=out/'calls'/(identifier()+'.json')
                row={'kind':kind,'position':position,'logical_id':logical,'attempt_id':attempt,'stage':stage,
                    'status':'dispatching','at':timestamp(),'messages_hash':fingerprint(messages)}
                atomic_json(file,row)
        except Exception as error:
            raise ModelError('m8e_acceptance_dispatch_blocked',diagnostics={'request_headers_sent':False,'acceptance_reason':type(error).__name__}) from None
        try:
            result=await original(client,model,messages)
            row.update(status='completed',usage=result[1],reply_hash=fingerprint(result[0]));atomic_json(file,row);return result
        except BaseException as error:
            diag=getattr(error,'diagnostics',None) or {}
            safe=diag.get('request_headers_sent') is False or diag.get('http_status')==429
            row.update(status='failed_safe' if safe else 'unknown',usage=getattr(error,'usage',None),error=type(error).__name__)
            atomic_json(file,row);raise
    ChatCompletionsClient.complete=complete
    if paths.home!=DEFAULT_HOME:
        from persona_runtime.automatic_memory import AutomaticMemory
        commit=AutomaticMemory.commit
        def fault(automatic,db,batch,payload,proposals,decisions):
            marker=out/'I1-receipt-crash.json'
            if not marker.exists():
                atomic_json(marker,{'at':timestamp(),'pid':os.getpid(),'after_durable_review_receipt':True,
                    'source_prefix':'m8e-artificial-I1','exit_code':77})
                os._exit(77)
            return commit(automatic,db,batch,payload,proposals,decisions)
        AutomaticMemory.commit=fault


def install_bridge():
    paths,out,expected=owner()
    from persona_runtime.qq_bridge import QQBridge
    original_handle=QQBridge.handle
    async def handle(bridge,*args,**kwargs):
        started=time.perf_counter();accepted=False
        try:
            accepted=await original_handle(bridge,*args,**kwargs)
            return accepted
        finally:
            elapsed=time.perf_counter()-started
            try:
                import ctypes
                from ctypes import wintypes as w
                user=ctypes.WinDLL('user32');user.GetForegroundWindow.restype=w.HWND
                user.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
                process=w.DWORD();user.GetWindowThreadProcessId(user.GetForegroundWindow(),ctypes.byref(process))
                application=read(paths.home/'runtime/console.json')['process']['pid']
                atomic_json(out/'frontend-admission'/(identifier()+'.json'),{'at':timestamp(),'seconds':elapsed,
                    'accepted':bool(accepted),'background_handler':kwargs.get('background',False),
                    'workbench_foreground_after':process.value==application,'formal':paths.home==DEFAULT_HOME})
            except Exception:pass
    QQBridge.handle=handle
    if paths.home==DEFAULT_HOME:return
    original_request=QQBridge.request;original_deliver=QQBridge.deliver_stream
    async def request(bridge,method,path,value=None):
        result=await original_request(bridge,method,path,value)
        marker=out/'I3A-ack-lost.json'
        if read(out/'phase.json')['id']=='I3A' and path.endswith('/result') and not marker.exists():
            atomic_json(marker,{'at':timestamp(),'operation_id':value['operation_id'],'part_id':path.split('/')[-2],
                'after_committed_result':True,'fault':'response_lost'})
            raise OSError('M8.E declared confirmation loss')
        return result
    async def deliver(bridge,stream,send):
        pending=[p for p in stream['parts'] if p['state']=='pending'];count=0
        async def observed(text):
            nonlocal count
            count+=1;result=await send(text)
            phase=read(out/'phase.json')['id']
            atomic_json(out/'sdk-sends'/(identifier()+'.json'),{'at':timestamp(),'phase':phase,
                'text_hash':fingerprint(text),'platform_message_id':result.get('message_id') if isinstance(result,dict) else result if isinstance(result,str) else None})
            marker=out/'I3B-third-unknown.json'
            if phase=='I3B' and count==3 and not marker.exists():
                atomic_json(marker,{'at':timestamp(),'part_id':pending[count-1]['id'],'fault':'platform_response_lost',
                    'test_observed_actual_send':True})
                return None
            return result
        return await original_deliver(bridge,stream,observed)
    QQBridge.request=request;QQBridge.deliver_stream=deliver


def window():
    paths,out,expected=owner()
    from persona_runtime.app_processes import AppProcesses
    start=AppProcesses.start
    def spawn(processes,role,args,**kwargs):
        if role in ('service','astrbot'):
            install='install_service' if role=='service' else 'install_bridge'
            code=f'from plugins.m8e_acceptance.runtime import {install};{install}();from persona_runtime.host import main;main()'
            args=args[:2]+['-c',code]+args[4:]
        return start(processes,role,args,**kwargs)
    AppProcesses.start=spawn
    from persona_runtime import desktop
    base=desktop.Workbench
    class AcceptanceWorkbench(base):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            if paths.home==DEFAULT_HOME:
                self.title('PCAR · 正式日常入口 · 原Bot尾号 '+ConfigStore(paths).load()['bot_qq'][-4:])
            self.after(300,self.hide_to_tray)
            self.after(600,self.observe)
            if os.environ.get('PCAR_M8E_REOPEN_LOGIN')=='1':
                self.after(1000,lambda:self.run_task('用户明确要求重新登录',{'lifecycle','config'},self.controller.start_all))
        def observe(self):
            if self.closed:return
            if paths.home==DEFAULT_HOME:
                try:
                    state=read(paths.home/'runtime/application.json').get('states',{}).get('napcat',{})
                    if state.get('account_matches') is False:
                        atomic_json(out/'formal-account-mismatch.json',{'at':timestamp(),'auto_stop':True})
                        self.close_window();return
                except (OSError,ValueError):pass
            folder=out/('formal-window' if paths.home==DEFAULT_HOME else 'test-window')
            atomic_json(folder/'ready.json',{'at':timestamp(),'pid':os.getpid(),'managed_lifecycle':self.managed_lifecycle,
                'tk_exists':bool(self.winfo_exists()),'tray_active':bool(self.tray and self.tray.active),
                'withdrawn':self.state()=='withdrawn','core_hashes':expected['core_hashes']})
            if (folder/'close.request').exists():self.close_window();return
            self.after(1000,self.observe)
        def destroy_console(self):
            hwnd=self.tray.hwnd if self.tray else None
            super().destroy_console()
            import ctypes
            user=ctypes.WinDLL('user32');user.IsWindow.argtypes=[ctypes.c_void_p]
            folder=out/('formal-window' if paths.home==DEFAULT_HOME else 'test-window')
            atomic_json(folder/'destroyed.json',{'at':timestamp(),'native_window_destroyed':not hwnd or not bool(user.IsWindow(hwnd))})
    desktop.Workbench=AcceptanceWorkbench
    desktop.run(paths)
