"""Visible isolated workbench; only explicit selector probes may call cloud."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from persona_runtime.chat import ChatCompletionsClient
from persona_runtime.character import CharacterService
from persona_runtime.character_embedding import CharacterEmbeddingProvider
from persona_runtime.character_retrieval import load_config
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_selector_protocol import PROMPTS
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop import Workbench
from persona_runtime.models import identifier,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.server import create_app
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_fixture


class ProbeOnlyClient:
    def __init__(self,store):self.store=store;self.client=ChatCompletionsClient();self.cloud_calls=0;self.mock_primary_calls=0
    async def complete(self,model,payload):
        if payload[0]['content'] in PROMPTS.values():
            with self.store.connection() as db:
                active=db.execute("SELECT purpose FROM character_selection_attempts WHERE status='running'").fetchall()
                probes=db.execute("SELECT count(*) FROM character_selection_attempts WHERE purpose='provider_probe'").fetchone()[0]
            if len(active)!=1 or active[0]['purpose']!='provider_probe' or probes>2:
                raise RuntimeErrorBase('ui_acceptance_probe_only')
            self.cloud_calls+=1
            return await self.client.complete(model,payload)
        self.mock_primary_calls+=1
        return '人工界面样例，未调用主模型。',{'total_tokens':0}


def counts(store):
    with store.connection() as db:
        return {t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('events','turns','attempts','character_selection_attempts','context_manifests','organization_attempts')}


def main(out):
    if out.exists() or out.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Use a new external evidence folder')
    out.mkdir(parents=True)
    paths=RuntimePaths(out/'runtime');store=Store(paths);store.initialize();pid,sid,_=seed_fixture(store)
    cfg=ConfigStore(paths);cfg.initialize();original=ConfigStore(RuntimePaths.from_environment()).load()
    data=cfg.load();data['model']=original['model'];data['organization']['daily_enabled']=False
    for key in ('base_url','name','encrypted_key'):data['character_selector'][key]=original['model'][key]
    data['character_selector']['enabled']=True;atomic_json(cfg.file,data)
    chars=CharacterService(store)
    fixture=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/m754r3_development.json').read_text(encoding='utf-8'))
    chars.update(pid,fixture['characters']['bakery'],chars.current(pid)['id'],identifier())
    client=ProbeOnlyClient(store);loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    async def start():
        app=create_app(paths,client)
        provider=CharacterEmbeddingProvider(RuntimePaths.from_environment(),RuntimePaths.from_environment().home/'models/character-embedding'/load_config()['model_fingerprint'])
        app['engine'].cognitive.characters.index_factory=lambda _:CharacterSemanticIndex(store,provider)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        cfg.update({'service_port':site._server.sockets[0].getsockname()[1]})
        return runner
    runner=asyncio.run_coroutine_threadsafe(start(),loop).result(20);quota=QuotaService(store,cfg);quota.begin_acceptance()
    before=counts(store);trace=[];window=None
    patches=[patch.object(Workbench,name,lambda self:None) for name in ('poll','refresh_conversation','poll_organization','poll_profile','poll_batches','poll_deliveries')]
    for item in patches:item.start()
    try:
        window=Workbench(paths,managed_lifecycle=False)
        window.title('Persona-Centric Agent Runtime (PCAR) · M7.5 R3 隔离验收')
        window.status_text.set('隔离人工资料 · 仅显式选择探针可计费 · 正式服务未启动')
        original_manage=window.client.manage
        def manage(action,**args):
            result=original_manage(action,**args)
            trace.append({'action':action,'args':args,'result':result});atomic_json(out/'ui-api-evidence.json',trace)
            return result
        window.client.manage=manage;window.refresh_personas();window.ss_id.set(sid)
        window.chat_input.insert('1.0','M754-R3未提交草稿');window.show_page('model')
        atomic_json(out/'ready.json',{'persona_id':pid,'session_id':sid,'port':cfg.load()['service_port'],'before':before,'synthetic_data':True,'actual_qq':False})
        window.mainloop()
    finally:
        if window is not None:window.hub.pool.shutdown(wait=True,cancel_futures=True)
        quota.end_acceptance()
        for item in reversed(patches):item.stop()
        asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(15);loop.call_soon_threadsafe(loop.stop);thread.join(10)
        atomic_json(out/'closed.json',{'before':before,'after':counts(store),'paid_selector_calls':client.cloud_calls,'paid_primary_calls':0,'simulated_primary_calls':client.mock_primary_calls,'api_stopped':not thread.is_alive(),'quota_restored':not quota.current()['acceptance_active']})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args().output.resolve())
