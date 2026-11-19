"""Exercise quota settings, both decisions and continued chat in Windows WebView2."""
import asyncio
import json
import tempfile
import threading
from pathlib import Path

from aiohttp import web
import webview
from persona_runtime.config import ConfigStore
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from persona_runtime.workbench_webview import DesktopControls,workbench_html
from tests.test_desktop_recovery import Completion
from tests.workbench_import_v2_smoke import Screen


def exercise(home):
    paths=RuntimePaths(home)
    config=ConfigStore(paths);config.initialize()
    config.update({'model':{'name':'deepseek-v4-pro','base_url':'https://api.deepseek.com','api_key':'791'}})
    store=Store(paths);store.initialize()
    service=RuntimeService(store)
    pid=service.create_persona(PersonaSpec(1,'791','791',['791'],'791',['791'],'791'))['id']
    service.use_persona(pid)
    with store.connection(write=True) as db:select(db,pid,'desktop')
    completion=Completion()
    loop=asyncio.new_event_loop()
    thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    async def start():
        runner=web.AppRunner(create_app(paths,client=completion,foreground_only=True))
        await runner.setup();await web.TCPSite(runner,'127.0.0.1',0).start()
        config.update({'service_port':runner.addresses[0][1]})
        return runner
    runner=asyncio.run_coroutine_threadsafe(start(),loop).result(20)
    controls=DesktopControls(lambda _:None,lambda:{'label':'791'},paths,lambda:window)
    window=webview.create_window("PCAR 限额验收",html=workbench_html(),js_api=controls,width=960,height=760,on_top=True)
    failures=[]
    def test():
        screen=Screen(window)
        def select_button(selector):
            screen.wait("document.querySelector("+json.dumps(selector)+")?.getClientRects().length>0")
            screen.eval("document.querySelector("+json.dumps(selector)+").click();true")
        def advanced():
            select_button('#menu-toggle');select_button('[data-menu="advanced"]')
        def send(text):
            screen.wait("!message.disabled && !pcarLive.selecting")
            screen.eval("message.value="+json.dumps(text)+";message.dispatchEvent(new Event('input',{bubbles:true}));true")
            select_button('#send')
        def completed(count):
            screen.wait("currentSession().turns.filter(t=>t.status==='completed').length==="+str(count),timeout=35)
        try:
            screen.wait("window.pcarLive?.active && !pcarLive.offline && !message.disabled")
            advanced();screen.click("模型调用限额")
            screen.wait("dialogBody.querySelector('input[type=checkbox]')!==null")
            assert not screen.eval("dialogBody.querySelector('input[type=checkbox]').checked")
            select_button('#dialog-body input[type=checkbox]')
            screen.eval("dialogBody.querySelector('input[type=number]').value='1';true")
            screen.click("保存限额")
            send("792");completed(1)
            send("793");screen.wait("document.querySelector('#quota-dialog').open",timeout=25)
            assert completion.calls==1
            select_button('#quota-wait')
            screen.wait("!document.querySelector('#quota-dialog').open")
            screen.wait("document.querySelector('#chat-status button').textContent==='查看等待任务'")
            select_button('#chat-status button')
            select_button('#quota-allow')
            screen.wait("!document.querySelector('#quota-dialog').open")
            completed(2)
            send("794");screen.wait("document.querySelector('#quota-dialog').open",timeout=25)
            select_button('#quota-allow');completed(3)
            assert completion.calls==3
            with store.connection() as db:
                assert db.execute("SELECT count(*) FROM ingress_receipts").fetchone()[0]==3
                assert db.execute("SELECT count(*) FROM model_call_meter").fetchone()[0]==3
            assert config.load()['call_limits']['enabled']
        except BaseException as exc:failures.append(exc)
        finally:window.destroy()
    try:webview.start(test,gui="edgechromium",private_mode=True,storage_path=str(home/'browser'))
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(35)
        loop.call_soon_threadsafe(loop.stop);thread.join(8);loop.close()
    if failures:raise failures[0]
    print(json.dumps({'window':'WebView2','accepted':3,'completed':3,'local_model_calls':completion.calls,
                      'defer_then_allow':True,'next_message_limited':True,'real_model_calls':0}))


if __name__=='__main__':
    root=RuntimePaths.from_environment().home/'tmp';root.mkdir(exist_ok=True,parents=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:exercise(Path(directory))
