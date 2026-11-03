"""Synthetic receipts validate stopped-batch evidence, without clients or calls."""
import json
from pathlib import Path
import tempfile
import unittest
from scripts.dialogue_communication_real_audit import batch,fp

class SettlementAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='cr31-audit-',dir='D:/Persona-Centric Agent Runtime/tmp')
        self.p=Path(self.tmp.name);(self.p/'receipts').mkdir();(self.p/'results').mkdir()
        self.write('state.json',{'status':'stopped','reason':'provider_retries_exhausted'})
        self.msg=[{'role':'user','content':'synthetic only'}]
        self.r={'id':'0001','slot':'g/raw/main','real':True,'simulation':False,'request_hash':'wire','messages':self.msg,'input_hash':fp(self.msg)}
        self.t={'real':True,'simulation':False,'status':'completed','reservation_hash':fp(self.r),'runtime_attempt':{'request_hash':'wire'}}
        self.write('receipts/0001.reserve.json',self.r);self.write('receipts/0001.terminal.json',self.t)
        self.write('receipts/0001.transport.json',{'real':True,'status':'returned'})
        self.result={'real':True,'simulation':False,'id':'g-raw','group':'g','status':'completed','messages':self.msg,'request_hash':'wire','reply':'synthetic','calls':['0001'],'manifest':{'input_snapshot_hash':fp(self.msg)}}
        self.write('results/g-raw.json',self.result)
    def tearDown(self):self.tmp.cleanup()
    def write(self,n,v):(self.p/n).write_text(json.dumps(v),encoding='utf-8')
    def test_stopped_audit_is_not_completed_batch(self):
        v=batch(self.p);self.assertTrue(v['audit_passed']);self.assertFalse(v['batch_complete']);self.assertEqual(v['physical_calls'],1)
    def test_unsettled_receipt_rejected(self):
        (self.p/'receipts/0001.terminal.json').unlink()
        with self.assertRaisesRegex(ValueError,'unsettled'):batch(self.p)
    def test_hash_change_rejected(self):
        self.r['messages'][0]['content']='changed';self.write('receipts/0001.reserve.json',self.r)
        with self.assertRaisesRegex(ValueError,'hash'):batch(self.p)
    def test_cross_group_alias_rejected(self):
        alias=self.result|{'id':'other-full','group':'other','status':'aliased','canonical_id':'g-raw'};alias.pop('manifest')
        self.write('results/other-full.json',alias)
        with self.assertRaisesRegex(ValueError,'bad_alias'):batch(self.p)
    def test_alias_has_no_fabricated_manifest(self):
        alias=self.result|{'id':'g-full','status':'aliased','canonical_id':'g-raw'}
        self.write('results/g-full.json',alias)
        with self.assertRaisesRegex(ValueError,'fake_manifest'):batch(self.p)

if __name__=='__main__':unittest.main()
