import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import dialogue_communication_continuation as c

class ContinuationTests(unittest.TestCase):
    def test_successful_groups_skipped(self):
        p={'positions':[{'id':'a','conditions':['raw','full']},{'id':'b','conditions':['raw','full']}]}
        done,pending=c.remaining(p,{'a-raw':{},'a-full':{}})
        self.assertEqual([x['id'] for x in done],['a']);self.assertEqual([x['id'] for x in pending],['b'])
    def test_partial_group_needs_separate_plan(self):
        with self.assertRaisesRegex(Exception,'partial_group'):c.remaining({'positions':[{'id':'a','conditions':['raw','full']}]},{'a-raw':{}})
    def test_root_cannot_be_formal(self):
        with self.assertRaisesRegex(Exception,'outside'):c.confined(c.HOME)
    def test_imported_success_cannot_reserve(self):
        with tempfile.TemporaryDirectory(dir=c.HOME/'tmp') as t:
            p={'real':False,'positions':[{'id':'new','conditions':['raw','full']}]};ledger=c.GuardedLedger(Path(t),p)
            with patch.object(c,'frozen',return_value=p):
                with self.assertRaisesRegex(Exception,'success_replay'):ledger.reserve('old-full','main',{},[])
            self.assertFalse(ledger.rows)
    def test_every_physical_reserve_rechecks_freeze(self):
        with tempfile.TemporaryDirectory(dir=c.HOME/'tmp') as t:
            p={'real':False,'positions':[{'id':'new','conditions':['raw']}]};ledger=c.GuardedLedger(Path(t),p)
            with patch.object(c,'frozen',side_effect=ValueError('source_changed')) as gate:
                with self.assertRaisesRegex(ValueError,'source_changed'):ledger.reserve('new','joint',{},[])
                self.assertEqual(gate.call_count,1)
            self.assertFalse(ledger.rows)
    def test_existing_started_batch_cannot_restart(self):
        with tempfile.TemporaryDirectory(dir=c.HOME/'tmp') as t:
            p=Path(t);(p/'run.started.json').write_text('{}')
            with patch.object(c,'frozen',return_value={'real':False}):
                with self.assertRaisesRegex(Exception,'report_only'):asyncio.run(c.run(p,False))
    def test_import_change_rejected(self):
        with tempfile.TemporaryDirectory(dir=c.HOME/'tmp') as t:
            p=Path(t);file=p/'parent.json';file.write_text('{}');r=p/'answer.json';r.write_text('{}')
            imp={'parent_closure':{'path':str(file),'sha256':c.sha(file)},'results':{'id':{'path':str(r),'sha256':c.sha(r)}},'auxiliary':{}}
            (p/'imports.json').write_text(json.dumps(imp));c.references(p);r.write_text('{"changed":1}')
            with self.assertRaisesRegex(Exception,'import_changed'):c.references(p)

if __name__=='__main__':unittest.main()
