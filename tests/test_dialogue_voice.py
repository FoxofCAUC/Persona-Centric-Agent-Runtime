"""Expression freedom is frozen and cannot change hard source/call contracts."""
import json
import unittest
from unittest.mock import patch

from persona_runtime.dialogue_voice import freeze,read,render,bubble_contract,VERSION
from persona_runtime.decision_capsule import capsule
from persona_runtime.provider_compat import build_request
from tests import test_m762 as fixture


class Voice(unittest.TestCase):
    def test_reproducible_variation_and_precise_task_sampling(self):
        a=freeze('stable-case',['今天发生件好笑的事']);b=freeze('stable-case',['今天发生件好笑的事'])
        self.assertEqual(a,b);self.assertEqual(render(a),render(b))
        self.assertGreater(a['temperature'],freeze('code',['给我完整代码'])['temperature'])
        self.assertGreater(len({render(freeze(str(i),['你好']))[1]['variation'] for i in range(50)}),1)
        self.assertIsNone(read({'model_snapshot':'{}'}))
        with self.assertRaisesRegex(Exception,'dialogue_profile_invalid'):
            read({'model_snapshot':{'_dialogue_profile':a|{'temperature':float('nan')}}})

    def test_wire_profile_is_explicit_and_auxiliary_unchanged(self):
        m={'base_url':'https://api.deepseek.com','name':'deepseek-flash','max_tokens':8192}
        plain,_=build_request(m,[{'role':'user','content':'hello'}]);self.assertNotIn('temperature',plain)
        new,_=build_request(m|{'_dialogue_profile':freeze('x',['你好'])},[{'role':'user','content':'hello'}])
        self.assertEqual(new['temperature'],1.1)
        self.assertNotIn('_dialogue_profile',new)
        self.assertNotIn('8至35',bubble_contract())
        self.assertNotIn('确认与追问',bubble_contract())


