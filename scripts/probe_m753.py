"""Isolated actual local embedding + mock main call, never formal deployment."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.character import CharacterService,empty_character
from persona_runtime.character_embedding import CharacterEmbeddingProvider,digest
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_retrieval import load_config
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store


class MockMain:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):
        self.calls.append(messages)
        return '隔离模拟主要模型：已收到。',{'total_tokens':1}


async def probe(home,evidence):
    evidence.mkdir(parents=True,exist_ok=True)
    sandbox=evidence/'isolated-runtime'
    if sandbox.exists():raise RuntimeError('Use a new evidence directory; never overwrite a prior run')
    paths=RuntimePaths(sandbox);store=Store(paths);store.initialize()
    config=ConfigStore(paths);config.initialize()
    config.update({'model':{'name':'mock-only','base_url':'http://127.0.0.1:1/v1','hourly_limit':10}})
    model=MockMain();engine=ChatEngine(store,config,model);runtime=engine.cognitive.characters
    provider=CharacterEmbeddingProvider(home,home.home/'models/character-embedding'/load_config()['model_fingerprint'])
    runtime.index_factory=lambda _:CharacterSemanticIndex(store,provider)
    pid=engine.service.create_persona(generic_persona())['id'];engine.service.use_persona(pid)
    chars=CharacterService(store);data=empty_character()
    # Reuse one known development input as an integration fixture, not recalibration.
    data['lorebook']=[{'id': 'reading', 'title': '夜读偏好', 'category': '背景', 'keywords': ['纸页时光'], 'text': '角色喜欢下班后沏热茶，在安静的书店阅读小说。', 'priority': 0, 'enabled': True, 'visibility': 'conversation', 'dependencies': []}]
    v=chars.update(pid,data,chars.current(pid)['id'],identifier())
    ingress=IngressService(engine);started=time.monotonic()
    try:
        with patch('socket.getaddrinfo',side_effect=AssertionError('offline only')),patch('socket.create_connection',side_effect=AssertionError('offline only')),patch.object(socket.socket,'connect',side_effect=AssertionError('offline only')):
            policy=ingress.current_policy(pid,'desktop')
            policy=ingress.update_policy(pid,'desktop','natural_batch',policy['id'],identifier(),character_retrieval_mode='hybrid')
            key=runtime._key(pid,v['id'],policy['character_retrieval'])
            def wait_ready():
                with runtime.coordinator.condition:
                    runtime.coordinator.condition.wait_for(lambda:runtime.coordinator.states.get(key,{}).get('state') in ('ready','failed'),60)
                return runtime.coordinator.status(key)
            state=await asyncio.to_thread(wait_ready)
            assert state['state']=='ready',state
            ready_ms=(time.monotonic()-started)*1000
            with patch.object(engine,'_schedule'):
                r=ingress.accept({'channel':'desktop','text':'先打个招呼','request_id':identifier()})
                ingress.accept({'channel':'desktop','text':'忙完一天想端杯热茶找个清静地方看看故事书','request_id':identifier()})
                b=ingress.get(r['batch_id']);ingress.action(b['id'],'seal',b['revision'],identifier(),pid)
            tid=ingress.get(r['batch_id'])['turn_id']
            await engine._run(tid)
            assert engine.get(tid)['status']=='completed',engine.get(tid)['error_code']
            assert len(model.calls)==1
            with store.connection() as conn:
                manifest=json.loads(conn.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(tid,)).fetchone()[0])
                before=list(conn.iterdump())
            audit=manifest['character_retrieval']
            assert audit['effective_mode']=='hybrid',audit
            assert 'reading' in audit['selected']['lore'],audit
            assert '阅读小说' in model.calls[0][0]['content']
            assert manifest['input_snapshot_hash']==digest(model.calls[0])
            preview=runtime.preview(pid,members=['忙完一天想端杯热茶找个清静地方看看故事书'],mode='hybrid',channel='desktop')
            assert preview['effective_mode']=='hybrid'
            with store.connection() as conn:assert list(conn.iterdump())==before
            report={'status':'passed','network_blocked':True,'formal_runtime_changed':False,
                'paid_calls':0,'mock_main_calls':len(model.calls),'local_queries':2,'model_fingerprint':provider.identity()['fingerprint'],
                'automatic_preparation_ms':round(ready_ms,3),'character_audit':audit,'manifest_input_hash':manifest['input_snapshot_hash'],
                'preview_read_only':True,'database_schema':14,'scope':'isolated integration only'}
            (evidence/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:runtime.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(probe(RuntimePaths.from_environment(),args.evidence.absolute()))
