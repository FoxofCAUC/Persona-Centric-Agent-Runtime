"""R06 execution protections, never using the frozen independent fixture."""
import json
from pathlib import Path
import tempfile
import unittest
from persona_runtime.config import atomic_json
from scripts.m763_r04 import model_identity
from scripts.m763_r06 import validate_provider,RunState


class Protections(unittest.TestCase):
    def test_provider_switch_cannot_mix_or_rewrite_frozen_matrix(self):
        from scripts.m763_r06 import validate_model_cohort
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);approved={'model':{'name':'grok-4.7'},'character_selector':{'name':'grok-4.7'}}
            original={'models':{k:model_identity(v) for k,v in approved.items()}}
            atomic_json(root/'execution-freeze.json',original);before=(root/'execution-freeze.json').read_bytes()
            validate_model_cohort(root,approved)
            with self.assertRaisesRegex(ValueError,'evaluation_model_cohort_changed'):
                validate_model_cohort(root,approved|{'model':{'name':'deepseek-flash'}})
            self.assertEqual((root/'execution-freeze.json').read_bytes(),before)

    def test_resume_requires_exact_tool_evidence_without_changing_labels_or_models(self):
        from copy import deepcopy
        from scripts.m763_r06 import validate_repair
        old={'source_hashes':{'persona_runtime/chat.py':'original'},'runner_hash':'old','fixture_hash':'frozen','prepared_hash':'prepared','models':{'model':'approved'}}
        identity=deepcopy(old)|{'runner_hash':'new','provider_gate_hash':'gate'}
        tests={'passed':True,'source_hashes':identity['source_hashes'],'runner_hash':'new','provider_gate_hash':'gate'}
        validate_repair(old,identity,tests)
        for item in (identity|{'fixture_hash':'changed'},identity|{'source_hashes':{'persona_runtime/chat.py':'changed'}},identity|{'provider_gate_hash':'untested'}):
            with self.assertRaisesRegex(ValueError,'Repair evidence'):validate_repair(old,item,tests)

    def test_terminal_failure_keeps_its_slot_and_does_not_replay_or_block_others(self):
        from scripts.m763_r06 import pending_slots
        rows=[{'case_id':'synthetic-failed','arm':'A','state':'failed','error':'provider_network_error'},
              {'case_id':'synthetic-success','arm':'C','state':'success'},
              {'case_id':'synthetic-pending','arm':'B','state':'accepted'}]
        ledger=type('Ledger',(),{'schedule':lambda _,package:list(rows)})()
        self.assertEqual([r['case_id'] for r in pending_slots(ledger)],['synthetic-pending'])
        self.assertEqual(rows[0]['state'],'failed');self.assertEqual(len(rows),3)

    def test_unknown_or_running_must_not_be_redispatched(self):
        from scripts.m763_r06 import pending_slots
        for state in ('fee_unknown','running','not_called'):
            ledger=type('Ledger',(),{'schedule':lambda _,package:[{'case_id':'synthetic','arm':'B','state':state}]})()
            with self.subTest(state=state),self.assertRaisesRegex(ValueError,'Resolve original ledger slot'):
                list(pending_slots(ledger))

    def test_provider_expiry_failure_and_both_models_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'check.json';approved={'model':{'name':'primary','timeout':180},'character_selector':{'name':'auxiliary','timeout':60}}
            value={'normal':True,'checks':[{'kind':k,'normal':True,'checked_epoch':100,'model_fingerprint':model_identity(approved[p])} for k,p in (('main','model'),('auxiliary','character_selector'))]}
            atomic_json(path,value);validate_provider(path,approved,now=101)
            with self.assertRaises(ValueError):validate_provider(path,approved,now=701)
            with self.assertRaises(ValueError):validate_provider(path,approved|{'model':{'name':'changed'}},now=101)
            value['normal']=False;atomic_json(path,value)
            with self.assertRaises(ValueError):validate_provider(path,approved,now=101)

    def test_saved_failure_stop_cannot_be_reset_by_new_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            state=RunState(root);state.value['stop']='single_failure_over_600_seconds';atomic_json(state.path,state.value)
            with self.assertRaisesRegex(ValueError,'model_tests_stopped'):RunState(root).check()

    def test_mock_or_missing_attempt_is_not_completed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'dispatch').mkdir()
            state=RunState(root);state.collect()
            self.assertEqual(state.value['processed'],[]);self.assertEqual(state.value['consecutive_failures'],0)


