"""Visible isolated workbench + real loopback API, artificial data, no QQ/model."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import threading
from unittest.mock import patch

from aiohttp import web
from persona_runtime.config import ConfigStore
from persona_runtime.desktop import Workbench
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_fixture


def counts(store):
    with store.connection() as db:
        return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
                ('events','turns','attempts','ingress_receipts','input_references','reference_checks','organization_attempts')}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    output=args.output.resolve();repo=Path(__file__).resolve().parents[1]
    if output.is_relative_to(repo) or output.exists():parser.error('Use a new evidence directory outside the repository')
    output.mkdir(parents=True)
    paths=RuntimePaths(output/'artificial-runtime');store=Store(paths);store.initialize();pid,sid,_=seed_fixture(store)
    config=ConfigStore(paths);config.initialize()
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    async def start():
        runner=web.AppRunner(create_app(paths));await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        config.update({'service_port':port})
        return runner,port
    runner,port=asyncio.run_coroutine_threadsafe(start(),loop).result(20)
    before=counts(store);trace=[];trace_lock=threading.Lock()
    # Background connection/process polling belongs to the managed product
    # console. This test window uses the real API on its own ephemeral port;
    # it must never start/stop the user's managed services.
    names=('poll','refresh_conversation','poll_organization','poll_profile','poll_batches','poll_deliveries')
    patches=[patch.object(Workbench,name,lambda self:None) for name in names]
    for patched in patches:patched.start()
    window=None
    try:
        window=Workbench(paths,managed_lifecycle=False)
        window.title('Persona-Centric Agent Runtime (PCAR) · M7.1 隔离验收')
        window.status_text.set('隔离人工档案 · 本机API已连接 · 未启停正式服务')
        original=window.client.manage
        def manage(action,**values):
            result=original(action,**values)
            if action.startswith('archive.'):
                with trace_lock:
                    trace.append({'action':action,'args':values,'result':result})
                    (output/'ui-api-evidence.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            return result
        window.client.manage=manage
        window.refresh_personas();window.show_page('archive-search')
        (output/'ready.json').write_text(json.dumps({'persona_id':pid,'session_id':sid,'port':port,
            'runtime_home':str(paths.home),'synthetic_only':True,'managed_services_started':False},indent=2)+'\n',encoding='utf-8')
        window.mainloop()
    finally:
        if window is not None:
            window.hub.pool.shutdown(wait=True,cancel_futures=True)
        for patched in reversed(patches):patched.stop()
        asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(15)
        loop.call_soon_threadsafe(loop.stop);thread.join(10)
        after=counts(store)
        (output/'closed.json').write_text(json.dumps({'before':before,'after':after,
            'authority_counts_unchanged':before==after,'provider_calls':0,'qq_sends':0,
            'api_stopped':not thread.is_alive(),'synthetic_only':True},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()
