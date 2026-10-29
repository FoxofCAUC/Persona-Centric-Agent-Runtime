"""M6.3 persistent partial-delivery, claim ownership, and recovery checks."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.delivery import DeliveryService
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.storage import SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6,SCHEMA7,execute_schema
from persona_runtime.schema_m61 import SCHEMA8
from persona_runtime.schema_m62 import SCHEMA9
from persona_runtime.schema_m63 import SCHEMA10
from persona_runtime.server import create_app
from persona_runtime.qq_bridge import QQBridge


class Fake:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def complete(self, model, messages):
        self.calls += 1
        return self.text, {"total_tokens": 1}


class M63Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="m63-",dir=root);self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name));self.store=Store(self.paths);self.store.initialize()
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({"model":{"base_url":"http://localhost:9/v1","name":"mock"},"owner_qq":"123","bot_qq":"456"})
        self.service=RuntimeService(self.store);self.persona=self.service.create_persona(generic_persona())["id"];self.service.use_persona(self.persona)
        self.reply="A"*901+"B"*500
        self.fake=Fake(self.reply);self.engine=ChatEngine(self.store,self.config,self.fake);self.delivery=DeliveryService(self.store)
        self.turn=await self.engine.submit("qq","question",identifier(),origin={"sender":"123","bot":"456"})
        await self.engine.wait();self.turn=self.engine.get(self.turn["id"])
        self.stream=self.delivery.by_batch(self.turn["reply_batch"]["id"])
        self.addAsyncCleanup(self.engine.wait)

    def claim(self, part=None):
        stream=self.delivery.get(self.stream["id"])
        part=part or stream["parts"][0]
        return self.delivery.claim(stream["id"],part["id"],stream["revision"],identifier(),origin={"sender":"123","bot":"456"})

    def result(self, part, claim, state="sent", message_id="message-1"):
        return self.delivery.result(self.stream["id"],part["id"],claim["claim_token"],state,
            claim["part_revision"],identifier(),platform_message_id=message_id if state=="sent" else None,
            error=None if state=="sent" else "timeout",origin={"sender":"123","bot":"456"})

    async def test_parts_are_durable_in_reply_completion_transaction(self):
        parts=self.stream["parts"]
        self.assertEqual([len(part["text"]) for part in parts],[450,450,450,51])
        self.assertEqual("".join(part["text"] for part in parts),self.reply)
        self.assertEqual([part["part_index"] for part in parts],[1,2,3,4])
        self.assertEqual({part["semantic_index"] for part in parts},{1})
        self.assertTrue(all(len(part["text_hash"])==64 for part in parts))

    async def test_claim_is_single_owner_and_result_requires_current_token(self):
        part=self.stream["parts"][0];claim=self.claim(part)
        self.assertTrue(claim["claimed"])
        with self.assertRaisesRegex(RuntimeErrorBase,"version_conflict"):
            self.delivery.claim(self.stream["id"],part["id"],self.stream["revision"],identifier(),origin={"sender":"123","bot":"456"})
        with self.assertRaisesRegex(RuntimeErrorBase,"claim_token_mismatch"):
            self.delivery.result(self.stream["id"],part["id"],"stale-token","sent",claim["part_revision"],identifier(),origin={"sender":"123","bot":"456"})
        self.result(part,claim)
        updated=self.delivery.get(self.stream["id"])
        self.assertEqual(updated["parts"][0]["platform_message_id"],"message-1")
        self.assertEqual(updated["parts"][0]["state"],"sent")

    async def test_concurrent_claimers_cannot_claim_the_same_part(self):
        part=self.stream["parts"][0];revision=self.stream["revision"]
        def attempt():
            try:
                return self.delivery.claim(self.stream["id"],part["id"],revision,identifier(),origin={"sender":"123","bot":"456"})
            except RuntimeErrorBase as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sum(isinstance(result,dict) and result.get("claimed") for result in results),1)
        self.assertEqual(sum(result=="version_conflict" for result in results),1)

    async def test_paused_stream_can_claim_after_release(self):
        first=self.stream["parts"][0];claim=self.claim(first);self.result(first,claim,"uncertain")
        view=self.delivery.get(self.stream["id"])
        self.delivery.resolve(view["id"],first["id"],"resolve",view["revision"],identifier(),
            persona_id=self.persona,reason="verified",allow_continue=False)
        view=self.delivery.get(self.stream["id"]);next_part=view["parts"][1]
        first_operation=identifier()
        held=self.delivery.claim(view["id"],next_part["id"],view["revision"],first_operation,
            origin={"sender":"123","bot":"456"})
        self.assertFalse(held["claimed"]);self.assertEqual(held["reason"],"admin_hold")
        self.delivery.resolve(view["id"],first["id"],"release",view["revision"],identifier(),persona_id=self.persona)
        resumed=self.delivery.get(view["id"])
        claimed=self.delivery.claim(resumed["id"],next_part["id"],resumed["revision"],identifier(),
            origin={"sender":"123","bot":"456"})
        self.assertTrue(claimed["claimed"])

    async def test_bridge_never_replays_a_sending_part(self):
        bridge=QQBridge(self.paths);stream=dict(self.stream);stream["parts"]=[dict(part) for part in stream["parts"]]
        stream["parts"][0]["state"]="sending"
        async def request(method,path,value=None):
            raise AssertionError("in-flight delivery must not request another claim")
        bridge.request=request;sent=[]
        async def send(text):sent.append(text);return "unexpected"
        self.assertFalse(await bridge.deliver_stream(stream,send))
        self.assertEqual(sent,[])

    async def test_same_target_turns_share_ordered_stream(self):
        second=await self.engine.submit("qq","second question",identifier(),origin={"sender":"123","bot":"456"})
        await self.engine.wait();second=self.engine.get(second["id"])
        second_stream=self.delivery.by_batch(second["reply_batch"]["id"])
        self.assertEqual(second_stream["id"],self.stream["id"])
        sequences=[part["stream_seq"] for part in second_stream["parts"]]
        self.assertEqual(sequences,sorted(sequences));self.assertEqual(len(sequences),8)
        self.assertEqual(len(set(sequences)),8)

    async def test_v1_v2_origin_serialization_shares_one_stream(self):
        from persona_runtime.ingress import IngressService
        # Upgrade an already-created, unreleased schema-10 trigger in place.
        with self.store.connection(write=True) as conn:
            trigger=conn.execute("SELECT sql FROM sqlite_master WHERE name='reply_delivery_part_scope_insert'").fetchone()[0]
            old=trigger.replace("AND json_extract(t.origin_json,'$.sender')=json_extract(s.origin_json,'$.sender')\n     AND json_extract(t.origin_json,'$.bot')=json_extract(s.origin_json,'$.bot')",'AND t.origin_json=s.origin_json')
            conn.execute('DROP TRIGGER reply_delivery_part_scope_insert');conn.execute(old)
        self.store.initialize();self.store.initialize()
        ingress=IngressService(self.engine)
        receipt=ingress.accept({'channel':'qq','sender':'123','bot':'456','platform_message_id':'synthetic-test',
            'request_id':identifier(),'persona_id':self.persona,'text':'second v2 input'})
        await self.engine.wait();turn=self.engine.get(receipt['turn_id'])
        self.assertEqual(turn['status'],'completed')
        self.assertEqual(self.delivery.by_batch(turn['reply_batch']['id'])['id'],self.stream['id'])
        self.assertEqual(len(self.delivery.get(self.stream['id'])['parts']),8)

    async def test_uncertain_blocks_remaining_and_never_auto_retries(self):
        first=self.stream["parts"][0];claim=self.claim(first);self.result(first,claim)
        view=self.delivery.get(self.stream["id"]);second=view["parts"][1]
        claim=self.delivery.claim(view["id"],second["id"],view["revision"],identifier(),origin={"sender":"123","bot":"456"})
        self.result(second,claim,"uncertain")
        view=self.delivery.get(self.stream["id"])
        self.assertEqual(view["state"],"blocked");self.assertEqual(view["parts"][0]["state"],"sent")
        self.assertEqual(view["parts"][1]["state"],"uncertain");self.assertEqual(view["parts"][2]["state"],"pending")
        blocked=self.delivery.claim(view["id"],view["parts"][2]["id"],view["revision"],identifier(),origin={"sender":"123","bot":"456"})
        self.assertFalse(blocked["claimed"]);self.assertEqual(blocked["reason"],"uncertain")
        result=self.delivery.resolve(view["id"],second["id"],"retry",view["revision"],identifier(),persona_id=self.persona,reason="manual confirmation; may duplicate")
        self.assertEqual(result["state"],"open");self.assertEqual(result["attempt"],1)
        next_claim=self.claim(view["parts"][1]);self.assertEqual(next_claim["attempt"],2)
        attempts=self.delivery.get(view["id"])["parts"][1]["attempts"]
        self.assertEqual([row["state"] for row in attempts],["uncertain","sending"])

    async def test_restart_marks_sending_uncertain_and_export_redacts_claim_token(self):
        part=self.stream["parts"][0];claim=self.claim(part)
        self.assertTrue(claim["claim_token"])
        self.delivery.recover()
        view=self.delivery.get(self.stream["id"])
        self.assertEqual(view["parts"][0]["state"],"uncertain")
        self.assertNotIn("claim_token",view["parts"][0])

    async def test_skip_only_remainder_of_current_reply_batch(self):
        part=self.stream["parts"][0];claim=self.claim(part);self.result(part,claim)
        view=self.delivery.get(self.stream["id"]);second=view["parts"][1]
        claim=self.delivery.claim(view["id"],second["id"],view["revision"],identifier(),origin={"sender":"123","bot":"456"})
        self.result(second,claim,"failed")
        view=self.delivery.get(self.stream["id"])
        skipped=self.delivery.resolve(view["id"],second["id"],"skip",view["revision"],identifier(),persona_id=self.persona,reason="stop remaining")
        self.assertEqual(skipped["state"],"skipped")
        states=[part["state"] for part in self.delivery.get(view["id"])["parts"]]
        self.assertEqual(states,["sent","skipped","skipped","skipped"])

    async def test_exports_include_ledger_without_claim_token(self):
        part=self.stream["parts"][0];claim=self.claim(part)
        exported=self.service.export_persona()
        payload=Path(exported["path"]).read_text(encoding="utf-8")
        self.assertIn('"format_version": 12',payload)
        self.assertNotIn(claim["claim_token"],payload)
        self.assertIn('"reply_delivery_parts"',payload)
        self.assertIn('"reply_delivery_attempts"',payload)

    async def test_bridge_records_platform_ids_and_stops_on_uncertain(self):
        bridge=QQBridge(self.paths);origin={"sender":"123","bot":"456"}
        async def request(method,path,value=None):
            if method=="GET" and path.startswith('/v2/delivery/streams/'):
                return self.delivery.get(path.rsplit('/',1)[1],origin=origin)
            if method=="GET" and path.startswith('/v2/replies/'):
                return self.delivery.by_batch(path.split('/')[3])
            batch_id=path.split('/')[3];part_id=path.split('/')[5]
            stream=self.delivery.by_batch(batch_id)
            if path.endswith('/claim'):
                return self.delivery.claim(stream['id'],part_id,value['expected_revision'],value['operation_id'],origin=origin)
            if path.endswith('/result'):
                return self.delivery.result(stream['id'],part_id,value['claim_token'],value['state'],value['expected_revision'],
                    value['operation_id'],platform_message_id=value.get('platform_message_id'),error=value.get('error'),origin=origin)
            raise AssertionError(path)
        bridge.request=request
        sent=[]
        async def send(text):
            sent.append(text);return {"data":{"message_id":f"platform-{len(sent)}"}}
        self.assertTrue(await bridge.deliver_stream(self.stream,send))
        final=self.delivery.get(self.stream['id'])
        self.assertTrue(all(part['state']=='sent' for part in final['parts']))
        self.assertEqual([part['platform_message_id'] for part in final['parts']],['platform-1','platform-2','platform-3','platform-4'])

    async def test_schema9_to10_upgrade_is_transactional_and_idempotent(self):
        path=RuntimePaths(self.paths.home/'v9');path.initialize();store=Store(path)
        db=sqlite3.connect(store.path)
        for schema in (SCHEMA,SCHEMA2,SCHEMA3,SCHEMA4,SCHEMA5,SCHEMA6,SCHEMA7,SCHEMA8,SCHEMA9):execute_schema(db,schema)
        db.execute('INSERT INTO users VALUES (?,?,?)',('00000000-0000-4000-8000-000000000001','legacy','time'))
        db.execute('PRAGMA user_version=9');db.commit();db.close()
        original=execute_schema
        def fail_on_m63(conn,schema):
            original(conn,schema)
            if schema==SCHEMA10:raise RuntimeErrorBase('injected schema10 failure')
        with patch('persona_runtime.storage.execute_schema',side_effect=fail_on_m63),self.assertRaisesRegex(RuntimeErrorBase,'schema10'):
            store.initialize()
        db=sqlite3.connect(store.path)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='reply_delivery_parts'").fetchone());db.close()
        store.initialize();store.initialize()
        self.assertEqual(store.status()['schema_version'],15)
        self.assertTrue(list((path.home/'backups').glob('before-m63-*.sqlite3')))

    async def test_v2_api_bridge_scope_admin_resolution_and_legacy_rejection(self):
        app=create_app(self.paths,self.fake)
        async with TestServer(app) as server,ClientSession() as client:
            bridge={'Authorization':'Bearer '+self.config.token('bridge')}
            admin={'Authorization':'Bearer '+self.config.token()}
            async with client.post(server.make_url('/v1/manage'),headers=admin,
                json={'action':'delivery.pending','args':{}}) as response:
                self.assertEqual(response.status,200)
                self.assertEqual(len(await response.json()),len(self.stream['parts']))
            async with client.get(server.make_url('/v2/replies/'+self.turn['reply_batch']['id']+'/parts'),headers=bridge) as response:
                self.assertEqual(response.status,200);parts=(await response.json())['parts']
            claim_data={'expected_revision':self.stream['revision'],'operation_id':identifier()}
            url=server.make_url('/v2/replies/'+self.turn['reply_batch']['id']+'/parts/'+parts[0]['id']+'/claim')
            async with client.post(url,headers=bridge,json=claim_data) as response:
                self.assertEqual(response.status,200);claim=await response.json()
            async with client.post(server.make_url('/v1/requests/'+self.turn['id']+'/delivery'),headers=admin,json={'action':'claim'}) as response:
                self.assertEqual(response.status,400);self.assertEqual((await response.json())['error'],'m63_delivery_protocol_required')
            resolve_url=server.make_url('/v2/replies/'+self.turn['reply_batch']['id']+'/parts/'+parts[0]['id']+'/resolve')
            async with client.post(resolve_url,headers=bridge,json={'action':'skip','expected_revision':parts[0]['revision'],'operation_id':identifier(),'persona_id':self.persona}) as response:
                self.assertEqual(response.status,403)
            bad_origin={'Authorization':'Bearer '+self.config.token('bridge')}
            original=self.config.load();self.config.update({'owner_qq':'789'})
            async with client.get(server.make_url('/v2/delivery/streams/'+self.stream['id']),headers=bad_origin) as response:
                self.assertEqual(response.status,400)
            self.config.update({'owner_qq':original['owner_qq']})


if __name__ == "__main__":
    unittest.main()
