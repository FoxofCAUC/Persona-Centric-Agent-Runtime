"""Offline checks for a real cross-runtime acceptance accounting omission."""
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from plugins.m8d_acceptance.budget import next_window,record_dispatch,records
from persona_runtime.chat import ModelError
from persona_runtime.paths import DEFAULT_HOME


def call(epoch,org=True):return {'at':datetime.fromtimestamp(epoch,timezone.utc).isoformat(),'organization':org,'status':'unknown'}


class BudgetTests(unittest.TestCase):
    def test_formal_start_waits_for_isolated_six_calls_to_leave_window(self):
        calls=[call(100+i) for i in range(6)]
        self.assertGreater(next_window(calls,200,True,6),3705)
        self.assertEqual(next_window(calls,3706,True,6),3706)
        self.assertGreater(next_window(calls,200,True),3700)

    def test_daily_and_main_caps_include_unknown_calls_and_native_retries(self):
        self.assertGreater(next_window([call(i*100) for i in range(24)],2400,True),86400)
        self.assertIsNone(next_window([call(i,False) for i in range(8)],2400,False))

    def test_cross_root_admission_cannot_reset_or_write_a_seventh_dispatch(self):
        with tempfile.TemporaryDirectory(prefix='m8d-budget-',dir=DEFAULT_HOME/'tmp') as folder:
            root=Path(folder);prior=root/'real-1/calls';prior.mkdir(parents=True)
            for i in range(6):(prior/(str(i)+'.json')).write_text(json.dumps(call(100+i)),encoding='utf-8')
            target=root/'formal-calls/next.json'
            with patch('plugins.m8d_acceptance.budget.time.time',return_value=200),self.assertRaises(ModelError):record_dispatch(root,target,call(200))
            self.assertFalse(target.exists());self.assertEqual(len(records(root)),6)
            with patch('plugins.m8d_acceptance.budget.time.time',return_value=3706):record_dispatch(root,target,call(3706))
            self.assertEqual(len(records(root)),7)