class EvidenceAudit(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close)
        self.db.executescript('''
          CREATE TABLE attempts(id,turn_id,model_snapshot,started_epoch,status,usage_json);
          CREATE TABLE situation_observation_attempts(id,turn_id,execution_revision,started_epoch,status,usage_json,model_snapshot);
          CREATE TABLE context_manifests(id,turn_id,persona_id,attempt_id,observation_attempt_id,manifest_json);
        ''')
        from persona_runtime.archive import fingerprint
        self.model={'name':'synthetic-primary'};self.model_snapshot=json.dumps(self.model)
        self.manifest={'input_snapshot_hash':'input-hash','task_type':'dialogue','model_snapshot_hash':fingerprint(self.model)}
        self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',('attempt','turn',self.model_snapshot,100,'completed','{"total_tokens":5}'))
        self.db.execute('INSERT INTO context_manifests VALUES(?,?,?,?,?,?)',('manifest','turn','artificial','attempt',None,json.dumps(self.manifest)))
        self.turn={'id':'turn','persona_id':'artificial','execution_revision':1,'model_snapshot':self.model_snapshot}
        self.trace={'purpose':'main','attempt_id':'attempt','manifest_id':'manifest','request_hash':'input-hash','dispatch_epoch':101,'state':'response_received','finished_epoch':102,'arm':'B','model_fingerprint':model_identity(self.model)}

    def test_actual_primary_hash_and_persisted_attempt(self):
        from scripts.check_m763_r06 import check_trace
        result=check_trace(self.db,self.trace,self.turn)
        self.assertTrue(result['finished']);self.assertEqual(result['known_tokens'],5)
        with self.assertRaisesRegex(ValueError,'dispatch_attempt_binding'):
            check_trace(self.db,self.trace|{'attempt_id':'missing'},self.turn)

    def test_scope_hash_model_and_order_tampering_rejected(self):
        from scripts.check_m763_r06 import check_trace
        for trace,turn,code in (
            (self.trace|{'request_hash':'modified'},self.turn,'dispatch_input_hash'),
            (self.trace,self.turn|{'persona_id':'other'},'dispatch_manifest_binding'),
            (self.trace,self.turn|{'model_snapshot':'other'},'dispatch_primary_frozen_binding'),
            (self.trace|{'dispatch_epoch':99},self.turn,'dispatch_before_registration'),
            (self.trace,self.turn|{'execution_revision':2},'dispatch_primary_frozen_binding')):
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,code):check_trace(self.db,trace,turn)

    def test_auxiliary_wrong_arm_and_wrong_revision_rejected(self):
        from scripts.check_m763_r06 import check_trace
        self.db.execute('INSERT INTO situation_observation_attempts VALUES(?,?,?,?,?,?,?)',('observation','turn',2,100,'completed',None,self.model_snapshot))
        self.db.execute('INSERT INTO context_manifests VALUES(?,?,?,?,?,?)',('observation-manifest','turn','artificial',None,'observation','{"input_snapshot_hash":"input-hash"}'))
        trace=self.trace|{'purpose':'understanding','attempt_id':'observation','manifest_id':'observation-manifest'}
        with self.assertRaisesRegex(ValueError,'dispatch_arm_purpose'):check_trace(self.db,trace,self.turn)
        with self.assertRaisesRegex(ValueError,'dispatch_revision_binding'):check_trace(self.db,trace|{'arm':'C'},self.turn)

    def test_purpose_and_model_hash_must_match_actual_attempt(self):
        from scripts.check_m763_r06 import check_trace
        for change,code in (({'task_type':'provider_probe'},'dispatch_manifest_purpose'),({'model_snapshot_hash':'invented'},'dispatch_model_identity')):
            self.db.execute('UPDATE context_manifests SET manifest_json=?',(json.dumps(self.manifest|change),))
            with self.subTest(code=code),self.assertRaisesRegex(ValueError,code):check_trace(self.db,self.trace,self.turn)
        self.db.execute('UPDATE context_manifests SET manifest_json=?',(json.dumps(self.manifest),))
        with self.assertRaisesRegex(ValueError,'dispatch_model_identity'):
            check_trace(self.db,self.trace|{'model_fingerprint':'another-connection'},self.turn)
