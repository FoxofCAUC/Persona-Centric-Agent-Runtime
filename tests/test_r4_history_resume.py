from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch
from scripts import r4_history_resume as resume
from scripts.r4_expression_realization import StopRun

class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.p={'real':False,'items':[{'key':'done'},{'key':'pending'}]}
        self.values={'done':{'real':False,'turn':{'status':'completed'},'attempts':[],'selection_attempts':[]}}
        self.rows=[{'item':'done','real':False,'status':'completed'}]
    def test_success_retained_pending_only_and_order_stable(self):
        with patch.object(resume.common,'reconcile'):
            done,pending=resume.pending_items(self.p,self.values,self.rows)
        self.assertEqual(done,['done']);self.assertEqual(pending,[{'key':'pending'}])
    def test_dispatch_without_saved_result_is_never_resumed(self):
        with patch.object(resume.common,'reconcile'):
            with self.assertRaisesRegex(StopRun,'called_position'):resume.pending_items(self.p,self.values,self.rows+[{'item':'pending','real':False,'status':'completed'}])
    def test_real_package_or_receipt_rejected(self):
        with self.assertRaisesRegex(StopRun,'real_continuation'):resume.pending_items(dict(self.p,real=True),{},[])
        with self.assertRaises(StopRun):resume.pending_items(self.p,self.values,[dict(self.rows[0],real=True)])
    def test_duplicate_and_incomplete_result_rejected(self):
        with self.assertRaises(StopRun):resume.pending_items({'real':False,'items':[{'key':'x'}]*2},{},[])
        values=deepcopy(self.values);values['done']['turn']['status']='failed'
        with self.assertRaises(StopRun):resume.pending_items(self.p,values,self.rows)

class CopySettlementTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.executescript('CREATE TABLE turns(id,status,cancelled_at,error_code);CREATE TABLE attempts(id,turn_id,status,usage_json,error_code);CREATE TABLE quota_reservations(turn_id,state);')
        self.db.execute("INSERT INTO turns VALUES('t','running',NULL,NULL)");self.db.execute("INSERT INTO attempts VALUES('a','t','running',NULL,NULL)")
    def test_predispatch_orphan_kept_as_interrupted_without_fabricating_usage(self):
        changes=resume.settle_uncalled_copy(self.db,'t')
        self.assertFalse(changes[0]['physical_call']);self.assertEqual(self.db.execute('SELECT status,usage_json FROM attempts').fetchall(),[('interrupted',None)])
        self.assertEqual(self.db.execute('SELECT status FROM turns').fetchone()[0],'queued')
    def test_attempt_with_runtime_audit_or_active_reservation_rejected(self):
        self.db.execute("UPDATE attempts SET usage_json='{}'")
        with self.assertRaises(StopRun):resume.settle_uncalled_copy(self.db,'t')
        self.db.execute('UPDATE attempts SET usage_json=NULL');self.db.execute("INSERT INTO quota_reservations VALUES('t','active')")
        with self.assertRaises(StopRun):resume.settle_uncalled_copy(self.db,'t')
    def test_already_completed_never_reset(self):
        self.db.execute("UPDATE turns SET status='completed'")
        with self.assertRaises(StopRun):resume.settle_uncalled_copy(self.db,'t')
    def test_clean_queued_turn_unchanged(self):
        self.db.execute('DELETE FROM attempts');self.db.execute("UPDATE turns SET status='queued'")
        self.assertEqual(resume.settle_uncalled_copy(self.db,'t'),[])
