"""R04 runner durability/audit tests. No provider or formal runtime access."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from scripts.m763_r04 import (model_identity,validate_check,claim_registry,run,
                             persist_preflight,reconcile,recover_audit,long_failure)
from scripts.inspect_m763_repair_evidence import inspect_real
from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from tests import test_m763_repair as fixtures


class Binding(unittest.TestCase):
    def test_long_failure_boundary_success_and_persistent_stop(self):
        with TemporaryDirectory() as temp:
            out=Path(temp);state={'slots':{'u01':{'state':'failed','elapsed_ms':600000}}}
            self.assertIsNone(long_failure(out,state))
            state['slots']['u01']['elapsed_ms']=600001
            stop=long_failure(out,state);self.assertEqual(stop['reason'],'failure_over_600_seconds')
            state['model_tests_stopped']=stop;state['slots'].clear()
            self.assertEqual(long_failure(out,state),stop)
            self.assertIsNone(long_failure(out,{'slots':{'u01':{'state':'completed','elapsed_ms':700000}}}))

    def test_interrupted_result_file_cannot_hide_long_transport_failure(self):
        with TemporaryDirectory() as temp:
            out=Path(temp);(out/'dispatch').mkdir()
            atomic_json(out/'dispatch/a.json',{'attempt_id':'a','state':'response_unknown','elapsed_ms':601000})
            self.assertEqual(long_failure(out,{'slots':{}})['attempt_id'],'a')

    def test_model_parameters_and_credentials_bound_quotas_excluded(self):
        model={'name':'a','encrypted_key':'protected','timeout':60,'hourly_limit':10}
        self.assertEqual(model_identity(model),model_identity(model|{'hourly_limit':0}))
        for change in ({'name':'b'},{'encrypted_key':'different'},{'timeout':15}):
            self.assertNotEqual(model_identity(model),model_identity(model|change))

    def test_false_expired_wrong_connection_and_outside_check_rejected(self):
        with TemporaryDirectory() as temp:
            home=Path(temp);folder=home/'exports/acceptance';folder.mkdir(parents=True);path=folder/'check.json'
            model={'name':'a'};good={'normal':True,'configured_model_listed':True,'checked_epoch':100,
                                   'connection_fingerprint':model_identity(model)}
            atomic_json(path,good);self.assertTrue(validate_check(home,path,model,now=101)['normal'])
            for change in ({'normal':False},{'checked_epoch':-501},{'checked_epoch':102},{'connection_fingerprint':'wrong'}):
                atomic_json(path,good|change)
                with self.assertRaises(ValueError):validate_check(home,path,model,now=101)
            with self.assertRaises(ValueError):validate_check(home,home/'outside',model,now=101)

    def test_registry_rejects_fresh_directory_bypass(self):
        with TemporaryDirectory() as temp:
            home=Path(temp);(home/'exports/acceptance').mkdir(parents=True)
            first=home/'exports/acceptance/first';claim_registry(home,first,True);claim_registry(home,first,True)
            with self.assertRaises(ValueError):claim_registry(home,home/'exports/acceptance/second',True)


class Runner(unittest.IsolatedAsyncioTestCase):
    async def test_long_failure_stop_rejects_resume_before_any_runtime_or_call(self):
        with TemporaryDirectory() as temp:
            home=Path(temp);out=home/'exports/acceptance/mock';out.mkdir(parents=True)
            from scripts.m763_r04 import CONTRACT
            atomic_json(out/'results.json',{'mode':'mock','contract':CONTRACT,'slots':{},
                'model_tests_stopped':{'reason':'failure_over_600_seconds'}})
            with patch('scripts.m763_r04.Store.initialize',side_effect=AssertionError('no runtime mutation')):
                with self.assertRaisesRegex(ValueError,'only non-call closure'):
                    await run(home,out,recheck='u01',reason='cannot bypass stop')

    async def test_twelve_preflights_resume_and_gate_does_not_accept_mock_as_real(self):
        with TemporaryDirectory() as temp:
            home=Path(temp);out=home/'exports/acceptance/mock';out.mkdir(parents=True)
            await run(home,out)
            state=json.loads((out/'results.json').read_text(encoding='utf-8'))
            self.assertTrue(state['all_contracts_passed']);self.assertTrue(state['limits_restored'])
            self.assertEqual((state['understanding_attempts'],state['main_attempts'],state['active_reservations']),(12,0,0))
            before=sorted(p.name for p in (out/'dispatch').glob('*.json'))
            await run(home,out)
            self.assertEqual(before,sorted(p.name for p in (out/'dispatch').glob('*.json')))
            with self.assertRaisesRegex(ValueError,'Successful output'):
                await run(home,out,recheck='u01',reason='should be refused')
            state=json.loads((out/'results.json').read_text(encoding='utf-8'))
            with closing(sqlite3.connect(out/'runtime/data/database/runtime.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions').fetchone()[0],12)
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
                self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
                reviews=[]
                for key,s in state['slots'].items():
                    result=json.loads(db.execute('SELECT result_json FROM situation_observation_attempts WHERE id=?',(s['observed_attempt_id'],)).fetchone()[0])
                    reviews.append({'case_id':key,'slot_id':key,'attempt_id':s['observed_attempt_id'],
                                    'result_hash':fingerprint(result),'decision_id':s['decision_id'],
                                    'final_input_hash':s['final_input_hash'],'passed':True,
                                    'reason':'Simulated labels; contract test only','reviewer':'execution_assistant'})
            atomic_json(out/'reviews.json',reviews)
            report=inspect_real(out,require_real=False)
            self.assertTrue(all(report['checks'].values()),report)
            self.assertFalse(report['repair_ready_for_R05']);self.assertFalse(inspect_real(out)['checks']['real_execution'])
            atomic_json(out/'reviews.json',reviews[:-1])
            self.assertFalse(inspect_real(out,require_real=False)['checks']['all_semantics_reviewed'])
            atomic_json(out/'reviews.json',reviews)
            with closing(sqlite3.connect(out/'runtime/data/database/runtime.sqlite3')) as db:
                db.execute("UPDATE turns SET observation_model_snapshot='{}' WHERE id=?",(state['slots']['u01']['turn_id'],))
                db.commit()
            self.assertFalse(inspect_real(out,require_real=False)['checks']['all_chains_verified'])


class Audit(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.Repair.asyncSetUp
    asyncTearDown=fixtures.Repair.asyncTearDown
    source=fixtures.Repair.source;document=fixtures.Repair.document;publish=fixtures.Repair.publish
    publish_document=fixtures.Repair.publish_document;observe_setup=fixtures.Repair.observe_setup
    admit=fixtures.Repair.admit;mode=fixtures.Repair.mode;queued=fixtures.Repair.queued

    async def test_preflight_failure_rolls_back_both_objects_and_no_second_call(self):
        self.observe_setup();tid=await self.queued('给我建议');observation=await self.engine.observer.prepare(tid)
        prepared=self.engine.cognitive.prepare(tid,observation=observation)
        original=self.engine.cognitive.persist
        def failed(*a,**kw):original(*a,**kw);raise OSError('injected write failure')
        with patch.object(self.engine.cognitive,'persist',side_effect=failed):
            with self.assertRaises(OSError):persist_preflight(self.engine,tid,prepared)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM disposition_decisions').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM context_manifests WHERE task_type='dialogue'").fetchone()[0],0)
        self.assertEqual(len(self.fake.calls),1)

    async def test_recovery_missing_receipt_does_not_submit(self):
        slot={'request_ids':['unaccepted'],'persona_id':self.pid}
        with patch.object(self.ingress,'accept',side_effect=AssertionError('must not resubmit')):
            reconcile(self.engine,self.ingress,slot)
        self.assertEqual(slot['state'],'not_accepted_interrupted');self.assertEqual(self.fake.calls,[])

    async def test_recovery_finds_committed_observation_without_statefile_id(self):
        self.observe_setup()
        from persona_runtime.models import identifier
        op=identifier()
        with patch.object(self.engine,'_schedule'):
            receipt=self.ingress.accept({'text':'给我建议','request_id':op,'persona_id':self.pid})
        tid=receipt['turn_id'];observed=await self.engine.observer.prepare(tid)
        slot={'request_ids':[op],'persona_id':self.pid,'state':'reserved'}
        reconcile(self.engine,self.ingress,slot)
        self.assertEqual(slot['observed_attempt_id'],observed['attempt_id'])
        self.assertEqual(slot['state'],'completed_observation_audit_pending');self.assertEqual(len(self.fake.calls),1)
        slot['case_id']='u01'
        await recover_audit(self.engine,slot,{'allowed':['needed'],'ask_allowed':True})
        self.assertEqual(slot['state'],'completed');self.assertTrue(slot['contract_correct'])
        self.assertEqual(len(self.fake.calls),1)

    async def test_main_dispatch_is_rejected(self):
        from scripts.m763_r04 import OnlyObservation
        with self.assertRaises(AssertionError):await OnlyObservation(False).complete({},[])

    async def test_foreign_receipt_recovery_rejected(self):
        with patch.object(self.ingress,'lookup',return_value={'persona_id':'foreign'}):
            with self.assertRaises(ValueError):reconcile(self.engine,self.ingress,{'request_ids':['x'],'persona_id':self.pid})
