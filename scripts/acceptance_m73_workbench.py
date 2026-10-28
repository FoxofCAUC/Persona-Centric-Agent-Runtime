"""Visible isolated native UI, real local API, artificial one-call history."""
import argparse
import asyncio
import json
from pathlib import Path
import threading
from unittest.mock import patch
from aiohttp import web
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.desktop import Workbench
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_fixture


class ArtificialModel:
    def __init__(self):self.calls=0
    async def complete(self,*args):self.calls+=1;return '人工离线回复，未连接真实模型。',{'total_tokens':1}


def counts(store):
    with store.connection() as db:return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
        ('events','turns','attempts','context_manifests','input_references','organization_attempts')}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    out=args.output.resolve()
    if out.exists() or out.is_relative_to(Path(__file__).resolve().parents[1]):parser.error('Use a new external evidence folder')
    out.mkdir(parents=True);paths=RuntimePaths(out/'artificial-runtime');store=Store(paths);store.initialize();pid,sid,_=seed_fixture(store)
    cfg=ConfigStore(paths);cfg.initialize();cfg.update({'model':{'name':'artificial','base_url':'http://127.0.0.1:1/v1'}})
    model=ArtificialModel();engine=ChatEngine(store,cfg,model)
    async def seed():
        turn=await engine.submit('desktop','青禾书店的预算和颜色是什么？','m73-gui-sample',sid);await engine.wait();return turn['id']
    tid=asyncio.run(seed())
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    async def start():
        runner=web.AppRunner(create_app(paths,model));await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1];cfg.update({'service_port':port});return runner,port
    runner,port=asyncio.run_coroutine_threadsafe(start(),loop).result(20)
    before=counts(store);trace=[];lock=threading.Lock()
    patches=[patch.object(Workbench,name,lambda self:None) for name in ('poll','refresh_conversation','poll_organization','poll_profile','poll_batches','poll_deliveries')]
    for p in patches:p.start()
    window=None;draft=None
    try:
        window=Workbench(paths,managed_lifecycle=False);window.title('Persona-Centric Agent Runtime (PCAR) · M7.3 隔离验收')
        window.status_text.set('隔离人工资料 · 本机API在线 · 未启停正式服务')
        original=window.client.manage
        def manage(action,**values):
            result=original(action,**values)
            if action.startswith(('session.','topic.','thread.')):
                with lock:
                    trace.append({'action':action,'args':values,'result':result})
                    (out/'ui-api-evidence.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            return result
        window.client.manage=manage;window.refresh_personas();window.ss_id.set(sid);window.chat_input.insert('1.0','M73未提交草稿');window.show_page('sessions')
        (out/'ready.json').write_text(json.dumps({'persona_id':pid,'session_id':sid,'turn_id':tid,'port':port,'synthetic_only':True,'formal_services_started':False},indent=2)+'\n',encoding='utf-8')
        window.mainloop()
    finally:
        if window is not None:window.hub.pool.shutdown(wait=True,cancel_futures=True)
        for p in reversed(patches):p.stop()
        asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(15);loop.call_soon_threadsafe(loop.stop);thread.join(10)
        (out/'closed.json').write_text(json.dumps({'before':before,'after':counts(store),'counts_unchanged':before==counts(store),
            'simulated_calls_total':model.calls,'draft_at_close':draft,'paid_provider_calls':0,'qq_sends':0,'api_stopped':not thread.is_alive()},indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
