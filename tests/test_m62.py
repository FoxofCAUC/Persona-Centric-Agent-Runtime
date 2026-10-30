"""M6.2 response modes, deterministic cues, and validated reply persistence."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sqlite3
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.reply_batch import ReplySchemaError, validate_reply
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.storage import SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6,SCHEMA7,execute_schema
from persona_runtime.schema_m61 import SCHEMA8
from persona_runtime.schema_m62 import SCHEMA9
from persona_runtime.schema_m63 import SCHEMA10
from persona_runtime.behavior import resolve_behavior


class Fake:
    def __init__(self, response):
        self.response=response;self.messages=[];self.calls=0
    async def complete(self,model,messages):
        self.calls+=1;self.messages.append(messages);return self.response,{'total_tokens':7}


class M62Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='m62-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize();self.config.update({'model':{'base_url':'http://localhost:9/v1','name':'mock'}})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.response=json.dumps({'schema_version':1,'segments':[{'text':'已更正，类型声明确实写错了。','format':'plain'},{'text':'这段代码按原样保留：\n```py\nx = "a\\nb"\n```','format':'markdown'}]},ensure_ascii=False)
        self.fake=Fake('兼容模式完整回复');self.engine=ChatEngine(self.store,self.config,self.fake)
        self.ingress=IngressService(self.engine)
        for channel in ('desktop','qq'):
            policy=self.ingress.current_policy(self.pid,channel)
            self.ingress.update_policy(self.pid,channel,'legacy_per_message',policy['id'],identifier(),reply_mode='legacy_full',character_retrieval_mode='lexical')
        self.addAsyncCleanup(self.engine.wait)

    def policy(self,reply_mode):
        current=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],identifier(),reply_mode=reply_mode)

    def accept(self,text):
        return self.ingress.accept({'channel':'desktop','text':text,'request_id':identifier(),'persona_id':self.pid})

    async def test_legacy_default_keeps_whole_text_and_records_one_segment(self):
        receipt=self.accept('普通聊天')
        await self.engine.wait();turn=self.engine.get(receipt['turn_id'])
        self.assertEqual(turn['reply'],'兼容模式完整回复');self.assertEqual(self.fake.calls,1)
        self.assertEqual(turn['reply_batch']['mode'],'legacy_full')
        self.assertEqual([row['text'] for row in turn['reply_batch']['segments']],['兼容模式完整回复'])

    async def test_context_full_resolves_local_cue_and_versions_without_extra_call(self):
        self.policy('context_full');self.fake.response='这是完整的技术回答。'
        receipt=self.accept('Python 报错，帮我调试这个函数')
        await self.engine.wait();turn=self.engine.get(receipt['turn_id'])
        self.assertEqual(self.fake.calls,1)
        self.assertIn('代码、公式、URL 和列表保持可用',self.fake.messages[0][0]['content'])
        manifest=turn['context']['behavior']
        self.assertEqual(manifest['signals'],['technical_help'])
        self.assertEqual(manifest['guidance'],'advisory_lexical_cues_only')
        self.assertTrue(manifest['profile_versions']['core']['id'])

    async def test_character_example_is_selected_by_behavior_scenario(self):
        self.policy('context_full');self.fake.response='我来看看代码问题。'
        character=CharacterService(self.store);version=character.current(self.pid);data=deepcopy(version['data'])
        data['examples']=[{'id':'tech-example','scenario':'technical_help','keywords':[],'text':'先定位最小复现。','enabled':True}]
        character.update(self.pid,data,version['id'],identifier())
        receipt=self.accept('帮我调试这一段程序')
        await self.engine.wait();turn=self.engine.get(receipt['turn_id'])
        self.assertIn('先定位最小复现',self.fake.messages[0][0]['content'])
        self.assertEqual(turn['context']['behavior']['example_ids'],['tech-example'])

    async def test_structured_mode_persists_segments_but_sends_full_reply(self):
        self.policy('context_structured');self.fake.response=self.response
        receipt=self.accept('刚才不对，帮我修正代码')
        await self.engine.wait();turn=self.engine.get(receipt['turn_id'])
        self.assertEqual(self.fake.calls,1)
        self.assertEqual(turn['reply_batch']['segment_count'],2)
        self.assertEqual(len(turn['reply_batch']['segments']),2)
        self.assertEqual(turn['reply'],'\n\n'.join(row['text'] for row in turn['reply_batch']['segments']))
        self.assertIn('只返回一个 JSON 对象',self.fake.messages[0][0]['content'])

    async def test_invalid_structured_reply_fails_without_second_call_and_keeps_usage(self):
        self.policy('context_structured');self.fake.response='{"schema_version":1,"segments":[{"text":"好","format":"plain","send_to":"other"}]}'
        receipt=self.accept('回答一下')
        await self.engine.wait();turn=self.engine.get(receipt['turn_id'])
        self.assertEqual(self.fake.calls,1);self.assertEqual(turn['error_code'],'reply_schema_invalid')
        self.assertIsNone(turn['reply']);self.assertIsNotNone(turn['usage']);self.assertEqual(turn['reply_batch'],None)

    async def test_mode_snapshot_is_frozen_at_acceptance(self):
        current=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',current['id'],identifier(),reply_mode='context_full')
        receipt=self.accept('哈哈')
        current=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],identifier(),reply_mode='legacy_full',pending_action='seal')
        batch=self.ingress.get(receipt['batch_id'])
        sealed=batch if batch['turn_id'] else self.ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid)
        await self.engine.wait();turn=self.engine.get(sealed['turn_id'])
        self.assertEqual(turn['reply_batch']['mode'],'context_full')
        self.assertEqual(turn['context']['behavior']['signals'],['playful_tone'])

    async def test_reply_validation_rejects_duplicate_fields_extra_keys_and_unclosed_fences(self):
        invalid=(
            '{"schema_version":1,"schema_version":1,"segments":[{"text":"x","format":"plain"}]}',
            '{"schema_version":1,"segments":[{"text":"x","format":"plain","tool":"x"}]}',
            '{"schema_version":1,"segments":[{"text":"```py\\nx=1","format":"markdown"}]}',
        )
        for raw in invalid:
            with self.subTest(raw=raw),self.assertRaises(ReplySchemaError):validate_reply(raw,'context_structured')

    async def test_behavior_signals_are_weak_cues_and_limited(self):
        value=resolve_behavior('我很难过，Python代码也报错了，哈哈')
        self.assertLessEqual(len(value['signals']),2)
        self.assertIn('not_a_user_state_or_intent_diagnosis',value['uncertainty'])

    async def test_export_includes_reply_manifest_and_segments_without_credentials(self):
        self.fake.response=self.response;self.policy('context_structured')
        receipt=self.accept('代码修正')
        await self.engine.wait()
        data=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(data['format_version'],15);self.assertEqual(len(data['reply_batches']),1)
        self.assertEqual(len(data['reply_segments']),2)
        self.assertNotIn('encrypted_key',json.dumps(data))

    async def test_schema8_upgrade_and_schema9_failure_are_atomic(self):
        path=RuntimePaths(self.paths.home/'v8');path.initialize();store=Store(path)
        db=sqlite3.connect(store.path)
        for schema in (SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6,SCHEMA7,SCHEMA8):execute_schema(db,schema)
        db.execute('INSERT INTO users VALUES (?,?,?)',('00000000-0000-4000-8000-000000000001','legacy','time'))
        db.execute('PRAGMA user_version=8');db.commit();db.close()
        original=execute_schema
        def fail_after_m62(conn,schema):
            original(conn,schema)
            if schema==SCHEMA10:raise RuntimeErrorBase('injected schema10 failure')
        with patch('persona_runtime.storage.execute_schema',side_effect=fail_after_m62),self.assertRaisesRegex(RuntimeErrorBase,'schema10'):
            store.initialize()
        db=sqlite3.connect(store.path)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
        self.assertNotIn('reply_mode_snapshot',[row[1] for row in db.execute('PRAGMA table_info(turns)')])
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='reply_batches'").fetchone());db.close()
        store.initialize();store.initialize()
        with store.connection() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],18)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(conn.execute("SELECT dflt_value FROM pragma_table_info('interaction_policies') WHERE name='reply_mode'").fetchone()[0],"'legacy_full'")
        self.assertTrue(list((path.home/'backups').glob('before-m62-*.sqlite3')))

    async def test_reply_mode_management_is_admin_only_and_revisioned(self):
        current=self.ingress.current_policy(self.pid,'desktop');operation=identifier()
        result=self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],operation,reply_mode='context_full')
        self.assertEqual(self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],operation,reply_mode='context_full'),result)
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],operation,reply_mode='legacy_full')
        with self.assertRaisesRegex(RuntimeErrorBase,'version_conflict'):
            self.ingress.update_policy(self.pid,'desktop',current['mode'],current['id'],identifier(),reply_mode='legacy_full',pending_action='seal')
        app=__import__('persona_runtime.server',fromlist=['create_app']).create_app(self.paths,self.fake)
        async with TestServer(app) as server,ClientSession() as client:
            admin={'Authorization':'Bearer '+self.config.token()};bridge={'Authorization':'Bearer '+self.config.token('bridge')}
            async with client.post(server.make_url('/v1/manage'),headers=admin,json={'action':'reply.current','args':{'persona_id':self.pid,'channel':'desktop'}}) as response:
                self.assertEqual((await response.json())['reply_mode'],'context_full')
            async with client.post(server.make_url('/v1/manage'),headers=bridge,json={'action':'reply.update','args':{'persona_id':self.pid,'channel':'desktop','reply_mode':'context_structured','expected_version':result['id'],'operation_id':identifier()}}) as response:
                self.assertEqual(response.status,403)


if __name__=='__main__':unittest.main()
