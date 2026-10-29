"""M7.4 deterministic protocol, timing, durable cancellation and compatibility gates."""
import asyncio
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.delivery import DeliveryService
from persona_runtime.ingress import IngressService
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.reply_batch import validate_reply, ReplySchemaError
from persona_runtime.reply_transport import render_parts, preview
from persona_runtime.reply_service import ReplyService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

CAPS=['reply_batch_v2','reply_rhythm_v1']


def natural(*texts):
    return json.dumps({'schema_version':2,'behavior_intent':'respond','messages':[
        {'content':t,'content_kind':'chat','cancel_if_new_input':i>0} for i,t in enumerate(texts or ('必要回答','可省略结束语'))]},ensure_ascii=False)


class ProtocolTests(unittest.TestCase):
    def test_v2_metadata_and_wait(self):
        batch=validate_reply(natural('好','补充','结束'),'natural_structured')
        self.assertEqual([s.wait_ms for s in batch.segments],[0,615,630])
        self.assertEqual(sum(s.wait_ms for s in validate_reply(natural('中'*100,'中'*100,'尾'),'natural_structured').segments),3600)

    def test_invalid_metadata_safe_fallback_all_bodies(self):
        for changes in ({'behavior_intent':'bad'}, {'schema_version':1,'segments':[{'text':'a','format':'bad'},{'text':'b','format':'plain'}]}):
            obj=json.loads(natural('a','b'));obj.update(changes)
            if obj['schema_version']==1:obj={k:v for k,v in obj.items() if k in ('schema_version','segments')}
            b=validate_reply(json.dumps(obj),'natural_structured')
            self.assertEqual(b.text,'a\n\nb');self.assertTrue(b.fallback_reason)
            self.assertEqual(len(b.segments),1);self.assertFalse(b.segments[0].cancel_if_new_input)

    def test_first_and_protected_kind_cannot_cancel(self):
        for index,kind in ((0,'chat'),(1,'code'),(1,'quote'),(1,'technical')):
            obj=json.loads(natural());obj['messages'][index].update(content_kind=kind,cancel_if_new_input=True)
            b=validate_reply(json.dumps(obj),'natural_structured')
            self.assertIsNotNone(b.fallback_reason);self.assertFalse(b.segments[0].cancel_if_new_input)

    def test_unsafe_body_never_salvages_partial(self):
        bad=[natural()[:-1],natural().replace('"schema_version": 2','"schema_version":2,"schema_version":2')]
        for field,value in [('content',''),('content','```py\na=1'),('content',None),('content','x'*8001),('extra','x')]:
            obj=json.loads(natural());obj['messages'][1][field]=value;bad.append(json.dumps(obj))
        for raw in bad:
            with self.subTest(raw=raw[:60]),self.assertRaises(ReplySchemaError):validate_reply(raw,'natural_structured')

    def test_capacity_and_no_fallback_bypass(self):
        for raw in (natural('x'*8000,'x'*4001),natural('a','b','c','d')):
            with self.assertRaises(ReplySchemaError):validate_reply(raw,'natural_structured')
        obj=json.loads(natural('x'*5000,'x'*4000));obj['behavior_intent']='bad'
        with self.assertRaises(ReplySchemaError):validate_reply(json.dumps(obj),'natural_structured')

    def test_legacy_contract_unchanged(self):
        self.assertEqual(validate_reply('hello','legacy_full').schema_version,1)
        raw='{"schema_version":1,"segments":[{"text":"ok","format":"plain"}]}'
        self.assertEqual(validate_reply(raw,'context_structured').text,'ok')
        with self.assertRaises(ReplySchemaError):validate_reply(natural(),'context_structured')

    def test_transport_reconstruction_and_atoms(self):
        url='https://example.org/path_a?value=12.345&v=0.7.4'
        text='前'*440+'\n'+url+'\n'+'后'*460
        rendered,parts=render_parts(text)
        self.assertEqual(''.join(parts),rendered);self.assertTrue(all(len(p)<=450 for p in parts))
        self.assertTrue(any(url in p for p in parts))
        _,parts=render_parts('中'*448+'123.456.789'+'中'*450)
        self.assertTrue(any('123.456.789' in p for p in parts))

    def test_code_lines_and_operators(self):
        code='\n'.join('print(2 ** 3) # '+str(i) for i in range(80))
        rendered,parts=render_parts('```python\n'+code+'\n```','code')
        self.assertEqual(rendered,code)
        for line in code.splitlines():self.assertTrue(any(line in p for p in parts))
        self.assertEqual(''.join(parts),code)
        bare='# heading is code\nx = 2 ** 3; y = 3 ** 2\n__all__ = ["x", "y"]'
        rendered,parts=render_parts(bare,'code')
        self.assertEqual(rendered,bare);self.assertEqual(''.join(parts),bare)
        for atom in ('v0.7.4-rc1','-123.45','1e-3'):
            _,parts=render_parts('中'*449+atom+'中'*460)
            self.assertTrue(any(atom in part for part in parts))

    def test_indivisible_failure(self):
        for text,kind in [('https://example.org/'+'x'*450,'chat'),('print("'+'x'*450+'")','code'),('1'*451,'technical')]:
            with self.assertRaisesRegex(ReplySchemaError,'indivisible'):render_parts(text,kind)

    def test_preview_no_model_or_timing_authority(self):
        self.assertEqual(preview(natural(),'natural_structured')['messages'][0]['wait_ms'],0)
        obj=json.loads(natural());obj['messages'][0]['delay']=100
        with self.assertRaises(ReplySchemaError):preview(json.dumps(obj),'natural_structured')


