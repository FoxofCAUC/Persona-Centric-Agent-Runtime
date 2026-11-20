"""Exercise repair, backups and interrupted exit through the real Windows workbench."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from persona_runtime.app_processes import AppProcesses
from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import LocalClient,LocalAPIError
from persona_runtime.lifecycle import ServiceController,port_open
from persona_runtime.local_repair import read
from persona_runtime.maintenance import mark_write_fault
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from tests.workbench_daily_cycle import Provider,setup,wait
from tests.workbench_import_v2_smoke import Screen


def phase_run(home,phase):
    import webview
    from persona_runtime import workbench_webview as desktop,maintenance,lifecycle
    assert home==RuntimePaths.from_environment().home and (home/'cycle-fixture').is_file()
    paths=RuntimePaths(home);store=Store(paths);config=ConfigStore(paths)
    pid,sid,number=json.loads((home/'ids.json').read_text())
    provider=Provider(home,number)
    hold_backup=threading.Event();hold_storage=threading.Event();hold_start=threading.Event()
    entered_backup=threading.Event();entered_storage=threading.Event();entered_start=threading.Event()
    result={};errors=[]
    backup=maintenance.automatic_backup;disk=maintenance.inspect_disk;start=ServiceController.start
    def slow_backup(paths,now,stopping):
        if phase=='live':
            entered_backup.set()
            while not hold_backup.wait(.05):
                if stopping():return
        return backup(paths,now,stopping)
    def paused_disk(paths):
        if (paths.home/'runtime/storage-fault.json').exists() and phase in ('live','repair-crash','repair-exit'):
            entered_storage.set()
            if not hold_storage.wait(40):raise AssertionError('storage test barrier timed out')
        return disk(paths)
    def paused_start(controller,role,*,automatic=False):
        incident=read(controller.paths,action='restart_service')
        if phase=='live' and role=='service' and incident and incident['phase']=='executing':
            entered_start.set()
            if not hold_start.wait(25):raise AssertionError('service test barrier timed out')
        return start(controller,role,automatic=automatic)
    create=AppProcesses.start
    def child(owner,role,args,**kwargs):
        assert role=='service'
        code="from tests.workbench_daily_cycle import fixture_service;fixture_service('persona_runtime.host')"
        return create(owner,role,[args[0],'-B','-c',code,*args[4:]],**kwargs)
    def row(text):
        with store.connection() as db:
            r=db.execute("SELECT t.id,t.status,t.error_code FROM turns t JOIN events e ON e.id=t.user_event_id WHERE e.text=?",(text,)).fetchone()
            return dict(r) if r else {}
    def flow(host):
        screen=Screen(host.window)
        def send(text):
            screen.wait("!pcarLive.offline && !pcarLive.selecting && !message.disabled")
            screen.eval("message.value="+json.dumps(text)+";message.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#composer').requestSubmit();true")
        def draft():
            screen.eval("message.value='714';message.dispatchEvent(new Event('input',{bubbles:true}));true")
            wait(lambda:host.controls.live_draft(pid,sid)['text']=='714')
        try:
            screen.wait("window.pcarLive?.active && !pcarLive.offline && !message.disabled",timeout=60)
            result['lease']=host.lease_token
            provider.start()
            if phase=='live':
                assert entered_backup.wait(8)
                send('5101');screen.wait("timeline.innerText.includes('6101')")
                result['chat_during_backup']=True
                provider.stop();send('5102')
                wait(lambda:row('5102').get('error_code')=='desktop_network_wait')
                wait(lambda:not host.controls.live_draft(pid,sid)['text']);draft()
                with host.controller.lifecycle_lock:host.controller.app_processes.stop('service')
                assert entered_start.wait(30)
                screen.wait("document.querySelector('#repair-dialog').open && document.querySelector('#repair-title').textContent==='修复中'")
                assert not hold_backup.is_set()
                restart_at=time.monotonic()
                hold_start.set()
                screen.wait("!document.querySelector('#repair-dialog').open && !pcarLive.offline && message.value==='714'",timeout=40)
                result['service_release_to_chat_seconds']=round(time.monotonic()-restart_at,2)
                assert result['service_release_to_chat_seconds']<=30
                result['restart']=read(paths,action='restart_service')['id']
                mark_write_fault(paths,store.path.parent)
                assert entered_storage.wait(16)
                screen.wait("document.querySelector('#repair-dialog').open && pcarLive.offline")
                direct=LocalClient(config)
                assert direct.call('GET','/v1/health')['service']=='persona-runtime'
                refusals=[]
                for path,body in (('/v1/config',{'interaction':{'cancel_queued':True}}),
                                  ('/v1/manage',{'action':'persona.use','args':{'persona_id':pid}}),
                                  ('/v2/ingress',{'channel':'desktop','persona_id':pid,'session_id':sid,'request_id':'650','text':'650'})):
                    try:direct.call('POST',path,body)
                    except LocalAPIError as error:assert error.status==423;refusals.append(path)
                    else:raise AssertionError('write was admitted during repair')
                result['service_write_rejections']=refusals
                from persona_runtime.local_repair import RepairInProgress
                try:host.controls.live_submit(pid,sid,'651','651')
                except RepairInProgress:result['host_write_rejected']=True
                else:raise AssertionError('host accepted a send during repair')
                provider.start()
                assert [json.loads(v) for v in (home/'provider.jsonl').read_text().splitlines()]==['5101']
                recovered_at=time.monotonic()
                hold_storage.set()
                screen.wait("!document.querySelector('#repair-dialog').open && !pcarLive.offline && message.value==='714'")
                screen.wait("timeline.innerText.includes('6102')",timeout=75)
                assert row('5102')['status']=='completed'
                result['storage_release_to_reply_seconds']=round(time.monotonic()-recovered_at,2)
                assert result['storage_release_to_reply_seconds']<=30
                result['storage']=read(paths,action='check_storage')['id']
                send('5113');assert provider.entered.wait(15)
                hold_storage.clear();entered_storage.clear()
                mark_write_fault(paths,store.path.parent)
                assert entered_storage.wait(18)
                screen.wait("document.querySelector('#repair-dialog').open")
                provider.release.set()
                wait(lambda:row('5113').get('status')=='completed')
                assert screen.eval("document.querySelector('#repair-dialog').open")
                hold_storage.set()
                screen.wait("!document.querySelector('#repair-dialog').open && timeline.innerText.includes('6103')")
                result['inflight_receipt_settled_during_repair']=True
                provider.release.clear();provider.entered.clear()
                hold_backup.set()
                wait(lambda:(home/'backups/workbench-automatic/index.json').is_file())
                screen.eval("document.querySelector('#menu-toggle').click();document.querySelector('[data-menu=advanced]').click();true")
                screen.click('本机备份与恢复')
                with patch.object(host.window,'create_file_dialog',return_value=[str(home.parent)]):
                    screen.click('选择恢复位置')
                    screen.wait("document.querySelector('dialog[open]').textContent.includes('已恢复')")
                restored=next(home.parent.glob('PCAR-恢复核对-*/restore.json'))
                result['restored_events']=json.loads(restored.read_text(encoding='utf8'))['counts']['events']
                assert result['restored_events']>=6
                screen.eval("document.querySelector('#dialog-close').click();true")
                send('5110');assert provider.entered.wait(15)
                host.controls.exit_workbench();host.controls.exit_workbench()
                wait(lambda:getattr(host.controller,'shutdown_progress',{}).get('phase')=='draining')
                provider.release.set()
            elif phase=='unconfirmed':
                send('5111');assert provider.entered.wait(15)
                host.controls.exit_workbench();host.controls.exit_workbench()
            elif phase in ('repair-crash','repair-exit'):
                assert row('5111')['status']=='interrupted'
                draft();mark_write_fault(paths,store.path.parent)
                assert entered_storage.wait(18)
                screen.wait("document.querySelector('#repair-dialog').open")
                ident=read(paths,action='check_storage')['id']
                (home/(phase+'-id.json')).write_text(json.dumps(ident))
                if phase=='repair-crash':os._exit(84)
                screen.eval("document.querySelector('#repair-exit').click();true")
                wait(host.controller.closing.is_set)
                hold_storage.set()
            else:
                prior=json.loads((home/('repair-crash-id.json' if phase=='after-crash' else 'repair-exit-id.json')).read_text())
                wait(lambda:read(paths,action='check_storage')['phase']=='resolved',30)
                assert read(paths,action='check_storage')['id']==prior
                screen.wait("!document.querySelector('#repair-dialog').open && message.value==='714' && !pcarLive.offline")
                result['resumed_repair']=prior
                if phase=='after-exit':
                    send('5105');screen.wait("timeline.innerText.includes('6105')")
        except BaseException as error:errors.append(error)
        finally:
            host.controls.exit_workbench()
            hold_backup.set();hold_storage.set();hold_start.set()
    class Host(desktop.WebViewHost):
        def run(self,web,html):
            launch=web.start
            with patch.object(web,'start',lambda **opts:launch(lambda:flow(self),**opts)):super().run(web,html)
    try:
        with patch.object(AppProcesses,'start',child),patch.object(ServiceController,'start',paused_start), \
                patch.object(maintenance,'automatic_backup',slow_backup),patch.object(maintenance,'inspect_disk',paused_disk), \
                patch.object(lifecycle,'DRAIN_SECONDS',.6 if phase=='unconfirmed' else 30),patch.object(desktop,'WebViewHost',Host):
            desktop.run(paths)
    finally:provider.release.set();provider.stop()
    if errors:raise errors[0]
    exit_record=json.loads((home/'runtime/last-exit.json').read_text(encoding='utf8'))
    assert exit_record['console_token']==result['lease'] and exit_record['processes_released']
    assert not ServiceController(paths).has_resident_processes() and not port_open(config.load()['service_port'])
    if phase=='live':
        assert row('5110')['status']=='completed'
        assert not exit_record['outstanding'].get('main_inflight')
    if phase=='unconfirmed':
        assert exit_record['drain_deadline_reached']
        assert exit_record['outstanding']['main_inflight']['count']==1
    result.update(exit_id=exit_record['exit_id'],outstanding=exit_record['outstanding'])
    (home/(phase+'.json')).write_text(json.dumps(result))


if __name__=='__main__':
    home=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:phase_run(home,sys.argv[2])
    else:
        setup(home);env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable}
        phases=('live','unconfirmed','repair-crash','after-crash','repair-exit','after-exit')
        for phase in phases:
            r=subprocess.run([sys.executable,'-B','-m','tests.workbench_repair_smoke',str(home),phase],env=env,timeout=155)
            assert r.returncode==(84 if phase=='repair-crash' else 0),(phase,r.returncode)
            if phase=='repair-crash':wait(lambda:not ServiceController(RuntimePaths(home)).has_resident_processes(),12)
        calls=[json.loads(v) for v in (home/'provider.jsonl').read_text().splitlines()]
        assert calls==['5101','5102','5113','5110','5111','5105'],calls
        report={p:json.loads((home/(p+'.json')).read_text()) for p in phases if p!='repair-crash'}
        print(json.dumps({'phases':report,'provider_received':calls,'real_model_calls':0,'crash_code':84}))
