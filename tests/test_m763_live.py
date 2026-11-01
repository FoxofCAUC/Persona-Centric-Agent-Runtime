"""Live candidate and evaluation production chain, artificial inputs and mock models."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_m762 as prior
from persona_runtime.decision_capsule import AssistAdmission,LiveAdmission,VERSION
from persona_runtime.models import identifier


class Live(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=prior.M762.asyncSetUp;asyncTearDown=prior.M762.asyncTearDown
    source=prior.M762.source;document=prior.M762.document;publish=prior.M762.publish;queued=prior.M762.queued

    def admit(self):
        self.engine.live_admission=LiveAdmission(AssistAdmission(self.paths.home,self.config.load()['instance_id'],frozenset((self.pid,)),self.paths.home/'absent'))

    def mode(self,mode):
        p=self.ingress.current_policy(self.pid,'desktop')
        return self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),disposition_mode='live' if mode=='assist' else mode)

    test_live_payload_and_strict_audit=prior.M762.test_assist_real_call_payload_and_body_free_audit
    test_withdraw_after_prepare=prior.M762.test_withdraw_after_prepare_removes_capsule
    test_manifest_storage_zero_calls=prior.M762.test_manifest_failure_zero_calls
    test_nonfirst_and_frozen=prior.M762.test_nonfirst_member_and_snapshot_frozen
    test_no_worker_in_final_transaction=prior.M762.test_final_rebuild_uses_no_worker_and_keeps_capsule
    test_busy_fallback=prior.M762.test_busy_falls_back_without_adding_call

    async def test_no_grant_and_disabled_returns_baseline(self):
        with self.assertRaisesRegex(Exception,'live_not_admitted'):self.mode('live')
        self.publish();self.admit();self.mode('live');tid=await self.queued()
        p=self.engine.cognitive.prepare(tid);self.assertEqual(p['manifest']['decision_capsule']['requested_mode'],'live')
        self.mode('baseline');other=await self.queued()
        self.assertNotIn(VERSION,self.engine.cognitive.prepare(other)['messages'][0]['content'])
        self.assertIn(VERSION,self.engine.cognitive.prepare(tid)['messages'][0]['content'])
        self.engine.live_admission=None
        with self.assertRaisesRegex(Exception,'live_not_admitted'):self.engine.cognitive.prepare(tid)

    async def test_missing_release_evidence_cannot_enable_formal(self):
        self.admit();grant=self.engine.live_admission
        self.engine.live_admission=LiveAdmission(grant.isolation,self.paths.home/'exports/acceptance/missing.json',True)
        with self.assertRaises(FileNotFoundError):self.mode('live')

    async def test_runtime_recovery_uses_original_request_without_dispatch(self):
        from persona_runtime.evaluation import EvaluationStore
        store=EvaluationStore(self.paths)
        store.create('recovery',[{'id':str(i),'scenario':'人工模拟','author_summary':'人工资料'} for i in range(100)],
            {'runtime_home':str(self.paths.home.resolve()),'persona_id':self.pid,'channel':'desktop'},simulated=True)
        with store.connect('recovery') as db:rid=db.execute("SELECT request_id FROM slots WHERE case_id='0' AND arm='A'").fetchone()[0]
        store.transition('recovery','0','A','not_called','accepted')
        row=await self.engine.submit('desktop','人工恢复场景',rid,persona_id=self.pid);await self.engine.wait()
        store.transition('recovery','0','A','accepted','running');store.recover('recovery')
        before=len(self.fake.calls);result=store.reconcile_runtime('recovery',self.engine,self.ingress)
        self.assertEqual(result['slots']['success'],1);self.assertEqual(len(self.fake.calls),before)
        with store.connect('recovery') as db:
            slot=db.execute("SELECT * FROM slots WHERE case_id='0' AND arm='A'").fetchone()
            self.assertEqual(slot['output'],'模拟回复');self.assertIsNotNone(slot['attempt_id'])

    async def test_release_evidence_and_new_identity_defaults(self):
        from persona_runtime.evaluation import EvaluationStore
        from persona_runtime.archive import fingerprint
        from persona_runtime.config import atomic_json
        from persona_runtime.models import generic_persona
        from hashlib import sha256
        import persona_runtime.decision_capsule as module
        self.admit();store=EvaluationStore(self.paths)
        # Synthetic contract evidence; no human preference or independent result claim.
        store.create('release-contract',[{'id':str(i),'scenario':'工具合同','author_summary':'人工'} for i in range(100)],{},simulated=True)
        review={'left_task':'pass','right_task':'pass','left_action':'respond','right_action':'respond','natural':'tie','reason':'模拟合同'}
        with store.connect('release-contract',True) as db:
            db.executemany('INSERT INTO reviews VALUES(?,1,?)',[(str(i),json.dumps(review)) for i in range(100)])
            db.execute("UPDATE metadata SET value='true' WHERE key='frozen'")
            rows=[dict(r) for r in db.execute('SELECT * FROM reviews ORDER BY case_id')]
        report={'package_id':'release-contract','instance_id':self.config.load()['instance_id'],'home':str(self.paths.home.resolve()),
            'target_persona_ids':[self.pid],'initialize_new':True,
            'review_hash':fingerprint(rows),'code_hashes':{p.name:sha256(p.read_bytes()).hexdigest() for p in Path(module.__file__).parent.glob('*.py')},
            'gates':{k:True for k in ('quality','human_review','platform','privacy','rollback')},'author_configuration_confirmed':True,
            'counts':{'situation_top1':34,'situation_top2':38,'implicit_gain':6,'reasonable_pairs':16,'unnecessary_cases':2,
                'action_implementation':95,'natural_wins':60,'b_task_success':100,'c_task_success':100,'critical_errors':0}}
        path=self.paths.home/'exports/acceptance/release-contract.json';atomic_json(path,report)
        grant=LiveAdmission(self.engine.live_admission.isolation,path,True);self.engine.live_admission=grant
        with self.assertRaisesRegex(Exception,'live_release_not_admitted'):grant.check(self.store,self.config,self.pid)
        # Construct a validator fixture in this disposable test directory. It is
        # not a human review or independent result and is deleted by tearDown.
        with store.connect('release-contract',True) as db:
            for i,row in enumerate(db.execute('SELECT * FROM cases ORDER BY ordinal').fetchall()):
                value=review|{'natural':('left' if row['left_arm']=='C' else 'right') if i<60 else 'tie'}
                db.execute('UPDATE reviews SET body=? WHERE case_id=?',(json.dumps(value),row['id']))
            db.execute("UPDATE metadata SET value='false' WHERE key='simulated'")
            report['review_hash']=fingerprint([dict(r) for r in db.execute('SELECT * FROM reviews ORDER BY case_id')])
        atomic_json(path,report);grant.check(self.store,self.config,self.pid)
        official=self.paths.home/'exports/acceptance/disposition-live-release.json';atomic_json(official,report)
        from persona_runtime.decision_capsule import load_live_release,new_identity_mode
        self.assertIsNotNone(load_live_release(self.store,self.config))
        # Exercise an offline CLI-style service without the engine callback too.
        callback=self.store.new_identity_mode;del self.store.new_identity_mode
        offline=self.service.create_persona(generic_persona())['id'];self.store.new_identity_mode=callback
        fresh=self.service.create_persona(generic_persona())['id']
        from persona_runtime.persona_management import PersonaManagement
        from persona_runtime.character import CharacterService
        management=PersonaManagement(self.store);view=management.inspect(self.pid)
        copied=management.clone(self.pid,view['versions'],self.pid,identifier())['id']
        characters=CharacterService(self.store)
        package=json.loads(Path(characters.export(self.pid)['path']).read_text(encoding='utf-8'))
        imported=characters.import_package(self.pid,package,identifier())['id']
        self.service.use_persona(fresh)
        with self.store.connection() as db:
            from persona_runtime.context_policy import current
            policies=[current(db,fresh,ch) for ch in ('desktop','qq')]
            for pid in (copied,imported,offline):
                self.assertEqual(current(db,pid,'desktop')['disposition_mode'],'live')
                self.assertEqual(current(db,pid,'qq')['disposition_mode'],'live')
        self.assertTrue(all(p['disposition_mode']=='live' and p['disposition']['status']=='not_configured' for p in policies))
        self.engine.live_admission=None
        self.ingress.update_policy(fresh,'desktop','legacy_per_message',policies[0]['policy_id'],identifier(),disposition_mode='baseline')
        self.store.initialize()
        self.assertEqual(self.ingress.current_policy(fresh,'desktop')['disposition_mode'],'baseline')


class Arms(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=prior.M762.asyncSetUp;asyncTearDown=prior.M762.asyncTearDown
    source=prior.M762.source;document=prior.M762.document;publish=prior.M762.publish;queued=prior.M762.queued;admit=prior.M762.admit;mode=prior.M762.mode

    async def matrix(self,members=None):
        from persona_runtime.evaluation_material import EvaluationSession
        from persona_runtime.character import CharacterService,empty_character
        from tests.test_m753 import lore
        self.publish();self.admit()
        data=empty_character();data['lorebook']=[lore('book','人工书店有阅读角',keywords=['倾诉'])]
        characters=CharacterService(self.store)
        with patch.object(self.engine.cognitive.characters,'notify',return_value=[]):
            characters.update(self.pid,data,characters.current(self.pid)['id'],identifier())
            p=self.ingress.current_policy(self.pid,'desktop')
            self.ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),character_retrieval_mode='hybrid_llm')
        turns={}
        for arm in 'ABC':
            self.mode('assist' if arm=='C' else 'baseline')
            if members:
                p=self.ingress.current_policy(self.pid,'desktop')
                self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier())
                with patch.object(self.engine,'_schedule'):
                    receipts=[self.ingress.accept({'text':t,'request_id':identifier(),'persona_id':self.pid}) for t in members]
                    batch=self.ingress.get(receipts[0]['batch_id'])
                    turns[arm]=self.ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid)['turn_id']
            else:turns[arm]=await self.queued('我想找人倾诉')
        session=EvaluationSession(self.engine.assist_admission).attach(self.engine);session.register(self.engine,turns)
        return session,turns

    async def prepare_matrix(self,members=None,selection_output=None):
        from persona_runtime.character_selector_protocol import candidates
        from persona_runtime.character_retrieval import load_config
        session,turns=await self.matrix(members)
        self.fake.output=selection_output if selection_output is not None else '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}'
        async def complete(model,messages):
            from copy import deepcopy
            self.fake.calls.append(model);self.fake.messages.append(deepcopy(messages))
            return (self.fake.output if '_reply_mode' not in model else '模拟主回复'),{'total_tokens':1}
        self.fake.complete=complete
        def pool(version,policy,members,scenario):return candidates(version,members,{'lore':[],'example':[]},load_config())
        with patch.object(self.engine.cognitive.characters,'local_pool',side_effect=pool):
            b_selected=await self.engine.selector.prepare(turns['B'])
        c_selected=await self.engine.selector.prepare(turns['C'])
        b=self.engine.cognitive.prepare(turns['B'],b_selected);c=self.engine.cognitive.prepare(turns['C'],c_selected)
        return session,turns,b,c

    async def test_shared_one_actual_selection_and_three_real_builds(self):
        session,turns,b,c=await self.prepare_matrix()
        self.assertIn('人工书店有阅读角',b['messages'][0]['content'])
        self.assertNotIn(VERSION,b['messages'][0]['content']);self.assertIn(VERSION,c['messages'][0]['content'])
        session.verify_pair(self.engine,turns['B'],b,turns['C'],c)
        a=self.engine.cognitive.prepare(turns['A'])
        self.assertNotIn('人工书店有阅读角',a['messages'][0]['content'])
        self.assertNotIn('expression_style": "'+self.source()['text'],a['messages'][0]['content'])
        self.assertFalse(a['manifest']['evaluation']['evaluation_reuse'])
        for arm in 'CBA':await self.engine._run(turns[arm])
        self.assertTrue(all(self.engine.get(t)['status']=='completed' for t in turns.values()))
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],3)
            m=json.loads(db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue'",(turns['C'],)).fetchone()[0])
            self.assertTrue(m['evaluation']['evaluation_reuse']);self.assertIsNotNone(m['evaluation']['selection_attempt_id'])
        self.assertEqual(len(self.fake.calls),4)

    async def test_mismatch_and_tamper_rejected_before_main(self):
        session,turns,b,c=await self.prepare_matrix()
        c['messages'][0]['content']='tampered'
        with self.assertRaisesRegex(Exception,'evaluation_capsule_location'):session.verify_pair(self.engine,turns['B'],b,turns['C'],c)
        await self.engine._run(turns['C'])
        self.assertEqual(self.engine.get(turns['C'])['attempt_count'],0)
        self.assertEqual(len(self.fake.calls),1)
        with self.store.connection(write=True) as db:db.execute("UPDATE turns SET character_snapshot='{}' WHERE id=?",(turns['B'],))
        with self.assertRaises(Exception):await self.engine.selector.prepare(turns['C'])

    async def test_restart_material_restore_without_another_selection(self):
        from persona_runtime.evaluation_material import EvaluationSession
        session,turns,b,c=await self.prepare_matrix()
        restored=EvaluationSession(self.engine.assist_admission).attach(self.engine);restored.register(self.engine,turns)
        before=len(self.fake.calls)
        await self.engine.selector.prepare(turns['B']);await self.engine.selector.prepare(turns['C'])
        self.assertEqual(len(self.fake.calls),before)
        restored.verify_pair(self.engine,turns['B'],b,turns['C'],c)
        path=restored.material_path(self.engine,restored.turns[turns['B']]['group'])
        value=json.loads(path.read_text(encoding='utf-8'));value['result']['text']='tampered'
        path.write_text(json.dumps(value),encoding='utf-8')
        again=EvaluationSession(self.engine.assist_admission).attach(self.engine)
        with self.assertRaisesRegex(Exception,'evaluation_saved_material_invalid'):again.register(self.engine,turns)

    async def test_failed_selection_shares_production_fallback_and_recovers_without_call(self):
        session,turns,b,c=await self.prepare_matrix(selection_output='broken synthetic output')
        self.assertEqual(b['manifest']['character_retrieval']['effective_mode'],'lexical')
        self.assertEqual(c['manifest']['character_retrieval']['degrade_reason'],'selector_invalid_output')
        session.verify_pair(self.engine,turns['B'],b,turns['C'],c)
        before=len(self.fake.calls)
        group=session.turns[turns['B']]['group'];session.shared.clear();session.material_path(self.engine,group).unlink()
        recovered=await self.engine.selector.prepare(turns['B'])
        self.assertEqual(recovered['degrade_reason'],'selector_invalid_output')
        self.assertIsNotNone(recovered['selection_attempt_id']);self.assertEqual(len(self.fake.calls),before)
        reused=await self.engine.selector.prepare(turns['C']);self.assertEqual(reused['selection_attempt_id'],recovered['selection_attempt_id'])

    async def test_three_members_normalize_only_generated_event_ids(self):
        # Normalization unit contract only; independent-runtime coverage is separate.
        session,turns,b,c=await self.prepare_matrix(['人工多成员开发合同','我想找人倾诉','原样文本含event_id字样，不得修改'])
        session.verify_pair(self.engine,turns['B'],b,turns['C'],c)
        self.assertIn('原样文本含event_id字样，不得修改',b['messages'][-1]['content'])
        self.assertNotEqual(b['messages'][-1]['content'],c['messages'][-1]['content'])

    async def test_independent_instances_sessions_and_shared_selection(self):
        from persona_runtime.evaluation_material import EvaluationSession
        from persona_runtime.character import CharacterService,empty_character
        from persona_runtime.character_retrieval import load_config
        from persona_runtime.character_selector_protocol import candidates
        from persona_runtime.storage import Store
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.config import ConfigStore,atomic_json
        from persona_runtime.chat import ChatEngine
        from persona_runtime.ingress import IngressService
        from persona_runtime.decision_capsule import AssistAdmission
        from tests.test_m753 import lore
        from contextlib import closing
        import sqlite3
        self.publish()
        data=empty_character();data['lorebook']=[lore('book','人工书店有阅读角',keywords=['倾诉'])]
        characters=CharacterService(self.store)
        with patch.object(self.engine.cognitive.characters,'notify',return_value=[]):
            characters.update(self.pid,data,characters.current(self.pid)['id'],identifier())
        p=self.ingress.current_policy(self.pid,'desktop')
        self.ingress.update_policy(self.pid,'desktop','natural_batch',p['id'],identifier(),character_retrieval_mode='hybrid_llm')
        engines={};turns={};calls=[]
        async def model(cfg,messages):
            calls.append(cfg)
            return ('模拟主回复' if '_reply_mode' in cfg else '{"schema_version":1,"lore_ids":["book"],"example_ids":[]}'),{'total_tokens':1}
        try:
            for arm in 'ABC':
                paths=RuntimePaths(self.paths.home/('arm-'+arm));paths.initialize();store=Store(paths)
                with self.store.connection() as source:
                    with closing(sqlite3.connect(store.path)) as dest:source.backup(dest)
                store.initialize();config=ConfigStore(paths);config.initialize()
                cfg=config.load();original=self.config.load();cfg['model']=original['model'];cfg['character_selector']=original['character_selector'];atomic_json(config.file,cfg)
                client=type('Model',(),{'complete':staticmethod(model)})()
                grant=AssistAdmission(paths.home,cfg['instance_id'],frozenset((self.pid,)),self.paths.home)
                engine=ChatEngine(store,config,client,assist_admission=grant);engines[arm]=engine;ingress=IngressService(engine)
                policy=ingress.current_policy(self.pid,'desktop');ingress.update_policy(self.pid,'desktop','natural_batch',policy['id'],identifier(),disposition_mode='assist' if arm=='C' else 'baseline')
                with patch.object(engine,'_schedule'):
                    receipts=[ingress.accept({'text':text,'request_id':identifier(),'persona_id':self.pid}) for text in ('人工第一条','我想找人倾诉','第三条仍属于普通用户文本')]
                    batch=ingress.get(receipts[0]['batch_id']);turns[arm]=ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid)['turn_id']
            sessions=EvaluationSession.register_independent(engines,turns)
            def pool(version,policy,members,scenario):return candidates(version,members,{'lore':[],'example':[]},load_config())
            with patch.object(engines['B'].cognitive.characters,'local_pool',side_effect=pool):selected_b=await engines['B'].selector.prepare(turns['B'])
            selected_c=await engines['C'].selector.prepare(turns['C'])
            prepared={a:engines[a].cognitive.prepare(turns[a],s) for a,s in (('B',selected_b),('C',selected_c))}
            sessions['C'].verify_independent(turns,prepared)
            self.assertEqual(len({engines[a].get(turns[a])['session_id'] for a in 'ABC'}),3)
            self.assertEqual(len(calls),1)
            for arm in 'CAB':await engines[arm]._run(turns[arm])
            self.assertTrue(all(engines[a].get(turns[a])['status']=='completed' for a in 'ABC'))
            self.assertEqual(len(calls),4)
            self.assertNotIn('人工书店有阅读角',engines['A'].cognitive.prepare(turns['A'])['messages'][0]['content'])
            # New isolated request may reuse exactly the original paid selection;
            # the retry coordinator separately rejects replay of successful slots.
            engines['B'].service.create_session();ingress=IngressService(engines['B'])
            with patch.object(engines['B'],'_schedule'):
                receipts=[ingress.accept({'text':text,'request_id':identifier(),'persona_id':self.pid}) for text in ('人工第一条','我想找人倾诉','第三条仍属于普通用户文本')]
                batch=ingress.get(receipts[0]['batch_id']);new_turn=ingress.action(batch['id'],'seal',batch['revision'],identifier(),self.pid)['turn_id']
            replacement=turns|{'B':new_turn}
            reused=EvaluationSession.register_independent(engines,replacement,material_turns=turns)
            selected=await engines['B'].selector.prepare(new_turn)
            self.assertEqual(selected['selection_attempt_id'],selected_b['selection_attempt_id']);self.assertEqual(len(calls),4)
            prepared={'B':engines['B'].cognitive.prepare(new_turn,selected),'C':engines['C'].cognitive.prepare(turns['C'],selected_c)}
            reused['C'].verify_independent(replacement,prepared)
            self.assertTrue(prepared['B']['manifest']['evaluation']['evaluation_reuse'])
            self.assertEqual(prepared['B']['manifest']['evaluation']['selection_turn_id'],turns['B'])
            await engines['B']._run(new_turn);self.assertEqual(engines['B'].get(new_turn)['status'],'completed')
            self.assertEqual(len(calls),5)
            self.assertEqual(engines['B'].get(turns['B'])['status'],'completed')
            with self.assertRaisesRegex(Exception,'evaluation_material_scope_mismatch'):
                EvaluationSession.register_independent(engines,replacement,material_turns=turns|{'B':turns['A']})
        finally:
            for engine in engines.values():
                await engine.wait();await engine.observer.drain();engine.cognitive.shadow.close();engine.cognitive.characters.close()