class Fake:
    def __init__(self):self.raw=natural();self.calls=0;self.gate=None
    async def complete(self,model,messages):
        self.calls+=1
        if self.gate:await self.gate.wait()
        return self.raw,{'total_tokens':3}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root,prefix='m74-');self.addCleanup(self.temp.cleanup)
        self.store=Store(RuntimePaths(Path(self.temp.name)));self.store.initialize()
        self.config=ConfigStore(self.store.paths);self.config.initialize()
        self.config.update({'model':{'base_url':'http://localhost:9/v1','name':'mock'},'bot_qq':'123','owner_qq':'456'})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.fake=Fake();self.engine=ChatEngine(self.store,self.config,self.fake);self.ingress=IngressService(self.engine)
        self.now=0.;self.delivery=DeliveryService(self.store,monotonic=lambda:self.now)
        self.mode('natural_structured');self.addAsyncCleanup(self.engine.wait)

    def mode(self,mode,channel='qq',**kw):
        p=self.ingress.current_policy(self.pid,channel)
        return self.ingress.update_policy(self.pid,channel,p['mode'],p['id'],identifier(),reply_mode=mode,**kw)

    def accept(self,text='hello',**kw):
        data={'channel':'qq','text':text,'request_id':identifier(),'bot':'123','sender':'456','platform_message_id':identifier(),**kw}
        if data['channel']=='desktop':
            for k in ('bot','sender','platform_message_id'):data.pop(k,None)
        return self.ingress.accept(data)

    async def make(self,**kw):
        r=self.accept(**kw);await self.engine.wait();t=self.engine.get(r['turn_id'])
        self.assertEqual(t['status'],'completed',t.get('error_code'))
        return t,self.delivery.by_batch(t['reply_batch']['id'])

    def claim(self,stream,index=0,**kw):
        current=self.delivery.get(stream['id']);part=current['parts'][index]
        return self.delivery.claim(current['id'],part['id'],current['revision'],identifier(),capabilities=CAPS,**kw)

    def sent(self,stream,index,claim):
        p=self.delivery.get(stream['id'])['parts'][index]
        return self.delivery.result(stream['id'],p['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),platform_message_id='out-'+str(index))

    async def test_timing_boundary_and_restart(self):
        t,s=await self.make();first=self.claim(s);self.sent(s,0,first)
        wait=self.claim(s,1);self.assertFalse(wait['claimed']);delay=wait['remaining_ms']
        self.now=(delay-1)/1000;self.assertFalse(self.claim(s,1)['claimed'])
        with patch('time.time',return_value=10**12):self.assertFalse(self.claim(s,1)['claimed'])
        self.delivery.recover();self.assertEqual(self.claim(s,1)['remaining_ms'],delay)
        self.now+=delay/1000;self.assertTrue(self.claim(s,1)['claimed'])

    async def test_result_rollback_does_not_reuse_uncommitted_deadline(self):
        _,s=await self.make();claim=self.claim(s)
        with patch.object(self.delivery,'_record',side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):self.sent(s,0,claim)
        self.now=100
        self.sent(s,0,claim)
        wait=self.claim(s,1)
        self.assertFalse(wait['claimed']);self.assertEqual(wait['remaining_ms'],660)

    async def test_real_acceptance_tool_reservations_and_artifact_sanitization(self):
        from scripts.acceptance_m74 import Acceptance,schedule
        from types import SimpleNamespace
        self.assertEqual(schedule(),schedule());self.assertEqual(len(schedule()),24)
        self.assertEqual(len({(r['case'],r['mode']) for r in schedule()}),24)
        a=Acceptance.__new__(Acceptance);a.store=self.store;a.cfg=self.config
        a.out=self.store.paths.home/'exports/acceptance';a.out.mkdir(parents=True,exist_ok=True)
        a.file=a.out/'real-checkpoint.json';a.state={'persona_id':self.pid,'calls':{},'model_hash':a.model_hash()}
        a.client=SimpleNamespace(manage=lambda *args,**kw:{'manifests':[{'context_id':'mock'}]})
        a.reserve('Q01','quality',{'request_id':'stable'})
        with self.assertRaisesRegex(RuntimeError,'Already reserved'):a.reserve('Q01','quality')
        turn,_=await self.make();a.record('Q01',turn,'qq')
        evidence=(a.out/'Q01.json').read_text(encoding='utf-8')
        self.assertNotIn('claim_token',evidence);self.assertNotIn('encrypted_key',evidence)
        self.assertEqual(a.state['calls']['Q01']['status'],'completed')
        a.state['calls'].update({f'x{i}':{} for i in range(29)})
        with self.assertRaisesRegex(RuntimeError,'Thirty-call'):a.reserve('Q02','quality')

    async def test_cancel_pending_only_and_no_duplicate_trigger(self):
        t,s=await self.make();self.sent(s,0,self.claim(s))
        r=self.accept(text='new');await self.engine.wait()
        old=self.delivery.by_batch(t['reply_batch']['id'])['parts'][:2]
        self.assertEqual([p['state'] for p in old],['sent','skipped']);self.assertEqual(old[1]['cancel_reason'],'new_input')
        with self.store.connection() as c:
            trigger=c.execute('SELECT event_id FROM ingress_receipts WHERE id=?',(r['receipt_id'],)).fetchone()[0]
            self.assertEqual(old[1]['trigger_event_id'],trigger)
        # The retried source returns its receipt without cancelling newly generated tails.
        with self.store.connection() as c:
            payload=json.loads(c.execute('SELECT payload_json FROM raw_events WHERE event_id=?',(trigger,)).fetchone()[0])
        before=self.delivery.get(s['id']);self.ingress.accept(payload);after=self.delivery.get(s['id'])
        self.assertEqual(before['revision'],after['revision'])

    async def test_new_input_before_generation_commit(self):
        self.fake.gate=asyncio.Event();a=self.accept()
        for _ in range(100):
            if self.fake.calls:break
            await asyncio.sleep(.01)
        self.accept(text='new');self.fake.gate.set();await self.engine.wait()
        t=self.engine.get(a['turn_id']);parts=self.delivery.by_batch(t['reply_batch']['id'])['parts']
        self.assertEqual(parts[1]['state'],'skipped');self.assertEqual(parts[0]['state'],'pending')
        self.assertEqual(self.fake.calls,2)

    async def test_claimed_group_not_cancelled(self):
        t,s=await self.make();self.sent(s,0,self.claim(s));self.now=3
        claim=self.claim(s,1);self.assertTrue(claim['claimed']);self.accept(text='new');await self.engine.wait()
        self.assertEqual(self.delivery.get(s['id'])['parts'][1]['state'],'sending')
        self.delivery.recover();self.assertEqual(self.delivery.get(s['id'])['parts'][1]['state'],'uncertain')
        self.assertFalse(self.claim(s,1)['claimed'])

    async def test_cancel_claim_race(self):
        t,s=await self.make();self.sent(s,0,self.claim(s));self.now=3
        current=self.delivery.get(s['id']);part=current['parts'][1]
        def claim():
            try:return self.delivery.claim(s['id'],part['id'],current['revision'],identifier(),capabilities=CAPS)
            except RuntimeErrorBase:return None
        def cancel():
            try:return self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],current['revision'],identifier())
            except RuntimeErrorBase:return None
        with ThreadPoolExecutor(2) as pool:
            a=pool.submit(claim);b=pool.submit(cancel);a.result();b.result()
        part=self.delivery.get(s['id'])['parts'][1]
        self.assertIn(part['state'],('sending','skipped'));self.assertEqual(part['attempt'],int(part['state']=='sending'))

    async def test_capability_and_manual_idempotency(self):
        t,s=await self.make();p=s['parts'][0]
        with self.assertRaisesRegex(RuntimeErrorBase,'capability'):
            self.delivery.claim(s['id'],p['id'],s['revision'],identifier())
        op=identifier();result=self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],s['revision'],op)
        self.assertEqual(result,self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],s['revision'],op))
        with self.assertRaisesRegex(RuntimeErrorBase,'conflict'):self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],999,op)

    async def test_scope_and_preview_readonly(self):
        t,s=await self.make();before=self.store.status()['counts']
        preview=ReplyService(self.store).preview(self.pid,batch_id=t['reply_batch']['id'])
        self.assertEqual(preview['schema_version'],2);self.assertEqual(before,self.store.status()['counts'])
        self.accept(text='desktop',channel='desktop');await self.engine.wait()
        self.assertEqual(self.delivery.get(s['id'])['parts'][1]['state'],'pending')
        other=self.service.create_persona(generic_persona())['id']
        with self.assertRaises(RuntimeErrorBase):ReplyService(self.store).preview(other,batch_id=t['reply_batch']['id'])

    async def test_fallback_one_call_usage_and_export(self):
        self.fake.raw=self.fake.raw.replace('"respond"','"nonsense"')
        t,s=await self.make();self.assertEqual(self.fake.calls,1);self.assertTrue(t['reply_batch']['fallback_reason'])
        self.assertEqual(len(s['parts']),1);self.assertEqual(t['usage']['total_tokens'],3)
        payload=json.loads(Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(payload['format_version'],11);self.assertIn('wait_ms',payload['reply_segments'][0])
        self.assertNotIn('claim_token',payload['reply_delivery_parts'][0])

    async def test_unsafe_batch_fails_before_any_send(self):
        self.fake.raw=natural('first','https://example.org/'+'x'*451)
        r=self.accept();await self.engine.wait();t=self.engine.get(r['turn_id'])
        self.assertEqual(t['status'],'failed');self.assertEqual(t['error_code'],'qq_indivisible_content_exceeds_limit')
        self.assertEqual(self.fake.calls,1);self.assertEqual(t['usage']['total_tokens'],3)
        with self.store.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM reply_delivery_parts').fetchone()[0],0)

    async def test_saved_snapshot_and_explicit_switch_boundary(self):
        p=self.ingress.current_policy(self.pid,'qq')
        self.ingress.update_policy(self.pid,'qq','natural_batch',p['id'],identifier())
        r=self.accept()
        with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch'):self.mode('legacy_full')
        self.mode('legacy_full',pending_action='seal');await self.engine.wait()
        b=self.ingress.get(r['batch_id']);t=self.engine.get(b['turn_id'])
        self.assertEqual(t['reply_batch']['mode'],'natural_structured')
        self.assertEqual(json.loads(t['expression_snapshot'])['mode'],'natural_structured')

    async def test_cancelled_text_excluded_from_archive_and_sealing_sources(self):
        from persona_runtime.archive import read_exact,scope
        from persona_runtime.sessions import blockers,SessionService
        t,s=await self.make();self.sent(s,0,self.claim(s))
        with self.store.connection() as c:self.assertTrue(blockers(c,t['session_id']))
        view=self.delivery.get(s['id']);self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],view['revision'],identifier())
        with self.store.connection() as c:
            result=read_exact(c,scope(self.pid,'qq','123','456'),{'kind':'turn','id':t['id']})
            self.assertEqual(result['status'],'found')
            self.assertEqual([r['text'] for r in result['replies']],['必要回答'])
            self.assertFalse(blockers(c,t['session_id']))
            self.assertEqual(c.execute('SELECT text FROM events WHERE id=?',(t['reply_event_id'],)).fetchone()[0],'必要回答\n\n可省略结束语')
        sessions=SessionService(self.store);args={'session_id':t['session_id'],'persona_id':self.pid,'channel':'qq','bot':'123','peer':'456'}
        current=sessions.show(**args);sessions.seal(**args,expected_revision=current['revision'],operation_id=identifier())
        replay=sessions.replay(**args)
        self.assertNotIn('可省略结束语',json.dumps(replay,ensure_ascii=False))

    async def test_partial_semantic_group_keeps_all_remaining_parts(self):
        self.fake.raw=natural('core','中'*901)
        t,s=await self.make();self.sent(s,0,self.claim(s));self.now=3
        first_tail=self.claim(s,1);self.sent(s,1,first_tail)
        view=self.delivery.get(s['id']);result=self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],view['revision'],identifier())
        self.assertEqual(result['cancelled'],[])
        self.assertEqual([p['state'] for p in self.delivery.get(s['id'])['parts']],['sent','sent','pending','pending'])
        self.assertTrue(self.claim(s,2)['claimed'])  # No inter-part artificial wait.

    async def test_admin_hold_not_released_by_optional_cancellation(self):
        t,s=await self.make();claim=self.claim(s);p=s['parts'][0]
        self.delivery.result(s['id'],p['id'],claim['claim_token'],'uncertain',claim['part_revision'],identifier())
        v=self.delivery.get(s['id']);self.delivery.resolve(s['id'],p['id'],'resolve',v['revision'],identifier(),allow_continue=False)
        v=self.delivery.get(s['id']);self.delivery.cancel_optional(self.pid,t['reply_batch']['id'],v['revision'],identifier())
        self.assertEqual(self.delivery.get(s['id'])['blocked_reason'],'admin_hold')

    async def test_http_capability_and_preview_permissions(self):
        from aiohttp.test_utils import TestServer
        from aiohttp import ClientSession
        from persona_runtime.server import create_app
        t,s=await self.make();server=TestServer(create_app(self.store.paths,self.fake));await server.start_server()
        try:
            async with ClientSession() as client:
                base=str(server.make_url('')).rstrip('/')
                for role in ('bridge','admin'):
                    headers={'Authorization':'Bearer '+self.config.token(role)}
                    async with client.post(base+'/v1/manage',headers=headers,json={'action':'reply.preview','args':{'persona_id':self.pid,'raw':natural()}}) as response:
                        self.assertEqual(response.status,403 if role=='bridge' else 200)
                part=s['parts'][0];path=base+f"/v2/replies/{part['reply_batch_id']}/parts/{part['id']}/claim"
                payload={'expected_revision':self.delivery.get(s['id'])['revision'],'operation_id':identifier()}
                async with client.post(path,headers=headers,json=payload) as response:self.assertNotEqual(response.status,200)
                payload.update(capabilities=CAPS,operation_id=identifier())
                async with client.post(path,headers=headers,json=payload) as response:self.assertTrue((await response.json())['claimed'])
        finally:await server.close()

    def test_migration_atomic_and_preserves_original_columns(self):
        # Construct a real v13 database by stopping immediately before v14 migration.
        paths=RuntimePaths(Path(self.temp.name)/'v13');legacy=Store(paths)
        import persona_runtime.storage as storage
        with patch.object(storage,'SCHEMA_VERSION',13),patch('persona_runtime.schema_m74.migrate'):
            legacy.initialize()
        connection=sqlite3.connect(legacy.path)
        before={r[0]:list(connection.execute('SELECT * FROM '+r[0])) for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        connection.close()
        from persona_runtime.schema_m74 import migrate
        def fail(c):migrate(c);raise OSError('injected disk failure')
        with patch('persona_runtime.schema_m74.migrate',side_effect=fail),self.assertRaises(OSError):legacy.initialize()
        with closing(sqlite3.connect(legacy.path)) as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],13)
            self.assertFalse(any(r[1]=='expression_snapshot' for r in c.execute('PRAGMA table_info(turns)')))
        legacy.initialize();legacy.initialize()
        with legacy.connection() as c:
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            for table,rows in before.items():
                if table=='sqlite_sequence':continue
                actual=[tuple(r)[:len(rows[0])] for r in c.execute('SELECT * FROM '+table)] if rows else []
                self.assertEqual(actual,rows,table)