class Integrated(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.M762.asyncSetUp
    asyncTearDown=fixture.M762.asyncTearDown
    source=fixture.M762.source;document=fixture.M762.document;publish=fixture.M762.publish
    admit=fixture.M762.admit;mode=fixture.M762.mode;queued=fixture.M762.queued

    async def test_frozen_v2_context_has_soft_voice_and_exact_manifest(self):
        self.publish();self.admit();self.mode('assist')
        cfg=self.config.load();cfg['model']['_dialogue_profile']=freeze('artificial-test',['我想找人倾诉'])
        from persona_runtime.config import atomic_json
        atomic_json(self.config.file,cfg)
        tid=await self.queued()
        prepared=self.engine.cognitive.prepare(tid)
        text=prepared['messages'][0]['content']
        self.assertIn('这一轮更在意',text);self.assertIn('现在是在私聊',text)
        self.assertNotIn('本轮表达线索',text);self.assertNotIn('先简短回应用户',text)
        self.assertEqual(prepared['manifest']['dialogue_voice']['version'],VERSION)
        self.assertEqual(prepared['manifest']['decision_capsule']['renderer'],'decision-capsule-voice-v2')
        self.assertEqual(self.fake.calls,[])
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            final=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertEqual(prepared['messages'],final['messages'])

    async def test_withdrawal_rebuild_removes_voice_capsule(self):
        head=self.publish();self.admit();self.mode('assist')
        cfg=self.config.load();cfg['model']['_dialogue_profile']=freeze('withdrawal',['我想找人倾诉'])
        from persona_runtime.config import atomic_json
        atomic_json(self.config.file,cfg);tid=await self.queued();prepared=self.engine.cognitive.prepare(tid)
        self.dispositions.withdraw(self.pid,head['active_id'],head['revision'],'withdraw-v2')
        with self.store.connection(write=True) as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
            final=self.engine.cognitive.finalize(db,turn,prepared)
        self.assertNotIn('这一轮更在意',final['messages'][0]['content'])
        self.assertFalse(final['manifest']['decision_capsule']['applied_to_prompt'])

    async def test_explicit_comparison_keeps_authorities_and_freezes_treatment(self):
        from contextlib import closing
        import sqlite3
        from persona_runtime.chat import ChatEngine
        from persona_runtime.config import ConfigStore,atomic_json
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.storage import Store
        from persona_runtime.decision_capsule import AssistAdmission
        from persona_runtime.ingress import IngressService
        from persona_runtime.evaluation_material import EvaluationSession
        from persona_runtime.models import identifier
        self.publish();engines={};turns={}
        try:
            for arm in 'ABC':
                paths=RuntimePaths(self.paths.home/arm);paths.initialize();store=Store(paths)
                with self.store.connection() as db:
                    with closing(sqlite3.connect(store.path)) as dest:db.backup(dest)
                cfg=ConfigStore(paths);cfg.initialize();value=cfg.load();value['model']=self.config.load()['model']
                if arm=='C':value['model']['_dialogue_profile']=freeze('paired',['我想找人倾诉'])
                atomic_json(cfg.file,value)
                grant=AssistAdmission(paths.home,value['instance_id'],frozenset((self.pid,)),self.paths.home)
                engine=ChatEngine(store,cfg,self.fake,assist_admission=grant);engines[arm]=engine
                ingress=IngressService(engine);p=ingress.current_policy(self.pid,'desktop')
                ingress.update_policy(self.pid,'desktop',p['mode'],p['id'],identifier(),disposition_mode='assist' if arm=='C' else 'baseline')
                with patch.object(engine,'_schedule'):
                    row=await engine.submit('desktop','我想找人倾诉',identifier(),persona_id=self.pid)
                turns[arm]=row['id']
            with self.assertRaisesRegex(Exception,'material_scope_mismatch'):
                EvaluationSession.register_independent(engines,turns)
            sessions=EvaluationSession.register_independent(engines,turns,prompt_comparison=True)
            contexts={arm:engines[arm].cognitive.prepare(turns[arm]) for arm in 'BC'}
            proof=sessions['C'].verify_independent(turns,contexts)
            self.assertNotEqual(proof['B'],proof['C'])
            with engines['B'].store.connection() as db:
                bturn=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns['B'],)).fetchone())
            sessions['B'].capture(engines['B'],bturn,contexts['B']['character_prepared'])
            with engines['C'].store.connection(write=True) as db:
                db.execute("UPDATE turns SET status='failed' WHERE id=?",(turns['C'],))
            engines['C'].service.create_session()
            with patch.object(engines['C'],'_schedule'):
                retry=await engines['C'].submit('desktop','我想找人倾诉',identifier(),persona_id=self.pid)
            supplemental=EvaluationSession.register_independent(engines,turns|{'C':retry['id']},material_turns=turns,prompt_comparison=True)
            with engines['C'].store.connection() as db:
                newturn=dict(db.execute('SELECT * FROM turns WHERE id=?',(retry['id'],)).fetchone())
                self.assertEqual(supplemental['C'].get(engines['C'],db,newturn)['arm'],'C')
            # Restore original isolated comparison to exercise treatment guard.
            EvaluationSession.register_independent(engines,turns,prompt_comparison=True)
            with engines['C'].store.connection(write=True) as db:
                row=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns['C'],)).fetchone())
                sessions['C'].before_dispatch(engines['C'],row,contexts['C'])
                model=json.loads(row['model_snapshot']);model['_dialogue_profile']['seed']='tampered'
                row['model_snapshot']=json.dumps(model)
                with self.assertRaisesRegex(Exception,'prompt_treatment_changed'):
                    sessions['C'].get(engines['C'],db,row)
            self.assertEqual(self.fake.calls,[])
        finally:
            for engine in engines.values():
                await engine.wait();await engine.observer.drain();engine.cognitive.shadow.close();engine.cognitive.characters.close()
