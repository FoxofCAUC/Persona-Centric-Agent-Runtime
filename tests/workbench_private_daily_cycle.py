"""Stage 4 event-driven Windows candidate cycle. Each window and service is a separate process."""
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from tests.test_private_reply_stage3 import ResearchProviders
from tests.workbench_daily_cycle import port, wait
from tests.workbench_import_v2_smoke import Screen


def setup(home, *, persona_spec=None):
    from persona_runtime import session_privacy
    from persona_runtime.service import RuntimeService
    from persona_runtime.sessions import select
    if home.exists():raise ValueError('expected a fresh rehearsal directory')
    paths=RuntimePaths(home);cfg=ConfigStore(paths);cfg.initialize()
    cfg.update({'service_port':port(),'model':{'name':'deepseek-v4-pro','base_url':'https://api.deepseek.com','api_key':'731'}})
    store=Store(paths);store.initialize();service=RuntimeService(store)
    spec=persona_spec or PersonaSpec(1,'731','732',['733'],'734',['735'],'736')
    pid=service.create_persona(spec)['id'];service.use_persona(pid)
    args=(store,cfg,'jev','https://example.invalid/v1','jev-1.14','737')
    preview=session_privacy.connection(*args)
    session_privacy.connection(*args,confirm=preview['confirmation'])
    with store.connection(write=True) as db:
        sid=select(db,pid,'desktop')
        for key,data in [('policy.',{'enabled':True,'channels':['desktop']}),('web.policy.',{'enabled':True})]:
            db.execute("INSERT OR REPLACE INTO knowledge_control VALUES(?,?)",
                       ('knowledge.'+key+pid,json.dumps({'revision':1,'data':data})))
    (home/'cycle.json').write_text(json.dumps({'persona':pid,'session':sid,'instance':cfg.load()['instance_id']}))


def local_service(module):
    from persona_runtime import server
    from persona_runtime.chat import ModelError
    home=RuntimePaths.from_environment().home
    assert (home/'cycle.json').is_file()
    def only_loopback(event,args):
        if event=='socket.connect' and isinstance(args[1],tuple):
            address=args[1][0]
            if address!='localhost' and not ipaddress.ip_address(address).is_loopback:
                raise RuntimeError('external connection in local rehearsal')
    sys.addaudithook(only_loopback)
    class Provider(ResearchProviders):
        def mark(self,key,aid):
            with (home/'calls.jsonl').open('a',encoding='ascii') as f:
                f.write(json.dumps([key,aid])+'\n');f.flush();os.fsync(f.fileno())
        async def choose(self,node,aid,state,choices):
            if node=='J1' and (home/'offline').exists():
                (home/'prewire-observed').touch()
                raise ModelError('provider_network_error',diagnostics={'request_headers_sent':False})
            self.mark(node,aid)
            if node=='J1' and (home/'hold').exists():
                (home/'entered').touch()
                async with asyncio.timeout(40):
                    while (home/'hold').exists():await asyncio.sleep(.05)
            self.route='web' if (home/'lookup').exists() else 'none'
            return await super().choose(node,aid,state,choices)
        async def reply(self,aid,packet):
            self.mark('writer',aid)
            self.body='751。'*30
            return await super().reply(aid,packet)
        async def retrieve(self,step,aid,query,model):
            self.mark(step,aid)
            return await super().retrieve(step,aid,query,model)
    actual=server.create_app
    with patch.object(server,'create_app',lambda *a,**kw:actual(*a,private_chat_providers=Provider(),**kw)):
        runpy.run_module(module,run_name='__main__')


