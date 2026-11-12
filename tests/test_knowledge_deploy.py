import asyncio,hashlib,json,sqlite3,unittest
from unittest.mock import patch
from contextlib import closing
from persona_runtime.config import atomic_json
from persona_runtime.private_personas import PrivatePersonas,provider_identity,guard_cloud_dispatch
from persona_runtime.security import protect
from persona_runtime.models import RuntimeErrorBase,generic_persona
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.knowledge_service import read
from persona_runtime.knowledge_web import WebKnowledge
from scripts import m9a_deploy as deploy
from tests import test_m4 as fixtures

class RolloutTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    async def asyncTearDown(self):self.chat.knowledge.close();await self.chat.wait();await self.org.knowledge.drain()
    def configure(self,private=True):
        self.pid=self.persona['id'];self.cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'test-main','api_key':'artificial-main'}})
        encrypted=protect('artificial-router');self.router_file=self.paths.home/'private/knowledge-router/config.json'
        atomic_json(self.router_file,{'base_url':'https://example.com/v1','name':'jev-1.13','encrypted_key':encrypted,'gate':{'base_url':'https://example.com/v1','name':'jev-1.13','completed_rounds':10,'successes':10,'credential_sha256':hashlib.sha256(encrypted.encode()).hexdigest(),'artificial_fixture':True}})
        self.private=PrivatePersonas(self.paths)
        if private:
            self.private.save_record({'id':'artificial-private','persona_id':self.pid,'approved_models':{}})
            self.private.authorize_main(self.store,self.pid,provider_identity(self.cfg.load()['model']),'allow-main')
        self.out=self.paths.home/'exports/acceptance/rollout'

    async def test_router_grant_is_explicit_idempotent_and_not_inherited(self):
        self.configure();model=configured_model(self.paths)
        self.assertFalse(self.org.routing.authorization(self.pid)['authorized'])
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):guard_cloud_dispatch(self.store,self.pid,'knowledge_router',model)
        grant=self.org.routing.authorize(self.pid,provider_identity(model),'allow-router')
        self.assertEqual(self.org.routing.authorize(self.pid,provider_identity(model),'allow-router'),grant)
        guard_cloud_dispatch(self.store,self.pid,'knowledge_router',model)
        other=self.service.create_persona(generic_persona())['id'];self.private.save_record({'id':'other-private','persona_id':other,'approved_models':{'knowledge_router':provider_identity(model)}})
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):guard_cloud_dispatch(self.store,other,'knowledge_router',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_selection_conflict'):self.org.routing.authorize(other,provider_identity(model),'wrong-role')

    async def test_changed_provider_or_unpassed_gate_cannot_get_a_grant(self):
        self.configure();model=configured_model(self.paths)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_provider_changed'):self.org.routing.authorize(self.pid,'stale-provider','stale')
        self.org.routing.authorize(self.pid,provider_identity(model),'valid-provider')
        saved=json.loads(self.router_file.read_text());saved['name']='changed';atomic_json(self.router_file,saved)
        with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_routing_gate_required'):self.org.routing.authorize(self.pid,provider_identity(configured_model(self.paths)),'bad-gate')
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):guard_cloud_dispatch(self.store,self.pid,'knowledge_router',configured_model(self.paths))

    async def test_migration_keeps_old_business_and_enable_waits_for_router_consent(self):
        self.configure();self.chat.knowledge.close()
        from persona_runtime.schema_m9a3 import TABLES
        with closing(sqlite3.connect(self.store.path)) as db,db:
            for table in reversed(TABLES):db.execute('DROP TABLE '+table)
            db.execute('DROP TABLE knowledge_control');db.execute('PRAGMA user_version=24')
        with patch('socket.socket.connect',side_effect=AssertionError('migration must not use network')):result=deploy.prepare(self.paths.home,self.out)
        self.assertEqual(result['schema'],26);self.assertTrue(result['old_business_unchanged']);self.assertTrue(result['awaiting_router_consent'])
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):deploy.activate(self.paths.home,self.out)
        with self.store.connection() as db:self.assertFalse(read(db,'policy.'+self.pid,{'enabled':False})['data']['enabled'])
        self.org.routing.authorize(self.pid,provider_identity(configured_model(self.paths)),'consent-after-preview')
        result=deploy.activate(self.paths.home,self.out);self.assertEqual(result['channels'],['desktop'])
        self.assertEqual(result['processing']['data']['model_source'],'main');self.assertEqual(result['routing']['data']['variant'],'web_only')
        self.assertEqual(deploy.activate(self.paths.home,self.out),result)

    async def test_activation_recovers_saved_write_without_widening_other_role(self):
        self.configure(private=False);other=self.service.create_persona(generic_persona())['id']
        deploy.prepare(self.paths.home,self.out)
        original=WebKnowledge.update_policy;seen=[]
        def after_commit(service,*args,**kwargs):
            result=original(service,*args,**kwargs);seen.append(result);raise OSError('response interruption after committed policy')
        with patch.object(WebKnowledge,'update_policy',after_commit),self.assertRaises(OSError):deploy.activate(self.paths.home,self.out)
        with self.store.connection() as db:self.assertFalse(read(db,'policy.'+self.pid,{'enabled':False})['data']['enabled'])
        result=deploy.activate(self.paths.home,self.out)
        with self.store.connection() as db:
            self.assertFalse(read(db,'policy.'+other,{'enabled':False})['data']['enabled'])
            self.assertEqual(read(db,'web.policy.'+self.pid,{})['revision'],seen[0]['revision'])
        self.assertTrue(result['other_roles_unchanged'])

    async def test_default_config_materialization_does_not_look_like_provider_change(self):
        self.configure(private=False)
        raw=json.loads(self.cfg.file.read_text(encoding='utf-8'));raw.pop('qq_backend');raw.pop('snowluma_port');atomic_json(self.cfg.file,raw)
        deploy.prepare(self.paths.home,self.out)
        plan=deploy.read(self.out/'plan.json');plan.pop('config_effective_sha256');atomic_json(self.out/'plan.json',plan)
        atomic_json(self.cfg.file,self.cfg.load())
        result=deploy.activate(self.paths.home,self.out)
        self.assertTrue(result['formal_limits_unchanged'])
        self.cfg.update({'model':{'name':'actually-changed'}})
        with self.assertRaisesRegex(RuntimeErrorBase,'knowledge_rollout_configuration_changed'):deploy.activate(self.paths.home,self.out)
