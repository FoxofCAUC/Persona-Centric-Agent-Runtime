"""Preparation gate only: does not claim a production adapter or independent quality."""
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.persona_evaluation.contracts import ranked_situations,readiness,metric_gate,wilson_lower
from experiments.persona_evaluation.source_probe import inspect_adapter
from persona_runtime.disposition_core.contracts import Context,Member
from persona_runtime.disposition_core.engine import load_policy,interpret
from scripts.check_m763_readiness import evaluate

ROOT=Path(__file__).resolve().parents[1]


class Preparation(unittest.TestCase):
    def test_24_frozen_development_contracts_are_separate_from_readiness(self):
        result=evaluate()
        self.assertEqual(len(result['contract_results']),24)
        self.assertTrue(result['development_passed'])
        self.assertFalse(result['paid_generation_allowed'])
        self.assertEqual(result['independent_cases_consumed'],0)

    def test_actual_adapter_cannot_claim_fixture_context_as_production(self):
        result=inspect_adapter(ROOT/'persona_runtime/disposition_shadow.py')
        self.assertFalse(result['ready'])
        self.assertEqual(result['diagnostic'],'dynamic_adapter_needs_integration_evidence')

    def test_dynamic_adapter_requires_integration_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'adapter.py'
            p.write_text("class ShadowService:\n def _compute(self):\n  raw={'context': arbitrary_function()}\n",encoding='utf-8')
            result=inspect_adapter(p)
        self.assertFalse(result['allow_paid_generation'])
        self.assertEqual(result['diagnostic'],'dynamic_adapter_needs_integration_evidence')

    def test_ranking_does_not_mutate_frame_or_claim_confidence(self):
        policy=load_policy(ROOT/'persona_runtime/disposition_core/rules.json')
        members=(Member('a','收到新图纸'),Member('b','请解释这个连接为什么会松动'))
        frame=interpret(members,Context('neutral','',False,False,False,''),policy)
        before=asdict(frame)
        result=ranked_situations(frame,members,policy)
        self.assertEqual(result['candidates'][0]['category'],'information_request')
        self.assertEqual(result['candidates'][1]['category'],'acknowledgement')
        self.assertIsNone(result['confidence']);self.assertEqual(asdict(frame),before)
        self.assertNotIn('图纸',json.dumps(result,ensure_ascii=False))

    def test_metric_fixed_denominators_and_threshold_edges(self):
        self.assertFalse(metric_gate('natural_wins',59));self.assertTrue(metric_gate('natural_wins',60))
        self.assertLess(wilson_lower(59),.5);self.assertGreater(wilson_lower(60),.5)
        for n in (0,59,99,101):
            with self.assertRaises(ValueError):wilson_lower(59,n)
        for name,bad in (('natural_wins',101),('unnecessary_cases',-1),('situation_top1',True)):
            with self.assertRaises(ValueError):metric_gate(name,bad)

    def test_other_frozen_quality_edges(self):
        for name,fail,success in (('reasonable_pairs',15,16),('unnecessary_cases',3,2),('action_implementation',94,95)):
            self.assertFalse(metric_gate(name,fail));self.assertTrue(metric_gate(name,success))

    def test_missing_domain_cannot_be_replaced_with_unrelated_capability(self):
        self.assertFalse(readiness({'relationship':['neutral'],'information_gap':[False],
                                   'resource_unavailable':[True]})['ready'])
        self.assertFalse(readiness({'relationship':['neutral','close'],'information_gap':['True',False]})['ready'])

    def test_imports_do_not_open_database_network_or_model(self):
        import experiments.persona_evaluation.contracts as contracts
        import experiments.persona_evaluation.source_probe as probe
        with patch('sqlite3.connect',side_effect=AssertionError('database')),patch('socket.socket',side_effect=AssertionError('network')),patch('subprocess.Popen',side_effect=AssertionError('process')):
            importlib.reload(contracts);importlib.reload(probe)