def desktop(home, phase):
    from persona_runtime import workbench_webview as gui
    from persona_runtime.app_processes import AppProcesses
    from persona_runtime.lifecycle import ServiceController,port_open
    from persona_runtime.workbench_import import WorkbenchData
    from persona_runtime.session_privacy import status
    paths=RuntimePaths(home);store=Store(paths);cfg=ConfigStore(paths)
    identity=json.loads((home/'cycle.json').read_text())
    pid,sid=identity['persona'],identity['session']
    def turn(marker):
        with store.connection() as db:
            row=db.execute("""SELECT t.* FROM turns t JOIN events e ON e.id=t.user_event_id
                WHERE t.session_id=? AND t.persona_id=? AND e.text=?""",(sid,pid,marker)).fetchone()
            return dict(row) if row else {}
    def visible(tid):
        with store.connection() as db:
            return [dict(r) for r in db.execute("""SELECT u.phase,v.ordinal,v.text,v.receipt_id,v.presented_at
                FROM expression_speech_units u JOIN expression_speech_visible v ON v.utterance_id=u.id
                WHERE u.turn_id=? ORDER BY u.rowid,v.ordinal""",(tid,))]
    begin=WorkbenchData.presentation_begin
    def offer(controls,page,claim,kind,args):
        if phase=='evening' and kind=='speech' and args['ordinal']>0 and (home/'partial').exists():
            return None
        return begin(controls,page,claim,kind,args)
    errors=[]; report={'phase':phase,'ui_pid':os.getpid(),'instance':identity['instance'],'session':sid}
    def exercise(host):
        from System import Action
        from System.Windows.Forms import FormWindowState
        screen=Screen(host.window)
        def activate():
            host.form.TopMost=True;host.form.Activate()
        def send(value):
            screen.wait('!pcarLive.offline && !message.disabled && !pcarLive.selecting')
            screen.eval("message.value="+json.dumps(value)+";message.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#composer').requestSubmit();true")
        def tray():
            screen.eval("document.querySelector('#menu-toggle').click();[...document.querySelectorAll('#app-menu button')].find(b=>b.textContent==='收起到托盘').click();true")
            wait(lambda:host.in_tray and not host.form.Visible)
        def wake():
            child=subprocess.run([sys.executable,'-B','-m','persona_runtime','gui','--frontend','webview'],
                                 capture_output=True,timeout=25)
            assert child.returncode==0
            wait(lambda:host.form.Visible and not host.in_tray and host.form.WindowState!=FormWindowState.Minimized)
            host.form.BeginInvoke(Action(activate))
        try:
            wait(lambda:host.form is not None)
            host.form.BeginInvoke(Action(activate));host.queue.put(('show',None))
            screen.wait("window.pcarLive?.active && !pcarLive.offline && currentSession()?.id==="+json.dumps(sid),timeout=50)
            report.update(console=host.lease_token,service_pid=host.controller.record('service')['host']['ProcessId'])
            if phase=='evening':
                screen.eval("document.getElementById('session-privacy').click();true")
                screen.click('同意并开启本会话全流程授权');screen.wait('!dialog.open')
                assert status(store,cfg,pid,sid)['enabled']
                (home/'hold').touch();(home/'lookup').touch()
                submitted=datetime.now(timezone.utc)
                send('741');wait(lambda:(home/'entered').exists())
                host.form.BeginInvoke(Action(lambda:setattr(host.form,'WindowState',FormWindowState.Minimized)))
                wait(lambda:host.form.WindowState==FormWindowState.Minimized)
                (home/'hold').unlink()
                tid=wait(lambda:turn('741').get('id'))
                def has_plan():
                    with store.connection() as db:return db.execute("SELECT 1 FROM expression_speech_units WHERE turn_id=?",(tid,)).fetchone()
                wait(has_plan);assert not visible(tid)
                with store.connection() as db:assert not db.execute("SELECT 1 FROM private_lookup_calls WHERE turn_id=?",(tid,)).fetchone()
                woke=datetime.now(timezone.utc);wake()
                wait(lambda:turn('741').get('status')=='completed',65)
                frames=visible(tid)
                assert [v['phase'] for v in frames]==['search_notice','main']
                end=datetime.fromisoformat(frames[-1]['presented_at'])
                first=datetime.fromisoformat(frames[0]['presented_at'])
                report.update(submission_to_first_seconds=(first-submitted).total_seconds(),
                              submission_to_last_seconds=(end-submitted).total_seconds(),wake_to_last_seconds=(end-woke).total_seconds())
                assert 0<=report['submission_to_last_seconds']<=150
                (home/'lookup').unlink();(home/'partial').touch()
                report['partial_submitted_at']=datetime.now(timezone.utc).isoformat()
                send('742');tid=wait(lambda:turn('742').get('id'));wait(lambda:len(visible(tid))==1)
                tray();report['partial_turn']=tid;report['prefix_receipt']=visible(tid)[0]['receipt_id']
                wake();screen.wait('!message.disabled');screen.eval("message.value='743';message.dispatchEvent(new Event('input',{bubbles:true}));true")
                wait(lambda:host.controls.live_draft(pid,sid)['text']=='743')
                tray();assert len(visible(tid))==1
            elif phase=='morning':
                old=json.loads((home/'evening.json').read_text());tid=old['partial_turn']
                wait(lambda:turn('742').get('status')=='completed',55)
                pieces=visible(tid)
                assert len(pieces)>1 and pieces[0]['receipt_id']==old['prefix_receipt']
                assert [p['ordinal'] for p in pieces]==list(range(len(pieces)))
                screen.wait("message.value==='743'");assert status(store,cfg,pid,sid)['enabled']
                report.update(partial_turn=tid,visible_fragments=len(pieces))
                start=datetime.fromisoformat(old['partial_submitted_at'])
                report.update(submission_to_first_seconds=(datetime.fromisoformat(pieces[0]['presented_at'])-start).total_seconds(),
                              submission_to_last_seconds=(datetime.fromisoformat(pieces[-1]['presented_at'])-start).total_seconds())
                assert 0<=report['submission_to_first_seconds']<=report['submission_to_last_seconds']<=120
                (home/'offline').touch();report['pending_submitted_at']=datetime.now(timezone.utc).isoformat();send('743')
                wait(lambda:turn('743').get('error_code')=='desktop_network_wait')
                report['pending_turn']=turn('743')['id']
                wait(lambda:host.controls.live_draft(pid,sid)['text']=='')
            else:
                old=json.loads((home/'morning.json').read_text())
                wait(lambda:turn('743').get('status')=='completed',55)
                assert turn('743')['id']==old['pending_turn'] and status(store,cfg,pid,sid)['enabled']
                pieces=visible(old['pending_turn']);start=datetime.fromisoformat(old['pending_submitted_at'])
                report.update(recovered_turn=old['pending_turn'],visible_fragments=len(pieces),
                              submission_to_first_seconds=(datetime.fromisoformat(pieces[0]['presented_at'])-start).total_seconds(),
                              submission_to_last_seconds=(datetime.fromisoformat(pieces[-1]['presented_at'])-start).total_seconds())
                assert 0<=report['submission_to_first_seconds']<=report['submission_to_last_seconds']<=120
                screen.wait("!document.getElementById('input-buffer-settings').disabled")
                screen.eval("document.getElementById('input-buffer-settings').click();true")
                screen.wait("Boolean(dialog.querySelector('select'))")
                screen.eval("const m=dialog.querySelector('select');m.value='manual';m.dispatchEvent(new Event('change'));true")
                screen.click('保存');screen.wait('!dialog.open')
                with store.connection() as db:before=db.execute('SELECT count(*) FROM model_call_meter').fetchone()[0]
                for count,marker in enumerate(('744','745'),1):
                    send(marker)
                    screen.wait("currentSession()?.inputBuffer?.batch?.members?.length==="+str(count))
                with store.connection() as db:assert db.execute('SELECT count(*) FROM model_call_meter').fetchone()[0]==before
                screen.wait("!document.getElementById('input-buffer-confirm').disabled")
                confirmed=datetime.now(timezone.utc)
                screen.eval("document.getElementById('input-buffer-confirm').click();true")
                def completed_batch():
                    with store.connection() as db:
                        return db.execute("""SELECT t.id FROM ingress_receipts r JOIN events e ON e.id=r.event_id
                            JOIN turns t ON t.input_batch_id=r.batch_id
                            WHERE e.text='744' AND t.status='completed'""").fetchone()
                batch_turn=wait(completed_batch)[0];pieces=visible(batch_turn)
                report['confirm_to_first_seconds']=(datetime.fromisoformat(pieces[0]['presented_at'])-confirmed).total_seconds()
                report['confirm_to_last_seconds']=(datetime.fromisoformat(pieces[-1]['presented_at'])-confirmed).total_seconds()
                assert 0<=report['confirm_to_first_seconds']<=report['confirm_to_last_seconds']<=120
        except BaseException as exc:
            import traceback
            (home/(phase+'-error.txt')).write_text(traceback.format_exc(),encoding='utf8')
            errors.append(exc)
        finally:host.controls.exit_workbench()
    class Host(gui.WebViewHost):
        def run(self,web,html):
            original=web.start
            with patch.object(web,'start',lambda **opts:original(lambda:exercise(self),**opts)):super().run(web,html)
    original=AppProcesses.start
    def start(owner,role,args,**opts):
        assert role=='service'
        code="from tests.workbench_private_daily_cycle import local_service;local_service('persona_runtime.host')"
        return original(owner,role,[args[0],'-B','-c',code,*args[4:]],**opts)
    with patch.object(AppProcesses,'start',start),patch.object(gui,'WebViewHost',Host),patch.object(WorkbenchData,'presentation_begin',offer):
        gui.run(paths)
    assert not ServiceController(paths).has_resident_processes() and not port_open(cfg.load()['service_port'])
    exit_report=json.loads((home/'runtime/last-exit.json').read_text(encoding='utf8'))
    if errors:raise errors[0]
    assert exit_report['processes_released'] and exit_report['console_token']==report['console']
    report['all_processes_released']=True
    (home/(phase+'.json')).write_text(json.dumps(report),encoding='utf8')


