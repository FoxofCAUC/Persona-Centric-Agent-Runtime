"""Verify a partially displayed scene message across tray exit and Windows cold start."""
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from tests.workbench_daily_cycle import port,wait
from tests.workbench_import_v2_smoke import Screen


async def prepare(home):
    from tests.test_scene_voice_delivery import Delivery
    case=Delivery()
    case.setUp()
    try:
        await case.asyncSetUp()
        try:
            case.cfg.update({"model":{"api_key":"791"}})
            case.enable()
            turn=case.send("801")
            plan=await case.wait_for(turn,lambda row:row.get("scene_speech"))
            await case.chat.wait()
            assert len(plan["items"])==2 and len(case.main.calls)==1
            identity=[case.pid,turn["session_id"],turn["id"],plan["utterance_id"]]
        finally:await case.asyncTearDown()
        shutil.copytree(case.paths.home,home)
        (home/"joint-presentation.json").write_text(json.dumps(identity),encoding="utf8")
        ConfigStore(RuntimePaths(home)).update({"service_port":port()})
    finally:case.doCleanups()


def local_service(module):
    assert module=="persona_runtime.host"
    def loopback(event,args):
        if event=="socket.connect" and isinstance(args[1],tuple):
            host=args[1][0]
            if host!="localhost" and not ipaddress.ip_address(host).is_loopback:
                raise ValueError("joint fixture requires loopback")
    sys.addaudithook(loopback)
    runpy.run_module(module,run_name="__main__")


def phase(home,reopen):
    from persona_runtime import workbench_webview as desktop
    from persona_runtime.app_processes import AppProcesses
    from persona_runtime.lifecycle import ServiceController
    from persona_runtime.workbench_import import WorkbenchData
    paths=RuntimePaths(home)
    pid,sid,tid,uid=json.loads((home/"joint-presentation.json").read_text())
    store=Store(paths)
    failures=[]
    offers=[]
    begin=WorkbenchData.presentation_begin
    def offer(controls,page,claim,kind,args):
        if args.get("utterance_id")==uid:
            ordinal=args["ordinal"]
            if not reopen and ordinal>0:return None
            offers.append(ordinal)
        return begin(controls,page,claim,kind,args)
    def displayed():
        with store.connection() as db:
            return [row[0] for row in db.execute("SELECT ordinal FROM expression_speech_visible WHERE utterance_id=? ORDER BY ordinal",(uid,))]
    def exercise(host):
        screen=Screen(host.window)
        try:
            from System import Action
            def foreground():
                host.form.TopMost=True
                host.form.Activate()
            wait(lambda:host.form is not None)
            host.form.BeginInvoke(Action(foreground))
            host.queue.put(("show",None))
            screen.wait("window.pcarLive?.active && !pcarLive.offline")
            screen.wait("currentSession()?.id==="+json.dumps(sid)+" && !currentSession().loading")
            wait(lambda:displayed()==([0,1] if reopen else [0]),45)
            if not reopen:
                screen.eval("document.querySelector('#menu-toggle').click();[...document.querySelectorAll('#app-menu button')].find(b=>b.textContent==='收起到托盘').click();true")
                wait(lambda:host.in_tray and not host.form.Visible)
                assert displayed()==[0] and offers==[0]
            else:
                assert offers==[1],offers
                screen.wait("timeline.innerText.includes('ID-PRESENTATION-FIRST') && timeline.innerText.includes('ID-PRESENTATION-SECOND')")
            (home/("reopened.json" if reopen else "tray.json")).write_text(json.dumps({"offers":offers,"visible":displayed()}))
        except BaseException as error:
            print("presentation",screen.eval("JSON.stringify([currentPerson()?.id,currentSession()?.id,document.visibilityState,dialog.open,!!document.querySelector('#dialog[open]'),timeline.innerText.slice(-150)])"),offers,displayed(),flush=True)
            failures.append(error)
        finally:host.controls.exit_workbench()
    class Host(desktop.WebViewHost):
        def run(self,web,html):
            original=web.start
            with patch.object(web,"start",lambda **ops:original(lambda:exercise(self),**ops)):
                super().run(web,html)
    start=AppProcesses.start
    def controlled(owner,role,args,**kwargs):
        assert role=="service"
        code="from tests.workbench_joint_presentation import local_service;local_service('persona_runtime.host')"
        return start(owner,role,[args[0],"-B","-c",code,*args[4:]],**kwargs)
    with patch.object(WorkbenchData,"presentation_begin",offer),patch.object(AppProcesses,"start",controlled),patch.object(desktop,"WebViewHost",Host):
        desktop.run(paths)
    if failures:raise failures[0]
    assert not ServiceController(paths).has_resident_processes()


if __name__=="__main__":
    home=Path(sys.argv[1]).resolve()
    if len(sys.argv)>2:phase(home,sys.argv[2]=="reopen")
    else:
        if home.exists():raise ValueError("请选择新隔离目录")
        asyncio.run(prepare(home))
        for stage in ("tray","reopen"):
            subprocess.run([sys.executable,"-B","-m","tests.workbench_joint_presentation",str(home),stage],
                           check=True,env=os.environ|{"PERSONA_RUNTIME_HOME":str(home)},timeout=120)
        print(json.dumps({k:json.loads((home/(k+".json")).read_text()) for k in ("tray","reopened")}))
