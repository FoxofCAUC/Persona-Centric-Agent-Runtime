"""Frozen selector prompt dispatch; no real provider calls."""
import json
import unittest
from unittest.mock import patch

from tests import test_m754r2 as prior
from persona_runtime.character_embedding import digest
from persona_runtime.character_policy import choose, checked
from persona_runtime.character_selector_protocol import messages, prompt_for, PROMPT_VERSION, SYSTEM
from persona_runtime.models import RuntimeErrorBase


class PromptTests(unittest.TestCase):
    def test_new_default_and_legacy_contract(self):
        policy=choose('hybrid_llm')
        self.assertEqual(policy['selector_contract']['prompt'],'character-selector-v2')
        policy['selector_contract']['prompt']='character-selector-v1'
        self.assertEqual(checked(policy),policy)
        self.assertEqual(prompt_for('character-selector-v1'),SYSTEM)
        self.assertNotEqual(prompt_for(PROMPT_VERSION),SYSTEM)
        for version in ('character-selector-v3',None,{},''):
            policy['selector_contract']['prompt']=version
            with self.subTest(version=version),self.assertRaisesRegex(RuntimeErrorBase,'invalid_character_policy_snapshot'):
                checked(policy)


class FrozenTests(prior.SelectorTests):
    async def test_frozen_v1_then_explicit_retry_v2(self):
        self.enable();turn=await self.turn('书店')
        snapshot=json.loads(self.raw(turn['id'])['context_policy_snapshot'])
        snapshot['character_retrieval']['selector_contract']['prompt']='character-selector-v1'
        with self.store.connection(write=True) as db:
            db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps(snapshot),turn['id']))
        self.policy(character_retrieval_mode='hybrid_llm',cross_session=True)
        prompts=[]
        async def provider(config,payload):
            prompts.append(payload[0]['content'])
            return '{"schema_version":1,"lore_ids":[],"example_ids":[]}',{'total_tokens':3}
        with patch.object(self.rt,'local_pool',side_effect=self.pool),patch.object(self.model,'complete',side_effect=provider):
            await self.engine.selector.prepare(turn['id'])
            self.engine.selector.release(turn['id'])
            with self.store.connection(write=True) as db:
                db.execute("UPDATE turns SET status='interrupted' WHERE id=?",(turn['id'],))
            with patch.object(self.engine,'_schedule'):
                await self.engine.retry(turn['id'])
            await self.engine.selector.prepare(turn['id'])
        self.assertEqual(prompts,[prompt_for('character-selector-v1'),prompt_for('character-selector-v2')])
        with self.store.connection() as db:
            rows=[json.loads(r[0]) for r in db.execute("SELECT manifest_json FROM context_manifests WHERE task_type='character_selection' ORDER BY rowid")]
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        self.assertEqual([r['prompt_version'] for r in rows],['character-selector-v1','character-selector-v2'])
        self.assertEqual([r['prompt_hash'] for r in rows],[digest(p) for p in prompts])

    async def test_unknown_frozen_version_zero_model_calls(self):
        self.enable();turn=await self.turn('书店')
        snapshot=json.loads(self.raw(turn['id'])['context_policy_snapshot'])
        snapshot['character_retrieval']['selector_contract']['prompt']='unregistered'
        with self.store.connection(write=True) as db:
            db.execute('UPDATE turns SET context_policy_snapshot=? WHERE id=?',(json.dumps(snapshot),turn['id']))
        with patch.object(self.model,'complete') as provider:
            await self.engine._run(turn['id'])
        provider.assert_not_called()
        self.assertEqual(self.engine.get(turn['id'])['error_code'],'invalid_character_policy_snapshot')
        self.assertEqual(self.engine.selector.list(self.pid),[])

    async def test_prompt_ledger_mismatch_zero_calls_and_no_partial_ledger(self):
        self.enable();turn=await self.turn('书店')
        version=json.loads(self.raw(turn['id'])['character_snapshot'])
        pool=self.pool(version,{},['书店']);model=self.config.load()['character_selector']
        payload=messages(pool,['书店'],'',2,48000,prompt_version='character-selector-v1')
        with patch.object(self.model,'complete') as provider:
            with self.assertRaisesRegex(RuntimeErrorBase,'selector_prompt_snapshot_mismatch'):
                await self.engine.selector._call(self.pid,self.raw(turn['id']),model,payload,pool,2,prompt_version='character-selector-v2')
        provider.assert_not_called()
        self.assertFalse(self.engine.selector.busy)
        with self.store.connection() as db:
            for table in ('character_selection_attempts','context_manifests','quota_reservations'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)


for name in dir(prior.SelectorTests):
    if name.startswith('test_') and name not in FrozenTests.__dict__:
        setattr(FrozenTests,name,None)
