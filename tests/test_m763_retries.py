import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.config import atomic_json
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.paths import RuntimePaths
from scripts.m763_retries import RetryQueue,VERSION,current_slot


class RetryPolicy(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.ledger=EvaluationStore(RuntimePaths(self.root));self.package='synthetic-retry-policy'
        self.ledger.create(self.package,[{'id':f'c{i}','scenario':'合成重试工具检查','author_summary':'人工资料'} for i in range(100)],{},simulated=True)
        with self.ledger.connect(self.package,True) as db:db.execute("UPDATE slots SET state='accepted'")
        atomic_json(self.root/'retry-policy.json',{'version':VERSION,'package_id':self.package,'immediate_retry_opportunities':3})
        self.queue=RetryQueue(self.root,self.ledger,self.package)
        self.ledger.transition(self.package,'c0','A','accepted','running',attempt_id='original')
        self.ledger.transition(self.package,'c0','A','running','failed',error='provider_network_error')

    def finish_retry(self,result='failed',dispatched=True,phase='immediate'):
        slot=current_slot(self.ledger,self.package,'c0','A');entry=self.queue.start(slot,phase)
        operation=self.ledger.supplement(self.package,'c0','A',entry['reason'],entry['operation_id'])
        entry.update(request_id=operation['request_id'],status='accepted');self.queue.save()
        self.ledger.transition(self.package,'c0','A','not_called','accepted')
        aid='synthetic-retry-'+str(len(self.queue.value['entries']))
        if dispatched:
            self.ledger.transition(self.package,'c0','A','accepted','running',attempt_id=aid)
            folder=self.root/'dispatch';folder.mkdir(exist_ok=True)
            (folder/('main-'+aid+'.json')).write_text('{"synthetic":true}',encoding='utf-8')
            self.ledger.transition(self.package,'c0','A','running',result,attempt_id=aid,
                output='合成通过输出' if result=='success' else None,error=None if result=='success' else 'provider_network_error')
        else:self.ledger.transition(self.package,'c0','A','accepted','failed',error='request_failed')
        self.queue.finish(entry)
        return entry

    def test_three_retries_then_other_slots_then_tail_without_erasing_failures(self):
        for _ in range(3):self.finish_retry()
        item=self.queue.next();self.assertEqual(item[1],'original');self.assertNotEqual(item[0]['case_id'],'c0')
        self.assertEqual(self.queue.used('c0/A','immediate'),3)
        with self.ledger.connect(self.package,True) as db:
            db.execute("UPDATE slots SET state='success' WHERE state='accepted'")
            audits=db.execute("SELECT body FROM audit WHERE kind='supplement'").fetchall()
        self.assertEqual(len(audits),3)
        self.assertEqual(json.loads(audits[0][0])['previous']['attempt_id'],'original')
        self.assertEqual(self.queue.next()[1],'tail')
        self.finish_retry(result='success',phase='tail')
        self.assertIsNone(self.queue.next())

    def test_first_retry_success_is_passed_and_cannot_replay(self):
        entry=self.finish_retry(result='success')
        self.assertEqual(entry['status'],'success');self.assertEqual(self.queue.used('c0/A','immediate'),1)
        with self.assertRaisesRegex(ValueError,'retry_requires_failed_unusable_output'):
            self.queue.start(current_slot(self.ledger,self.package,'c0','A'),'immediate')
        self.assertEqual(current_slot(self.ledger,self.package,'c0','A')['state'],'success')

    def test_interrupted_preparation_reuses_same_operation(self):
        entry=self.queue.start(current_slot(self.ledger,self.package,'c0','A'),'immediate')
        first=self.ledger.supplement(self.package,'c0','A',entry['reason'],entry['operation_id'])
        recovered=RetryQueue(self.root,self.ledger,self.package)
        slot,phase=recovered.next();same=recovered.start(slot,phase)
        second=self.ledger.supplement(self.package,'c0','A',same['reason'],same['operation_id'])
        self.assertEqual(first,second);self.assertEqual(len(recovered.value['entries']),1)

    def test_unknown_fee_and_running_are_not_implicitly_retried(self):
        with self.ledger.connect(self.package,True) as db:db.execute("UPDATE slots SET state='fee_unknown' WHERE case_id='c1' AND arm='C'")
        with self.assertRaisesRegex(ValueError,'retry_reconciliation_required'):self.queue.next()

    def test_non_dispatched_failure_is_not_a_charged_retry_or_infinite_loop(self):
        with self.assertRaisesRegex(ValueError,'requires_repair'):self.finish_retry(dispatched=False)
        self.assertEqual(self.queue.used('c0/A','immediate'),0)
        self.assertIsNone(self.queue.value['active'])
        with self.assertRaisesRegex(ValueError,'requires_repair'):RetryQueue(self.root,self.ledger,self.package).next()

    def test_replacement_receipt_resolves_turn_through_actual_batch_foreign_key(self):
        import sqlite3
        from contextlib import nullcontext
        from unittest.mock import patch
        from persona_runtime.archive import fingerprint
        from persona_runtime.storage import Store
        from scripts.m763_retries import effective_case
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;self.addCleanup(db.close)
        db.executescript('CREATE TABLE ingress_receipts(request_id,channel,batch_id); CREATE TABLE turns(id,persona_id,input_batch_id,model_snapshot);')
        db.execute("INSERT INTO ingress_receipts VALUES('new-request','desktop','batch')")
        db.execute("INSERT INTO turns VALUES('new-turn','artificial','batch','frozen')")
        canonical={'case_id':'synthetic','persona_id':'artificial','runtime_roots':{'A':str(self.root)},
                   'turns':{'A':'old-A','B':'old-B','C':'old-C'},'frozen':{'A':{'model_snapshot':fingerprint('frozen')}}}
        atomic_json(self.root/'retry-views.json',{'synthetic/A':{'request_id':'new-request','turn_id':'new-turn'}})
        with patch.object(Store,'connection',return_value=nullcontext(db)):
            result=effective_case(self.root,canonical)
            self.assertEqual(result['turns']['A'],'new-turn');self.assertEqual(canonical['turns']['A'],'old-A')
            self.assertEqual(result['material_turns'],canonical['turns'])
            db.execute("UPDATE ingress_receipts SET batch_id='wrong'")
            with self.assertRaisesRegex(ValueError,'retry_view_binding'):effective_case(self.root,canonical)
