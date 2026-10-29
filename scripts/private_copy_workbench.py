"""Window-bound private copy acceptance. No QQ, probes, hidden archive or replay.

Only a body-free audit survives; the sandbox is deleted on normal/error exit.
"""
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
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.presentation import violations
from persona_runtime.private_personas import PrivatePersonas
from persona_runtime.quotas import QuotaService
from persona_runtime.server import create_app
from persona_runtime.storage import Store


class BoundedClient:
    def __init__(self):self.client=ChatCompletionsClient();self.main=0;self.selector=0;self.usage=[]
    async def complete(self,model,payload):
        selector=payload[0]['content'] in PROMPTS.values()
        if selector:
            if self.selector>=4:raise RuntimeError('private_copy_selection_limit')
            self.selector+=1
        else:
            if self.main>=4:raise RuntimeError('private_copy_primary_limit')
            self.main+=1
        text,usage=await self.client.complete(model,payload)
        self.usage.append({'kind':'selector' if selector else 'main','usage':usage})
        return text,usage


def configure_embedding(runtime,store,formal):
    if runtime.indexes:raise RuntimeError('private_copy_embedding_setup_order')
    provider=CharacterEmbeddingProvider(formal,formal.home/'models/character-embedding'/load_config()['model_fingerprint'])
    runtime.index_factory=lambda _:CharacterSemanticIndex(store,provider)
    return provider


def main(private_id):
    formal=RuntimePaths.from_environment();private=PrivatePersonas(formal)
    copy=private.create_sandbox(private_id);paths=RuntimePaths(Path(copy['home']));store=Store(paths)
    cfg=ConfigStore(paths);cfg.initialize();original=ConfigStore(formal).load();data=cfg.load()
    data['model']=original['model'];data['character_selector']=original['character_selector']
    data['organization']['daily_enabled']=False;atomic_json(cfg.file,data)
    client=BoundedClient();loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True)
    thread.start();runner=None;window=None;audit={'actual_qq':False,'copied_hidden_archive':False,'private_original_tested':False}
    quota=QuotaService(store,cfg);patches=[]
    try:
        async def start():
            app=create_app(paths,client);engine=app['engine'];policies=app['ingress'];pid=copy['persona_id']
            configure_embedding(engine.cognitive.characters,store,formal)
            version=CharacterService(store).current(pid)
            await asyncio.to_thread(engine.cognitive.characters.index(load_config()).build,pid,version['id'])
            for channel in ('desktop','qq'):
                old=policies.current_policy(pid,channel)
                policies.update_policy(pid,channel,'legacy_per_message',old['id'],identifier(),reply_mode='natural_structured',context_mode='working_set',cross_session=False,character_retrieval_mode='hybrid_llm')
            policy=policies.current_policy(pid,'desktop')['character_retrieval']
            await asyncio.to_thread(engine.cognitive.characters.local_pool,version,policy,['你好'])
            runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            cfg.update({'service_port':site._server.sockets[0].getsockname()[1]})
            return runner
        runner=asyncio.run_coroutine_threadsafe(start(),loop).result(90);quota.begin_acceptance()
        # The API and workers are owned by this very window process, not a host.
        for name in ('poll','poll_organization','poll_profile','poll_batches','poll_deliveries'):
            item=patch.object(Workbench,name,lambda self:None);item.start();patches.append(item)
        window=Workbench(paths,managed_lifecycle=False);window.title('Persona-Centric Agent Runtime (PCAR) · 私有临时副本')
        window.show_page('chat');window.refresh_personas();window.after(700,window.load_chat)
        window.status_text.set('独立私有副本，仅窗口4次主要调用，关闭后删除本地副本。')
        atomic_json(private.root/private_id/'copy-window-state.json',copy|{'port':cfg.load()['service_port']})
        window.mainloop()
    finally:
        if window is not None:window.hub.pool.shutdown(wait=True,cancel_futures=True)
        quota.end_acceptance()
        if runner is not None:asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(30)
        loop.call_soon_threadsafe(loop.stop);thread.join(15)
        for item in reversed(patches):item.stop()
        with store.connection() as db:
            turns=[dict(r) for r in db.execute('SELECT id,status,reply_event_id FROM turns ORDER BY rowid')]
            raw_errors=[]
            for turn in turns:
                for row in db.execute('SELECT s.text,s.content_kind FROM reply_segments s JOIN reply_batches b ON b.id=s.batch_id WHERE b.turn_id=?',(turn['id'],)):
                    raw_errors+=violations(row['text'],row['content_kind'])
            audit.update(main_calls=client.main,selector_calls=client.selector,usage=client.usage,
                turn_statuses=[t['status'] for t in turns],raw_format_errors=raw_errors,
                zero_organization_calls=db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]==0,
                zero_memories=db.execute('SELECT count(*) FROM memories').fetchone()[0]==0)
        if thread.is_alive():raise RuntimeError('private_sandbox_api_not_stopped')
        audit.update(private.cleanup_sandbox(paths.home))
        atomic_json(private.root/private_id/('copy-acceptance-'+paths.home.name+'.json'),audit)
        atomic_json(private.root/private_id/'copy-acceptance-audit.json',audit)
        (private.root/private_id/'copy-window-state.json').unlink(missing_ok=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--id',required=True)
    main(parser.parse_args().id)
