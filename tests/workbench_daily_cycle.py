"""Two independent Windows desktop sessions using a private loopback model endpoint."""
import asyncio
import json
import os
import runpy
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from persona_runtime.chat import ChatCompletionsClient,ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store


def fixture_service(module):
    paths=RuntimePaths.from_environment()
    url=ConfigStore(paths).load()['model']['base_url']
    assert url.startswith('http://127.0.0.1:') and (paths.home/'cycle-fixture').is_file()
    class LocalTransport(ChatCompletionsClient):
        async def route_reachable(self,model):
            if (paths.home/'pause-in-probe').exists():
                (paths.home/'probe-entered').touch()
                await asyncio.Event().wait()
            return await super().route_reachable(model)
    with patch('persona_runtime.chat.ChatCompletionsClient',lambda:LocalTransport(allow_loopback_fixture=True)):
        runpy.run_module(module,run_name='__main__')


def wait(check,timeout=45):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=check()
        if value:return value
        time.sleep(.1)
    raise AssertionError(f'timed out at {check.__code__.co_firstlineno}')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


class Provider:
    def __init__(self,home,number):
        self.home,self.port=home,number
        self.http=None;self.thread=None
        self.entered=threading.Event();self.release=threading.Event()
    def start(self):
        home=self.home
        entered,release=self.entered,self.release
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                marker=payload['messages'][-1]['content']
                assert marker in ('5101','5102','5103','5104','5105','5106','5110','5111','5112','5113')
                with (home/'provider.jsonl').open('a',encoding='utf8') as out:out.write(json.dumps(marker)+'\n')
                if marker=='5104':
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                if marker in ('5110','5111','5113'):
                    entered.set()
                    if not release.wait(45) or marker=='5111':return
                body=json.dumps({'choices':[{'message':{'content':'610'+marker[-1]},'finish_reason':'stop'}],
                                 'usage':{'total_tokens':1}}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        self.http=ThreadingHTTPServer(('127.0.0.1',self.port),Handler)
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
    def stop(self):
        if self.http:self.http.shutdown();self.http.server_close();self.thread.join(2);self.http=None


def setup(home):
    from persona_runtime.ingress import IngressService
    from persona_runtime.models import PersonaSpec,identifier
    from persona_runtime.service import RuntimeService
    if home.exists() and any(home.iterdir()):raise ValueError("验收需要新的隔离目录。")
    paths=RuntimePaths(home);cfg=ConfigStore(paths);cfg.initialize()
    (home/'cycle-fixture').touch()
    service_port,model_port=port(),port()
    while service_port==model_port:model_port=port()
    cfg.update({'service_port':service_port,'model':{'base_url':f'http://127.0.0.1:{model_port}/v1','name':'791','api_key':'791'}})
    store=Store(paths);store.initialize();service=RuntimeService(store)
    pid=service.create_persona(PersonaSpec(1,'790','790',['790'],'790',['790'],'790'))['id'];service.use_persona(pid)
    engine=ChatEngine(store,cfg);ingress=IngressService(engine)
    policy=ingress.current_policy(pid,'desktop')
    if policy.get('companion'):
        policy=ingress.update_policy(pid,'desktop',policy['mode'],policy['id'],identifier(),companion_mode='disabled')
    ingress.update_policy(pid,'desktop','legacy_per_message',policy['id'],identifier(),reply_mode='legacy_full',context_mode='legacy_recent')
    sid=engine.session(persona_id=pid)['session_id']
    engine.cognitive.characters.close()
    (home/'ids.json').write_text(json.dumps([pid,sid,model_port]))


def desktop(home,phase):
    import webview
    import persona_runtime.workbench_webview as desktop_module
    from persona_runtime.app_processes import AppProcesses
    from tests.workbench_import_v2_smoke import Screen
    if home!=RuntimePaths.from_environment().home or not (home/'cycle-fixture').is_file():
        raise ValueError("验收目录与子进程实例不一致。")
    pid,sid,number=json.loads((home/'ids.json').read_text())
    paths=RuntimePaths(home);store=Store(paths);provider=Provider(home,number)
    results={};errors=[]
    def last(marker):
        with store.connection() as db:
            r=db.execute("""SELECT t.id,t.status,t.error_code,t.reply_event_id FROM turns t JOIN events e ON t.user_event_id=e.id
                WHERE t.persona_id=? AND t.session_id=? AND e.text=?""",(pid,sid,marker)).fetchone()
            return dict(r) if r else {}
    def exercise(host):
        screen=Screen(host.window)
        def send(value):
            screen.wait("!pcarLive.offline && !message.disabled && !pcarLive.selecting")
            screen.eval("message.value="+json.dumps(value)+";message.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#composer').requestSubmit();true")
        def menu(label):
            screen.eval("document.querySelector('#menu-toggle').click();[...document.querySelectorAll('#app-menu button')].find(b=>b.textContent==="+json.dumps(label)+").click();true")
        def draft():
            screen.eval("message.value='710';message.dispatchEvent(new Event('input',{bubbles:true}));true")
            wait(lambda:host.controls.live_draft(pid,sid)['text']=='710')
        def hidden():
            menu("收起到托盘");wait(lambda:host.in_tray and not host.form.Visible)
        try:
            screen.wait("window.pcarLive?.active && !pcarLive.offline && !message.disabled",timeout=55)
            screen.wait("currentSession()?.id==="+json.dumps(sid))
            results['console']=host.lease_token
            if phase=='power_loss':
                send('5102');wait(lambda:last('5102').get('error_code')=='desktop_network_wait')
                wait(lambda:not host.controls.live_draft(pid,sid)['text']);draft()
                def probed_offline():
                    with store.connection() as db:
                        return db.execute("""SELECT 1 FROM knowledge_control WHERE key LIKE 'desktop.network.%'
                            AND json_extract(value,'$.probes')>=1""").fetchone()
                if (home/'pause-in-probe').exists():wait(lambda:(home/'probe-entered').exists())
                else:wait(probed_offline)
                assert host.controller.owned('service')
                (home/'before-loss.json').write_text(json.dumps({'turn':last('5102')['id'],'console':host.lease_token}))
                os._exit(73)
            elif phase=='second_loss':
                before=json.loads((home/'before-loss.json').read_text())
                assert host.lease_token!=before['console'] and host.controller.console_lease['previous_stale']
                screen.wait("message.value==='710'")
                wait(lambda:(home/'probe-entered').exists())
                assert last('5102')['id']==before['turn'] and not (home/'provider.jsonl').exists()
                os._exit(73)
            elif phase in ('after_loss','after_joint_loss'):
                before=json.loads((home/'before-loss.json').read_text())
                assert host.lease_token!=before['console'] and host.controller.console_lease['previous_stale']
                screen.wait("message.value==='710'")
                provider.start();screen.wait("timeline.innerText.includes('6102')",timeout=75)
                assert last('5102')['id']==before['turn']
                send('5105');wait(lambda:last('5105').get('status')=='completed')
                results.update(stale_owner_recovered=True,original_turn=before['turn'])
                if phase=='after_joint_loss':
                    other=json.loads((home/'joint-other.json').read_text())
                    def browse(person):
                        value=json.dumps(person)
                        screen.eval("pcarLive.select(state.people.find(p=>p.id==="+value+"));true")
                        screen.wait("currentPerson()?.id==="+value+" && !pcarLive.selecting && !pcarLive.offline")
                    send('5111');assert provider.entered.wait(15)
                    browse(other)
                    assert not screen.eval("timeline.innerText.includes('5111')")
                    hidden()
                    with host.controller.lifecycle_lock:host.controller.app_processes.stop('service')
                    provider.release.set()
                    wait(lambda:last('5111').get('status')=='interrupted')
                    host.queue.put(('show',None))
                    screen.wait("currentPerson()?.id==="+json.dumps(other)+" && !pcarLive.offline")
                    assert not screen.eval("timeline.innerText.includes('5111')")
                    browse(pid)
                    send('5112')
                    wait(lambda:last('5112').get('status')=='completed')
                    reply_event=last('5112')['reply_event_id']
                    screen.wait("[...timeline.querySelectorAll('.bubble.reply')].find(b=>b.dataset.anchor==="+json.dumps(reply_event)+")?.textContent==='6102'")
                    results.update(double_loss=True,unknown_on_original=last('5111')['id'],other_history_clean=True)
            elif phase=='evening':
                provider.start();send('5101')
                screen.wait("timeline.innerText.includes('6101')")
                send('5110');assert provider.entered.wait(12)
                draft();hidden();provider.release.set()
                wait(lambda:last('5110').get('status')=='completed')
                assert host.in_tray and not host.form.Visible
                from System import Action
                host.form.BeginInvoke(Action(lambda:host.menu.Items[0].PerformClick()))
                wait(lambda:host.form.Visible and not host.in_tray)
                screen.wait("message.value==='710' && timeline.innerText.includes('6100')")
                results.update(tray_menu=True,background_reply=True)
                provider.stop();send('5102')
                wait(lambda:last('5102').get('error_code')=='desktop_network_wait')
                wait(lambda:not host.controls.live_draft(pid,sid)['text']);draft()
                hidden()
                old=host.controller.record('service')['host']['ProcessId']
                with host.controller.lifecycle_lock:host.controller.app_processes.stop('service')
                def restarted():
                    record=host.controller.record('service') or {}
                    return record.get('host',{}).get('ProcessId') not in (None,old) and host.controls.live_health()['state']=='online'
                wait(restarted,55);results['service_restarted_in_tray']=True
                child=subprocess.run([sys.executable,'-B','-m','persona_runtime','gui','--frontend','webview'],capture_output=True,timeout=20)
                assert child.returncode==0,child.stderr
                wait(lambda:host.form.Visible and not host.in_tray)
                screen.wait("!pcarLive.offline && message.value==='710'")
                results['second_launch_woke_existing']=True
            elif phase=='morning':
                screen.wait("message.value==='710'")
                assert last('5102')['error_code']=='desktop_network_wait'
                provider.start();screen.wait("timeline.innerText.includes('6102')",timeout=75)
                results['original_recovered']=last('5102')['id']
                provider.stop();send('5103')
                wait(lambda:last('5103').get('error_code')=='desktop_network_wait')
                tid=last('5103')['id']
                screen.wait("currentSession().turns.some(t=>t.id==="+json.dumps(tid)+" && t.error_code==='desktop_network_wait')")
                screen.eval("[...timeline.querySelectorAll('button')].filter(b=>b.textContent==='停止本轮').at(-1).click();true")
                wait(lambda:last('5103').get('error_code')=='cancelled_by_user')
                provider.start();send('5104')
                wait(lambda:last('5104').get('error_code')=='provider_effect_unknown')
                send('5105');screen.wait("timeline.innerText.includes('6105')")
                results['cancelled']=tid;results['unknown']=last('5104')['id']
                client=host.controls._client
                policy=client.manage('interaction.current',persona_id=pid,channel='desktop')
                client.manage('interaction.update',persona_id=pid,channel='desktop',mode='natural_batch',
                              expected_version=policy['id'],operation_id='75106')
                send('5106')
                def accepted_buffer():
                    with store.connection() as db:
                        return db.execute("""SELECT b.id FROM ingress_receipts r JOIN events e ON e.id=r.event_id
                            JOIN input_batches b ON b.id=r.batch_id WHERE e.text='5106' AND b.state='collecting'""").fetchone()
                results['collecting_on_exit']=wait(accepted_buffer)[0]
            else:
                provider.start();screen.wait("timeline.innerText.includes('6106')",timeout=75)
                from System import Action
                from System.Windows.Forms import FormWindowState
                host.form.BeginInvoke(Action(lambda:setattr(host.form,'WindowState',FormWindowState.Minimized)))
                wait(lambda:host.form.WindowState==FormWindowState.Minimized)
                wake=subprocess.run([sys.executable,'-B','-m','persona_runtime','gui','--frontend','webview'],capture_output=True,timeout=20)
                assert wake.returncode==0
                wait(lambda:host.form.WindowState==FormWindowState.Normal)
                screen.wait("currentSession()?.id==="+json.dumps(sid))
                assert last('5104')['error_code']=='provider_effect_unknown'
                assert last('5103')['error_code']=='cancelled_by_user'
                results['minimize_wake']=True
                send('5111');assert provider.entered.wait(12)
                with host.controller.lifecycle_lock:host.controller.app_processes.stop('service')
                provider.release.set()
                wait(lambda:last('5111').get('status')=='interrupted',55)
                screen.wait("!pcarLive.offline && currentSession().turns.some(t=>t.id==="+json.dumps(last('5111')['id'])+" && t.status==='interrupted')")
                send('5112');wait(lambda:last('5112').get('status')=='completed')
                screen.wait("currentSession().turns.some(t=>t.id==="+json.dumps(last('5112')['id'])+" && t.status==='completed')")
                results['inflight_service_loss']=True
        except BaseException as error:
            errors.append(error)
            print('failed step',repr(error),[last('510'+str(i)) for i in range(1,6)],flush=True)
        finally:
            host.queue.put(('exit',None))
    class Host(desktop_module.WebViewHost):
        def run(self,web,html):
            start=web.start
            with patch.object(web,'start',lambda **opts:start(lambda:exercise(self),**opts)):
                super().run(web,html)
    actual=AppProcesses.start
    def start_process(owner,role,args,**opts):
        assert role=='service'
        code="from tests.workbench_daily_cycle import fixture_service;fixture_service('persona_runtime.host')"
        return actual(owner,role,[args[0],'-B','-c',code,*args[4:]],**opts)
    try:
        with patch.object(AppProcesses,'start',start_process),patch.object(desktop_module,'WebViewHost',Host):
            desktop_module.run(paths)
    finally:provider.stop()
    if errors:raise errors[0]
    from persona_runtime.lifecycle import ServiceController
    assert not ServiceController(paths).has_resident_processes()
    exit_report=json.loads((home/'runtime/last-exit.json').read_text(encoding='utf8'))
    assert exit_report['processes_released'] and exit_report['console_token']==results['console']
    from persona_runtime.lifecycle import port_open
    assert not port_open(ConfigStore(paths).load()['service_port'])
    results['all_services_released']=True
    (home/(phase+'.json')).write_text(json.dumps(results),encoding='utf8')


if __name__=='__main__':
    home=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2 and sys.argv[2] in ('--crash-pair','--crash-in-probe','--joint-loss'):
        assert not (home/'config').exists()
        setup(home)
        joint=sys.argv[2]=='--joint-loss'
        during_probe=joint or sys.argv[2]=='--crash-in-probe'
        if joint:
            from persona_runtime.models import PersonaSpec
            from persona_runtime.service import RuntimeService
            svc=RuntimeService(Store(RuntimePaths(home)))
            other=svc.create_persona(PersonaSpec(1,'792','792',['792'],'792',['792'],'792'))['id']
            svc.use_persona(json.loads((home/'ids.json').read_text())[0])
            (home/'joint-other.json').write_text(json.dumps(other))
        if during_probe:(home/'pause-in-probe').touch()
        env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable}
        cmd=[sys.executable,'-B','-m','tests.workbench_daily_cycle',str(home)]
        crashed=subprocess.run(cmd+['power_loss'],env=env,timeout=100)
        assert crashed.returncode==73 and (home/'before-loss.json').is_file()
        from persona_runtime.lifecycle import ServiceController,port_open
        ctl=ServiceController(RuntimePaths(home))
        wait(lambda:not ctl.has_resident_processes() and not port_open(ctl.config.load()['service_port']),15)
        if joint:
            (home/'probe-entered').unlink()
            second=subprocess.run(cmd+['second_loss'],env=env,timeout=75)
            assert second.returncode==73
            wait(lambda:not ctl.has_resident_processes() and not port_open(ctl.config.load()['service_port']),15)
        if during_probe:
            assert (home/'probe-entered').exists()
            (home/'pause-in-probe').unlink()
        reopened='after_joint_loss' if joint else 'after_loss'
        subprocess.run(cmd+[reopened],check=True,env=env,timeout=150)
        calls=[json.loads(s) for s in (home/'provider.jsonl').read_text().splitlines()]
        assert calls==(['5102','5105','5111','5112'] if joint else ['5102','5105'])
        print(json.dumps({'crash_exit_code':73,'during_probe':during_probe,'orphan_services':0,'received':calls,
                          'reopened':json.loads((home/(reopened+'.json')).read_text()),'real_model_calls':0}))
    elif len(sys.argv)>2:desktop(home,sys.argv[2])
    else:
        assert not (home/'config').exists()
        setup(home)
        env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable}
        for phase in ('evening','morning','reopen'):
            subprocess.run([sys.executable,'-B','-m','tests.workbench_daily_cycle',str(home),phase],
                           check=True,env=env,timeout=160)
        phases=[json.loads((home/(p+'.json')).read_text()) for p in ('evening','morning','reopen')]
        assert len({p['console'] for p in phases})==3
        calls=[json.loads(line) for line in (home/'provider.jsonl').read_text().splitlines()]
        assert calls==['5101','5110','5102','5104','5105','5106','5111','5112'],calls
        with Store(RuntimePaths(home)).connection() as db:
            assert db.execute("SELECT count(*) FROM ingress_receipts").fetchone()[0]==9
            outcomes={r[0]:(r[1],r[2]) for r in db.execute("""SELECT e.text,t.status,r.text FROM turns t
                JOIN events e ON e.id=t.user_event_id LEFT JOIN events r ON r.id=t.reply_event_id""")}
            for marker in ('5101','5110','5102','5105','5106','5112'):assert outcomes[marker]==('completed','610'+marker[-1])
            assert outcomes['5103']==('interrupted',None) and outcomes['5111']==('interrupted',None) and outcomes['5104']==('failed',None)
            assert not db.execute('PRAGMA foreign_key_check').fetchone()
            assert db.execute("PRAGMA integrity_check").fetchone()[0]=='ok'
        print(json.dumps({'phases':phases,'received':calls,'outcomes':outcomes,'accepted_messages':9,'real_model_calls':0}))
