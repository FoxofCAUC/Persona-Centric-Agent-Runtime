"""Released features default on; artificial isolated identities, zero network calls."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_persona_management as management_base
from persona_runtime.character import CharacterService
from persona_runtime.context_policy import initialize
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier, RuntimeErrorBase


class FeatureDefaultsTests(unittest.TestCase):
    def setUp(self):
        management_base.ManagementTests.setUp(self)
        engine=SimpleNamespace(store=self.store,config=self.cfg,service=self.service,tasks={})
        self.ingress=IngressService(engine)

    def assert_enabled(self,pid):
        for channel in ('desktop','qq'):
            p=self.ingress.current_policy(pid,channel)
            self.assertEqual(p['mode'],'natural_batch')
            self.assertEqual(p['reply_mode'],'natural_structured')
            self.assertEqual((p['context_mode'],p['cross_session']),('working_set',False))
            self.assertEqual(p['character_retrieval']['mode'],'hybrid_llm')
            self.assertEqual(p['character_retrieval']['selector_contract']['prompt'],'character-selector-v2')
            self.assertEqual(json.loads(p['data_json'])['expression']['mode'],'natural_structured')

    def legacy(self,pid,channel='desktop',**kw):
        p=self.ingress.current_policy(pid,channel)
        return self.ingress.update_policy(pid,channel,'legacy_per_message',p['id'],identifier(),
            reply_mode='legacy_full',character_retrieval_mode='lexical',**kw)

    def test_new_identity_both_entries_enabled_without_model_configuration(self):
        self.assertFalse(self.cfg.load()['character_selector']['enabled'])
        self.assert_enabled(self.a)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0],0)

    def test_copy_uses_released_defaults_not_source_opt_out_and_keeps_zero_history(self):
        self.legacy(self.a)
        before=self.service.show_persona(self.a)['spec']
        copied=self.m.clone(self.a,self.m.inspect(self.a)['versions'],self.a,identifier())['id']
        self.assert_enabled(copied)
        self.assertEqual(self.service.show_persona(copied)['spec'],before)
        self.assertEqual(self.ingress.current_policy(self.a,'desktop')['reply_mode'],'legacy_full')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE persona_id=?',(copied,)).fetchone()[0],0)

    def test_import_has_atomic_enabled_policies_and_repeat_does_not_duplicate(self):
        characters=CharacterService(self.store)
        package={'format':'pcar_character_v1','persona_spec':self.service.show_persona(self.a)['spec'],
                 'character':characters.current(self.a)['data']}
        op=identifier();result=characters.import_package(self.a,package,op)
        self.assert_enabled(result['id'])
        self.assertEqual(characters.import_package(self.a,package,op)['id'],result['id'])
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM interaction_policies WHERE persona_id=?',(result['id'],)).fetchone()[0],2)

    def test_post_commit_preparation_for_create_copy_and_import(self):
        seen=[]
        def committed(pid):
            # A separate write succeeds only after the authoring transaction commits.
            with self.store.connection(write=True) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM interaction_heads WHERE persona_id=?',(pid,)).fetchone()[0],2)
            seen.append(pid)
        self.store.character_committed=committed
        created=self.service.create_persona(generic_persona())['id']
        copied=self.m.clone(self.a,self.m.inspect(self.a)['versions'],self.a,identifier())['id']
        characters=CharacterService(self.store)
        package={'format':'pcar_character_v1','persona_spec':self.service.show_persona(self.a)['spec'],
                 'character':characters.current(self.a)['data']}
        imported=characters.import_package(self.a,package,identifier())['id']
        self.assertEqual(seen,[created,copied,imported])

    def test_creation_rolls_back_if_default_contract_cannot_be_frozen(self):
        with self.store.connection() as db:count=db.execute('SELECT count(*) FROM personas').fetchone()[0]
        with patch('persona_runtime.character_policy.choose',side_effect=RuntimeErrorBase('invalid_default_contract')):
            with self.assertRaisesRegex(RuntimeErrorBase,'invalid_default_contract'):
                self.service.create_persona(generic_persona())
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM personas').fetchone()[0],count)

    def test_reinitialization_preserves_explicit_opt_out_and_old_policy_bytes(self):
        p=self.legacy(self.a)
        with self.store.connection(write=True) as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM interaction_policies ORDER BY id')]
            initialize(db);initialize(db,self.a,new_persona=True)
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM interaction_policies ORDER BY id')])
        self.store.initialize()
        self.assertEqual(self.ingress.current_policy(self.a,'desktop')['id'],p['id'])

    def test_legacy_migration_initialization_does_not_silently_enable_cloud(self):
        with self.store.connection(write=True) as db:
            db.execute('DELETE FROM interaction_heads WHERE persona_id=?',(self.b,))
            db.execute('DELETE FROM interaction_policies WHERE persona_id=?',(self.b,))
            initialize(db,self.b)
        p=self.ingress.current_policy(self.b,'qq')
        self.assertEqual((p['mode'],p['reply_mode'],p['character_retrieval']['mode']),('legacy_per_message','legacy_full','lexical'))

    def test_explicit_admin_target_does_not_switch_current_identity(self):
        p=self.legacy(self.b,expected_selected=self.a)
        self.assertEqual(self.service.show_persona()['id'],self.a)
        self.assertEqual(p['persona_id'],self.b)
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_selection_conflict'):
            self.legacy(self.b)
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_selection_conflict'):
            self.legacy(self.b,expected_selected=self.b)

    def test_admin_target_checks_idempotency_selection_trash_and_existence(self):
        p=self.ingress.current_policy(self.b,'qq');op=identifier()
        args=dict(persona_id=self.b,channel='qq',mode='natural_batch',expected_version=p['id'],operation_id=op,expected_selected=self.a)
        result=self.ingress.update_policy(**args)
        self.assertEqual(result,self.ingress.update_policy(**args))
        with self.assertRaisesRegex(RuntimeErrorBase,'submission_conflict'):
            self.ingress.update_policy(**(args|{'mode':'legacy_per_message'}))
        self.m.trash(self.b,0,self.a,identifier())
        with self.assertRaisesRegex(RuntimeErrorBase,'persona_in_trash'):
            self.legacy(self.b,expected_selected=self.a)
        with self.assertRaisesRegex(RuntimeErrorBase,'Persona not found'):
            self.ingress.update_policy('missing','qq','natural_batch',None,identifier(),expected_selected=self.a)

    def test_accepted_batch_keeps_frozen_defaults_and_switch_requires_resolution(self):
        from persona_runtime.chat import ChatEngine
        engine=ChatEngine(self.store,self.cfg)
        self.addCleanup(engine.cognitive.characters.close)
        ingress=IngressService(engine)
        with patch.object(engine,'_schedule'):
            receipt=ingress.accept({'channel':'desktop','text':'今晚想聊聊','request_id':identifier(),'persona_id':self.a})
            p=ingress.current_policy(self.a,'desktop')
            with self.assertRaisesRegex(RuntimeErrorBase,'pending_batch_resolution_required'):
                ingress.update_policy(self.a,'desktop','legacy_per_message',p['id'],identifier())
            ingress.update_policy(self.a,'desktop','legacy_per_message',p['id'],identifier(),
                reply_mode='legacy_full',character_retrieval_mode='lexical',pending_action='cancel')
            with self.store.connection() as db:
                batch=dict(db.execute('SELECT * FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone())
            frozen=json.loads(batch['policy_json'])
            self.assertEqual(batch['state'],'cancelled')
            self.assertEqual(frozen['reply_mode'],'natural_structured')
            self.assertEqual(frozen['context']['character_retrieval']['mode'],'hybrid_llm')


class FeatureDefaultsAPITests(unittest.IsolatedAsyncioTestCase):
    call=management_base.ManagementAPITests.call

    async def asyncSetUp(self):
        await management_base.ManagementAPITests.asyncSetUp(self)

    async def test_nonselected_explicit_target_is_admin_only_and_does_not_switch(self):
        status,p=await self.call('interaction.current',{'persona_id':self.b,'channel':'desktop'})
        self.assertEqual(status,200)
        args={'persona_id':self.b,'channel':'desktop','mode':'natural_batch','expected_version':p['id'],
              'operation_id':identifier(),'expected_selected':self.a,'reply_mode':'natural_structured',
              'character_retrieval_mode':'hybrid_llm'}
        status,_=await self.call('interaction.update',args,role='bridge');self.assertEqual(status,403)
        status,result=await self.call('interaction.update',args);self.assertEqual(status,200)
        self.assertEqual(result['reply_mode'],'natural_structured')
        self.assertEqual(self.service.show_persona()['id'],self.a)
        status,_=await self.call('interaction.update',args|{'expected_selected':self.b,'operation_id':identifier()})
        self.assertNotEqual(status,200)


if __name__=='__main__':unittest.main()