def run(home):
    setup(home)
    env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable}
    phases=[]
    for phase in ('evening','morning','restart'):
        if phase=='restart':(home/'offline').unlink()
        subprocess.run([sys.executable,'-B','-m','tests.workbench_private_daily_cycle',str(home),phase],
                       check=True,env=env,timeout=165)
        phases.append(json.loads((home/(phase+'.json')).read_text()))
    calls=[json.loads(line) for line in (home/'calls.jsonl').read_text().splitlines()]
    assert len({aid for _,aid in calls})==len(calls)
    assert len({p['console'] for p in phases})==3 and len({p['service_pid'] for p in phases})==3
    with Store(RuntimePaths(home)).connection() as db:
        recorded=[r[0] for r in db.execute("SELECT attempt_id FROM private_reply_nodes WHERE node NOT LIKE '%handoff' UNION ALL SELECT id FROM private_reply_attempts UNION ALL SELECT attempt_id FROM private_lookup_calls")]
        assert sorted(recorded)==sorted(aid for _,aid in calls)
        assert db.execute("SELECT count(*) FROM ingress_receipts").fetchone()[0]==5
        assert db.execute("SELECT count(*) FROM turns WHERE status='completed'").fetchone()[0]==4
        assert db.execute("SELECT count(*) FROM model_call_meter").fetchone()[0]==len(calls)
        assert not db.execute("PRAGMA foreign_key_check").fetchone() and db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    result={'phases':phases,'local_dispatches':len(calls),'accepted_inputs':5,'completed_turns':4,'duplicates':0,'real_model_calls':0}
    (home/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    directory=Path(sys.argv[1]).resolve()
    if len(sys.argv)==2:run(directory)
    else:
        assert RuntimePaths.from_environment().home==directory
        desktop(directory,sys.argv[2])
