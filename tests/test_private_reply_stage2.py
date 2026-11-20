"""Ordinary candidate integration uses opaque numeric source material and isolated transports."""
import asyncio
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.models import PersonaSpec, RuntimeErrorBase, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime import session_privacy
from persona_runtime.expression_utterance_publication import SpeechPublication
from persona_runtime.import_memory_scope import source_group
from persona_runtime.archive import turn_scope


class LocalProviders:
    version='2'
    def __init__(self):
        self.calls=[]
        self.bodies=[]
        self.selection='recent'
        self.wait=False
        self.target=None
        self.body='801。'*45

    async def choose(self,node,attempt,state,options):
        self.calls.append((node,attempt,state))
        values={'J1':'wait' if self.wait else self.target or 'batch','J2.action':'respond','J2.task':'closed','J2.style':'v2',
                'J2.stance':'0','J3':self.selection,'J6.mode':'chat','J6.coverage':'all','J6.ending':'stop'}
        return values[node],{'total_tokens':1}

    async def reply(self,attempt,packet):
        self.bodies.append((attempt,packet))
        return self.body,{'total_tokens':1}


class OrdinaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.paths=RuntimePaths(Path(temp.name))
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.config.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro','api_key':'702'}})
        self.store=Store(self.paths);self.store.initialize()
        service=RuntimeService(self.store)
        self.pid=service.create_persona(PersonaSpec(1,'700','701',['702'],'703',['704'],'705'))['id']
        service.use_persona(self.pid)
        args=(self.store,self.config,'jev','https://example.invalid/v1','jev-1.14','706')
        preview=session_privacy.connection(*args)
        session_privacy.connection(*args,confirm=preview['confirmation'])
        self.providers=LocalProviders()
        self.reopen()
        self.sid=self.chat.session()['session_id']

    def reopen(self):
        self.chat=ChatEngine(self.store,self.config,private_chat_providers=self.providers)
        self.out=SpeechPublication(self.chat.expression.outlet)

    async def submit(self,text='707'):
        turn=await self.chat.submit('desktop',text,identifier(),self.sid,persona_id=self.pid)
        await self.chat.wait()
        return turn['id']

    def plan(self,tid):
        units=self.out.list(self.pid,self.sid,tid)
        self.assertEqual(len(units),1,self.chat.get(tid))
        uid=units[0]['id']
        return uid,self.out.plan(self.pid,self.sid,uid)

    def paint(self,uid,plan,index,state='presented'):
        rid=identifier();presenter='webview-v2.'+rid
        item=self.out.offer(self.pid,self.sid,uid,plan['id'],plan['revision'],index,presenter)
        self.assertLessEqual(len(item['body']),75)
        self.out.acknowledge(self.pid,self.sid,uid,item['id'],item['lease_id'],presenter,rid,item['body_hash'],
                             state,webview=True,publication_id=plan['id'],revision=plan['revision'],ordinal=index)
        return item['body']

    async def test_complete_visible_history_and_memory_source(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        self.assertIsNone(self.chat.get(tid)['reply_event_id'])
        body=''.join(self.paint(uid,plan,i) for i in range(len(plan['items'])))
        self.assertEqual(body,self.providers.body)
        with self.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            data=source_group(db,turn_scope(db,turn),turn)
            self.assertEqual([r['text'] for r in data['replies']],[body])
        second=await self.submit('708')
        self.plan(second)
        packet=self.providers.bodies[-1][1]
        self.assertIn(body,[e['text'] for e in packet['evidence']])
        self.assertNotIn('tools',packet)

    async def test_cold_plan_starts_one_writer_and_never_reviews_body(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        self.assertEqual(len(self.providers.bodies),1)
        self.assertIsNone(self.providers.bodies[0][1]['repair_reason'])
        self.assertNotIn('J7',[node for node,*_ in self.providers.calls])
        self.reopen()
        self.chat.recover()
        await self.chat.retry(tid)
        self.assertEqual(len(self.providers.bodies),1)
        self.assertEqual(self.plan(tid)[0],uid)
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        self.assertEqual(self.chat.get(tid)['status'],'completed')

    async def test_restart_consumes_saved_review_without_calling_again(self):
        from persona_runtime.ingress import IngressService
        with patch.object(self.chat.private_reply.ordinary,'publish',side_effect=asyncio.CancelledError()):
            tid=await self.submit()
        self.assertFalse(self.out.list(self.pid,self.sid,tid))
        before=len(self.providers.calls)
        self.reopen();self.chat.recover()
        IngressService(self.chat).schedule_committed()
        await self.chat.wait()
        uid,plan=self.plan(tid)
        self.assertEqual(len(self.providers.calls),before)
        self.assertEqual(len(self.providers.bodies),1)
        for i in range(len(plan['items'])):self.paint(uid,plan,i)

    async def test_incomplete_proof_cannot_dispatch_and_stale_source_stops_offer(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        with self.store.connection(write=True) as db:
            root,base=self.chat.private_reply._binding(db,tid,'main')
            with self.assertRaisesRegex(RuntimeErrorBase,'reply_decision_required'):
                self.chat.private_reply.ordinary.extend_contract(db,root,base,{'draft':0})
            db.execute("INSERT INTO source_exclusions SELECT id,persona_id,user_id,'withdrawn',created_at FROM events WHERE id=?",
                       (root['user_event_id'],))
        offer=self.out.offer(self.pid,self.sid,uid,plan['id'],plan['revision'],0,'908')
        self.assertEqual(offer['state'],'stopped')
        self.assertIsNone(self.chat.get(tid)['reply'])

    async def test_retired_unknown_review_does_not_block_saved_body(self):
        from persona_runtime.ingress import IngressService
        with patch.object(self.chat.private_reply.ordinary,'publish',side_effect=asyncio.CancelledError()):
            tid=await self.submit()
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO private_reply_nodes VALUES(?,0,'J7',?,?,'unknown',NULL,NULL)",
                       (tid,identifier(),'archived'))
        before=len(self.providers.calls)
        self.reopen();self.chat.recover()
        IngressService(self.chat).schedule_committed()
        await self.chat.wait()
        uid,plan=self.plan(tid)
        self.assertEqual(len(self.providers.calls),before)
        self.assertEqual(len(self.providers.bodies),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT state FROM private_reply_nodes WHERE turn_id=? AND node='J7'",
                                        (tid,)).fetchone()[0],'unknown')
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        self.assertEqual(self.chat.get(tid)['status'],'completed')

    async def test_wait_skips_writer_and_review(self):
        self.providers.wait=True
        tid=await self.submit()
        self.assertEqual(self.chat.get(tid)['status'],'completed')
        self.assertEqual([c[0] for c in self.providers.calls],['J1'])
        self.assertFalse(self.providers.bodies)

    async def test_j2_freezes_a_reviewed_style_before_writing(self):
        self.providers.styles={self.pid:{'v2':'919'}}
        tid=await self.submit()
        self.plan(tid)
        self.assertEqual(self.providers.bodies[0][1]['style'],'919')
        with self.store.connection() as db:
            proof=json.loads(db.execute("SELECT result_json FROM private_reply_nodes WHERE node='handoff'").fetchone()[0])
            self.assertIn('J2.style',proof['node_receipts'])

    async def test_unknown_fragment_keeps_only_confirmed_prefix(self):
        tid=await self.submit()
        uid,plan=self.plan(tid)
        first=self.paint(uid,plan,0)
        self.paint(uid,plan,1,'unknown')
        self.assertEqual(self.chat.get(tid)['reply'],first)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE role=\'persona\'').fetchone()[0],1)

    async def test_invalid_judge_and_missing_adapter(self):
        self.providers.selection='invalid'
        tid=await self.submit()
        self.assertFalse(self.providers.bodies)
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_decision_required')
        self.chat=ChatEngine(self.store,self.config)
        await self.chat.retry(tid)
        self.assertFalse(self.out.list(self.pid,self.sid,tid))

    def consent(self,enable):
        state=session_privacy.status(self.store,self.config,self.pid,self.sid)
        return session_privacy.update(self.store,self.config,self.pid,self.sid,enable,state['revision'],
                                      state['recipient_hash'],identifier(),state['protocol'])

    async def test_imported_explicit_reference_under_session_consent(self):
        from persona_runtime.import_history import restore
        from persona_runtime.ingress import IngressService
        from persona_runtime.desktop_recovery import intent_binding
        self.consent(True)
        with self.store.connection(write=True) as db:
            restore(db,self.pid,'801','A',[{'id':'81','role':'B','text':'811','original':'811','date':'2025-01-01',
                    'speaker':'802','line':1,'start':0,'end':3}],meta={'version':2,'withdrawn':False,
                    'timezone_offset':480,'self_role':'B','providers':{},'organize':False})
            eid=db.execute("SELECT id FROM events WHERE id LIKE 'imported.%'").fetchone()[0]
        self.providers.target=eid
        ingress=IngressService(self.chat)
        ident=identifier()
        payload={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','text':'808','request_id':ident,
                 'reference':{'kind':'event','id':eid}}
        with self.store.connection() as db:intent_binding(db,self.store,self.config,payload)
        receipt=ingress.accept(payload)
        with self.store.connection() as db:batch=db.execute('SELECT revision,state FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()
        if batch['state']=='collecting':
            ingress.action(receipt['batch_id'],'seal',batch['revision'],identifier(),self.pid,self.sid)
        await self.chat.wait()
        tid=ingress.lookup('desktop',ident)['turn_id']
        uid,plan=self.plan(tid)
        self.assertEqual(self.providers.bodies[0][1]['targets'][0]['text'],'811')
        self.consent(False)
        offer=self.out.offer(self.pid,self.sid,uid,plan['id'],plan['revision'],0,'801')
        self.assertEqual(offer['state'],'stopped')

    async def test_automatic_memory_reviews_visible_source_and_is_used_next_turn(self):
        from tests.test_m8d import Model
        from persona_runtime.organization import OrganizationEngine
        self.config.update({'organization':{'reuse_chat':True}})
        self.consent(True)
        memory=Model()
        org=OrganizationEngine(self.store,self.config,memory,self.chat)
        policy=org.managed.current_policy(self.pid)
        org.managed.update_policy(self.pid,policy['data']|{'memory_automation':True},policy['id'],self.pid,identifier())
        tid=await self.submit('751')
        uid,plan=self.plan(tid)
        org.automatic.tick(force=True)
        self.assertFalse(await org.run_next())
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        for _ in range(5):
            org.automatic.tick(force=True)
            if not await org.run_next():break
        self.assertEqual(len(memory.calls),2)
        with self.store.connection() as db:
            facts=db.execute("SELECT id,text FROM memories WHERE status='active'").fetchall()
            self.assertEqual([f['text'] for f in facts],['751'])
            for row in db.execute('SELECT payload_json FROM organization_tasks'):
                p=json.loads(row[0])
                self.assertEqual(p['automatic_memory']['session_privacy_revision'],1)
                self.assertEqual([e['text'] for e in p['events'] if e['role']!='user'],[self.providers.body])
        self.providers.selection='memory'
        uid,plan=self.plan(await self.submit('752'))
        self.assertEqual(self.providers.bodies[-1][1]['evidence'][0]['id'],facts[0]['id'])
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        self.sid=self.chat.session(fresh=True)['session_id']
        self.plan(await self.submit('753'))
        self.assertEqual(self.providers.bodies[-1][1]['evidence'],[])
        self.assertEqual(self.providers.bodies[-1][1]['dialogue'],[])

    async def test_planner_unknown_is_not_replayed(self):
        original=self.providers.choose
        async def choose(node,*args):
            if node=='J3':
                raise TimeoutError()
            return await original(node,*args)
        self.providers.choose=choose
        tid=await self.submit()
        self.reopen();self.chat.recover()
        self.providers.choose=original
        await self.chat.retry(tid)
        self.assertFalse(self.providers.bodies)
        self.assertEqual(self.chat.get(tid)['error_code'],'reply_dispatch_unknown')
        self.assertFalse(self.out.list(self.pid,self.sid,tid))

    async def test_revoke_between_memory_extract_and_verify(self):
        from tests.test_m8d import Model
        from persona_runtime.organization import OrganizationEngine
        self.config.update({'organization':{'reuse_chat':True}})
        self.consent(True)
        memory=Model();org=OrganizationEngine(self.store,self.config,memory,self.chat)
        policy=org.managed.current_policy(self.pid)
        org.managed.update_policy(self.pid,policy['data']|{'memory_automation':True},policy['id'],self.pid,identifier())
        uid,plan=self.plan(await self.submit())
        for i in range(len(plan['items'])):self.paint(uid,plan,i)
        org.automatic.tick(force=True)
        await org.run_next()
        self.consent(False)
        await org.run_next()
        self.assertEqual(len(memory.calls),1)
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT 1 FROM memories WHERE status='active'").fetchone())

    async def test_registered_native_adapter_uses_scoped_choices_and_plain_writer(self):
        from persona_runtime.ordinary_providers import ReviewedPrivateChatProviders,register,registered,retire
        nodes=('J1','J2.action','J2.stance','J2.task','J3','J6.mode','J6.coverage','J6.ending','J7')
        bundle={'protocol':'maibot-private-v2','scope':{'instance':self.config.load()['instance_id'],'personas':[self.pid]},
                'main':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro'},
                'jev':{'base_url':'https://example.invalid/v1','name':'jev-1.14'},
                'reply_system':'801','nodes':{n:{'instructions':'802','criteria':{},'source_criterion':'803'} for n in nodes}}
        path=self.paths.home/'804.json';path.write_text(json.dumps(bundle),encoding='utf8')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeErrorBase,'review_version_changed'):
            register(self.config,path,'0'*64)
        register(self.config,path,digest)
        self.assertEqual(registered(self.config).version,digest)
        retire(self.config)
        with self.assertRaisesRegex(RuntimeErrorBase,'review_version_changed'):
            register(self.config,path,'0'*64)
        self.assertIsNone(registered(self.config))
        register(self.config,path,digest)
        self.assertEqual(registered(self.config).version,digest)
        native_calls=[]
        class Jev:
            def __init__(self,model):pass
            async def close(self):pass
            async def complete(self,wire,**callbacks):
                native_calls.append(wire)
                node,=wire['questions']
                criteria=wire['questions'][node]['criteria']
                choice=next(iter(criteria))
                return 200,json.dumps({'answers':{node:{'type':'choice','choice':choice,
                    'probabilities':{k:int(k==choice) for k in criteria}}},'usage':{'total_tokens':1}})
        packets=[]
        class Writer:
            async def complete(self,model,messages):
                packets.append(messages)
                return '805',{'total_tokens':1}
        provider=ReviewedPrivateChatProviders(self.config,path,digest,writer=Writer(),jev_factory=Jev)
        self.assertEqual((await provider.choose('J1','806',{'current':['807']},['batch','wait']))[0],'batch')
        self.assertEqual((await provider.reply('809',{'targets':['807']}))[0],'805')
        self.assertEqual(packets[0],[{'role':'system','content':'801'},{'role':'user','content':'{"targets":["807"]}'}])
        self.assertEqual(set(native_calls[0]['questions']),{'J1'})

    async def test_fresh_messages_stay_held_until_manual_confirmation(self):
        from persona_runtime.ingress import IngressService
        ingress=IngressService(self.chat)
        policy=ingress.current_policy(self.pid,'desktop')
        ingress.update_policy(self.pid,'desktop','natural_batch',policy['id'],identifier(),manual_confirmation=True)
        inp={'persona_id':self.pid,'session_id':self.sid,'channel':'desktop','text':'901','request_id':'911'}
        receipt=ingress.accept(inp);ingress.accept(inp|{'text':'902','request_id':'912'})
        self.assertFalse(self.providers.calls)
        with self.store.connection() as db:version=db.execute('SELECT revision FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()[0]
        args=(receipt['batch_id'],'confirm',version,identifier(),self.pid,self.sid)
        self.assertEqual(ingress.action(*args),ingress.action(*args))
        await self.chat.wait()
        self.assertEqual([row['text'] for row in self.providers.bodies[0][1]['targets']],['901','902'])
        self.assertEqual(len(self.providers.bodies),1)
