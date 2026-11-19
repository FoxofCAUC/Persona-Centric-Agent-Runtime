"""Artificial copy consent is explicit and does not transfer to other roots."""
import json
from pathlib import Path
import shutil
import unittest

from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas,guard_cloud_dispatch,provider_identity
from persona_runtime.storage import Store
from tests import test_pre_m8 as fixtures

class PrivateCopyAuthorizationTests(unittest.TestCase):
    setUp=fixtures.PrivateTests.setUp

    def copy(self):
        copied=self.private.create_sandbox(self.rec['id'],visible_only=True)
        self.addCleanup(self.private.cleanup_sandbox,copied['home'])
        paths=RuntimePaths(Path(copied['home']));cfg=ConfigStore(paths);cfg.initialize()
        return copied,paths,Store(paths),cfg,PrivatePersonas(paths)

    def test_explicit_main_grant_is_idempotent_and_provider_bound(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',model)
        grant=private.authorize_main(store,pid,provider_identity(model),'copy-explicit-main')
        self.assertEqual(private.authorize_main(store,pid,provider_identity(model),'copy-explicit-main'),grant)
        guard_cloud_dispatch(store,pid,'main',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',dict(model,name='other-model'))

    def test_jev_switch_requires_its_own_provider_grant(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id']
        from persona_runtime.knowledge_routing import configured_model
        atomic_json(paths.home/'private/knowledge-router/config.json',{'base_url':'https://decision.fixture.invalid/v1','name':'jev-test','encrypted_key':'artificial-protected-jev'})
        model=configured_model(paths)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'knowledge_router',model)
        private.authorize_model(store,pid,'knowledge_router',provider_identity(model),'jev-switch-visible-copy')
        guard_cloud_dispatch(store,pid,'knowledge_router',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'knowledge_router',dict(model,name='other-provider'))

    def test_visible_copy_main_needs_own_grant_even_when_legacy_provider_matches(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',model)
        private.authorize_main(store,pid,provider_identity(model),'copy-own-main')
        guard_cloud_dispatch(store,pid,'main',model)
        other=paths.home.parent/(paths.home.name+'-copied-main')
        shutil.copytree(paths.home,other);self.addCleanup(self.private.cleanup_sandbox,other)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(Store(RuntimePaths(other)),pid,'main',model)

    def test_copy_instance_identity_remains_bound_after_config_change(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        private.authorize_main(store,pid,provider_identity(model),'copy-main-bound')
        data=cfg.load();data['instance_id']='different-instance';atomic_json(cfg.file,data)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',model)

    def test_visible_only_excludes_creator_material_and_source_relationship(self):
        hidden=dict(self.package['character']['lorebook'][0],id='hidden',visibility='creator_only',text='Artificial hidden data')
        self.package['character']['lorebook'].append(hidden)
        self.source.write_text(json.dumps(self.package,ensure_ascii=False),encoding='utf-8')
        self.rec=self.private.register(self.source,self.archive,'人工初始关系，不是实际经历。')
        copied,paths,store,_,_=self.copy()
        from persona_runtime.character import CharacterService
        from persona_runtime.profile import head
        self.assertTrue(all(r['visibility']=='conversation' for r in CharacterService(store).current(copied['persona_id'])['data']['lorebook']))
        with store.connection() as db:
            self.assertNotEqual(head(db,copied['persona_id'],'relationship')['data']['description'],'人工初始关系，不是实际经历。')
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
        self.assertFalse((paths.home/'source-archive.md').exists())

    def test_session_memory_grant_is_copy_session_account_and_revision_bound(self):
        from persona_runtime.copy_session_memory import set_consent,grant
        from persona_runtime.sessions import select
        copied,paths,store,cfg,private=self.copy()
        pid=copied['persona_id']
        cfg.update({'organization':{'reuse_chat':False,'model':{'api_key':'771','name':'773'}}})
        model=cfg.load()['organization']['model'];provider=provider_identity(model)
        with store.connection(write=True) as db:sid=select(db,pid,'desktop')
        self.assertEqual(set_consent(store,pid,sid,provider,0,enabled=True),1)
        self.assertEqual(set_consent(store,pid,sid,provider,0,enabled=True),1)
        with store.connection() as db:
            self.assertEqual(grant(store,db,pid,sid,model),1)
            self.assertIsNone(grant(store,db,pid,'774',model))
            self.assertIsNone(grant(store,db,pid,sid,model|{'name':'774'}))
        original=cfg.load()
        atomic_json(cfg.file,original|{'instance_id':'776'})
        with store.connection() as db:self.assertIsNone(grant(store,db,pid,sid,model))
        atomic_json(cfg.file,original)
        with self.assertRaisesRegex(ValueError,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'organization',model)
        cfg.update({'organization':{'model':{'api_key':'775'}}})
        with store.connection() as db:self.assertIsNone(grant(store,db,pid,sid,cfg.load()['organization']['model']))
        self.assertEqual(set_consent(store,pid,sid,provider,1,enabled=False),2)
        with self.assertRaisesRegex(ValueError,'version_conflict'):set_consent(store,pid,sid,provider,0,enabled=True)
        with store.connection() as db:self.assertIsNone(grant(store,db,pid,sid,model))

    def test_only_native_automatic_batch_uses_the_session_grant(self):
        import asyncio
        from persona_runtime.chat import ChatEngine
        from persona_runtime.copy_session_memory import set_consent,check_task,local_fact
        from persona_runtime.organization import OrganizationEngine
        from persona_runtime.models import identifier
        from tests.test_desktop_recovery import Completion
        from tests.test_m8d import Model
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id']
        cfg.update({'model':{'base_url':'http://127.0.0.1:9','name':'772','api_key':'771'},
                    'organization':{'reuse_chat':False,'model':{'base_url':'http://127.0.0.1:9','name':'773','api_key':'771'}}})
        private.authorize_main(store,pid,provider_identity(cfg.load()['model']),'main-776')
        chat=ChatEngine(store,cfg,Completion());org=OrganizationEngine(store,cfg,Model(),chat)
        async def exercise():
            old=chat.session(persona_id=pid)['session_id']
            await chat.submit('desktop','775',identifier(),old,persona_id=pid);await chat.wait()
            sid=chat.session(persona_id=pid,fresh=True)['session_id']
            provider=provider_identity(org.model());set_consent(store,pid,sid,provider,0,enabled=True)
            policy=org.managed.current_policy(pid)
            org.managed.update_policy(pid,policy['data']|{'memory_automation':True},policy['id'],pid,identifier())
            await chat.submit('desktop','777',identifier(),sid,persona_id=pid);await chat.wait()
            from persona_runtime.models import LOCAL_USER_ID,timestamp
            with store.connection(write=True) as db:
                eid=db.execute("SELECT id FROM events WHERE session_id=? AND text='777'",(sid,)).fetchone()[0]
                now=timestamp()
                db.execute("INSERT INTO memory_batches VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)",
                           ('7791',pid,LOCAL_USER_ID,old,'{}','7791','extract','completed','{}',now,now))
                detail={'decision':{'subject':'用户','attribute':'pref-'+eid},'blocked_memory_ids':[]}
                db.execute("INSERT INTO memory_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                           ('7792',pid,LOCAL_USER_ID,'7791','7793','candidates','quarantined','7794',json.dumps(detail),now))
            for _ in range(10):
                org.automatic.tick(force=True)
                if not await org.run_next():break
            self.assertEqual(len(org.client.calls),2)
            with store.connection() as db:
                mem=db.execute("SELECT id,text FROM memories WHERE status='active'").fetchone()
                self.assertIsNotNone(mem,[tuple(r) for r in db.execute('SELECT status,error_code FROM organization_tasks')])
                self.assertEqual(mem['text'],'777')
                self.assertEqual(db.execute("SELECT state FROM memory_decisions WHERE id='7792'").fetchone()[0],'quarantined')
                self.assertTrue(local_fact(db,mem['id'],pid,sid));self.assertFalse(local_fact(db,mem['id'],pid,old))
                tasks=db.execute('SELECT * FROM organization_tasks').fetchall()
                self.assertEqual(len(tasks),2);self.assertEqual({t['session_id'] for t in tasks},{sid})
                task=dict(tasks[0]);payload=json.loads(task['payload_json'])
                with self.assertRaisesRegex(ValueError,'private_session_mismatch'):
                    check_task(store,db,task|{'id':'778'},payload,org.model())
                altered=json.loads(task['payload_json']);altered['events'][0]['text']='779'
                with self.assertRaisesRegex(ValueError,'private_session_mismatch'):
                    check_task(store,db,task,altered,org.model())
            set_consent(store,pid,sid,provider,1,enabled=False)
            set_consent(store,pid,sid,provider,2,enabled=True)
            with store.connection() as db:
                with self.assertRaisesRegex(ValueError,'private_cloud_provider_not_authorized'):
                    check_task(store,db,task,payload,org.model())
            with store.connection(write=True) as db:
                db.execute("DELETE FROM memory_decisions WHERE id='7792'")
                db.execute("DELETE FROM memory_batches WHERE id='7791'")
            await chat.submit('desktop','780',identifier(),sid,persona_id=pid);await chat.wait()
            complete=org.client.complete
            entered,release=asyncio.Event(),asyncio.Event()
            async def pending(model,messages):
                entered.set();await release.wait()
                return await complete(model,messages)
            org.client.complete=pending
            org.automatic.tick(force=True)
            executing=asyncio.create_task(org.run_next())
            try:
                await asyncio.wait_for(entered.wait(),5)
                set_consent(store,pid,sid,provider,3,enabled=False)
            finally:release.set();await executing
            for _ in range(3):
                org.automatic.tick(force=True);await org.run_next()
            self.assertEqual(len(org.client.calls),3)
            with store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM memories WHERE text='780'").fetchone()[0],0)
                self.assertTrue(db.execute("SELECT 1 FROM organization_tasks WHERE status='blocked_authorization'").fetchone())
        try:asyncio.run(exercise())
        finally:chat.cognitive.characters.close()

    def test_background_grants_and_loose_markers_cannot_authorize_a_copy(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id']
        with self.assertRaisesRegex(RuntimeErrorBase,'private_copy_permission_kind_not_allowed'):
            private.authorize_model(store,pid,'organization',provider_identity(cfg.load()['model']),'deny-background')
        (paths.home/'private-sandbox.json').unlink()
        with self.assertRaisesRegex(RuntimeErrorBase,'private_scope_mismatch'):
            private.authorize_main(store,pid,provider_identity(cfg.load()['model']),'deny-loose-copy')
        # Restore ownership metadata solely so the normal scoped cleanup can run.
        atomic_json(paths.home/'private-sandbox.json',{'private_id':self.rec['id'],'persona_id':pid,'formal_home':str(self.paths.home.resolve())})

if __name__=='__main__':unittest.main()
