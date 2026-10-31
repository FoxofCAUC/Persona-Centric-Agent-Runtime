"""Isolated prototype contracts. Never initialize the PCAR runtime."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
import builtins
import contextlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments.persona_decision.contracts import (
    ContractError, VERSION, canonical, validate_input)
from experiments.persona_decision.engine import (
    decide, interpret, load_policy, parse_policy, read_json, run_case, select_dispositions)
from experiments.persona_decision.cli import evaluate, main, validate_corpus

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'experiments/persona_decision'


class PrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus=read_json(DATA/'fixtures.json');cls.policy=load_policy(DATA/'rules.json')

    def case(self,n=0):
        return deepcopy(self.corpus['development'][n])

    def test_all_frozen_development_and_contract_cases(self):
        result=evaluate(self.corpus,self.policy)
        self.assertTrue(result['passed'])
        self.assertEqual(result['summary'],{'development_passed':32,'development_count':32,
            'contracts_passed':24,'contract_count':24,'text_categories_passed':32,'fixture_action_passed':32})
        self.assertNotIn('CANARY_BODY_760',canonical(result))

    def test_counterfactual_pairs_and_invariant_controls(self):
        for group,different in [('persona_counterfactual',True),('situation_counterfactual',True),('invariant',False)]:
            pairs={}
            for c in self.corpus['development']:
                if c['group']==group:pairs.setdefault(c['pair_id'],[]).append(c)
            self.assertEqual(len(pairs),4)
            for pair in pairs.values():
                a,b=(run_case(c,'text',self.policy).chosen_action for c in pair)
                self.assertEqual(a!=b,different)

    def test_text_neutral_and_fixture_are_separate(self):
        c=self.case();text_result=run_case(c,'text',self.policy)
        self.assertEqual(text_result.frame_origin,'rules')
        self.assertEqual(run_case(c,'fixture',self.policy).frame_origin,'fixture')
        self.assertEqual(run_case(c,'neutral',self.policy).chosen_action,'respond')
        self.assertNotEqual(text_result.trace_hash,run_case(c,'neutral',self.policy).trace_hash)
        c['fixture_frame']['categories']=['unknown']
        self.assertEqual(run_case(c,'fixture',self.policy).chosen_action,'respond')
        self.assertEqual(run_case(c,'text',self.policy),text_result)

    def test_sources_and_dtos_are_immutable(self):
        s=validate_input(self.case()['input'])
        with self.assertRaises(FrozenInstanceError):s.context.information_gap=True
        with self.assertRaises(FrozenInstanceError):s.definitions[0].priority=100
        self.assertIsInstance(s.definitions[0].preferences,tuple)
        original=self.case();snapshot=validate_input(original['input'])
        original['input']['members'][0]['text']='changed'
        self.assertNotEqual(snapshot.members[0].text,'changed')

    def test_selection_priority_cap_clamp_and_order(self):
        c=self.case();base=c['input']['definitions'][0]
        c['input']['definitions']=[dict(deepcopy(base),id=f'd{i}',priority=i) for i in range(6)]
        a=run_case(c,'text',self.policy)
        self.assertEqual(a.activated,('d5','d4','d3'))
        self.assertEqual(a.candidates[0][2],3)
        c['input']['definitions'].reverse()
        self.assertEqual(a,run_case(c,'text',self.policy))
        self.assertEqual(len(a.excluded),3)

    def test_predicates_are_data_only_and_authority_is_checked(self):
        c=self.case();d=c['input']['definitions'][0]
        d['triggers']=[{'field':'category','op':'eq','value':'celebration'}]
        self.assertIn(('inclination','trigger_not_matched'),run_case(c,'text',self.policy).excluded)
        d['triggers'][0]['op']='ne'
        self.assertEqual(run_case(c,'text',self.policy).chosen_action,'explain')
        d['triggers'][0]['value']='__import__("os")'
        with self.assertRaisesRegex(ContractError,'invalid_predicate'):validate_input(c['input'])
        for kind in ('memory','summary','model','world_knowledge'):
            c=self.case();c['input']['sources'][0]['kind']=kind
            with self.assertRaisesRegex(ContractError,'unsupported_source_kind'):validate_input(c['input'])

    def test_hard_eligibility_beats_arbitrary_preferences(self):
        c=self.case();c['input']['definitions'][0]['preferences']={a:2 for a in ('ask','clarify','challenge','defer','set_boundary')}
        self.assertEqual(run_case(c,'text',self.policy).chosen_action,'respond')
        c['input']['members'][0]['text']='请批评这个想法'
        self.assertEqual(run_case(c,'text',self.policy).chosen_action,'challenge')
        c['input']['context']['boundary_source_id']='author-source'
        self.assertEqual(run_case(c,'text',self.policy).chosen_action,'set_boundary')
        c['input']['sources'][0]['withdrawn']=True
        with self.assertRaisesRegex(ContractError,'unavailable_context_source'):run_case(c,'text',self.policy)

    def test_trusted_relationship_requires_relation_source(self):
        c=self.case();c['input']['context'].update(relationship='close',relationship_source_id='author-source')
        with self.assertRaisesRegex(ContractError,'unavailable_context_source'):run_case(c,'text',self.policy)
        c['input']['sources'][0]['kind']='relationship'
        c['input']['definitions'][0]['triggers']=[{'field':'relationship','op':'eq','value':'close'}]
        self.assertEqual(run_case(c,'text',self.policy).chosen_action,'explain')

    def test_literal_boundaries_quotes_negation_and_offsets(self):
        for message in ['他说“只解释原因”','他说「请安慰我」',"他说'只解释原因'",'他说“「只解释原因」”']:
            c=self.case();c['input']['members'][0]['text']=message
            self.assertEqual(run_case(c,'text',self.policy).chosen_action,'respond')
        c=self.case();c['input']['members']=[{'id':'one','text':'只解'},{'id':'two','text':'释'}]
        self.assertEqual(run_case(c,'text',self.policy).categories,('unknown',))
        c['input']['members'][1]['text']='İ Python代码'
        r=run_case(c,'text',self.policy)
        ev=next(e for e in r.evidence if e.rule_id=='topic_technical')
        self.assertEqual(c['input']['members'][1]['text'][ev.start:ev.end],'代码')
        c['input']['members']=[{'id':'one','text':'不要安慰，请解释原因'}]
        self.assertEqual(run_case(c,'text',self.policy).chosen_action,'explain')

    def test_repetition_has_one_evidence_per_rule(self):
        c=self.case();c['input']['members']=[{'id':str(i),'text':'谢谢'*10} for i in range(20)]
        r=run_case(c,'text',self.policy)
        self.assertEqual(len({e.rule_id for e in r.evidence}),len(r.evidence))
        self.assertEqual(len(r.evidence),1)

    def test_hash_covers_scope_members_and_versions(self):
        c=self.case();a=run_case(c,'text',self.policy)
        self.assertEqual(a,run_case(deepcopy(c),'text',self.policy))
        c['input']['members'][0]['text']+=' '
        self.assertNotEqual(a.input_hash,run_case(c,'text',self.policy).input_hash)
        self.assertEqual(a.policy_hash,self.policy.config_hash)
        self.assertNotIn('给些建议',canonical(a.to_dict()))

    def test_policy_change_without_version_identity_is_rejected(self):
        raw=read_json(DATA/'rules.json');raw['explicit'][0][2].append('another rule')
        with self.assertRaisesRegex(ContractError,'policy_fingerprint_mismatch'):parse_policy(raw)

    def test_duplicate_json_nan_and_bad_frame_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'test.json'
            for body,code in [('{"x":1,"x":2}','duplicate_json_key'),('{"x":NaN}','invalid_contract'),('{','invalid_json')]:
                p.write_text(body,encoding='utf-8')
                with self.assertRaisesRegex(ContractError,code):read_json(p)
        c=self.case();c['fixture_frame']['categories']=['diagnosed_depression']
        with self.assertRaises(ContractError):run_case(c,'fixture',self.policy)

    def test_runtime_import_and_execution_has_no_external_side_effect(self):
        code='''import builtins, io, socket, sqlite3, sys
def blocked(*a, **k): raise AssertionError("side effect")
sqlite3.connect=blocked; socket.socket=blocked
builtins.open=blocked; io.open=blocked
import experiments.persona_decision.contracts
import experiments.persona_decision.engine
import experiments.persona_decision.cli
assert not any(n.startswith("persona_runtime") for n in sys.modules)
'''
        subprocess.run([sys.executable,'-B','-c',code],cwd=ROOT,check=True,capture_output=True)
        with patch('sqlite3.connect',side_effect=AssertionError('database')),patch('socket.socket',side_effect=AssertionError('network')),patch('builtins.open',side_effect=AssertionError('file')),patch('io.open',side_effect=AssertionError('file')),patch('os.getenv',side_effect=AssertionError('environment')):
            self.assertTrue(evaluate(self.corpus,self.policy)['passed'])

    def test_cli_explicit_paths_no_overwrite_and_sanitized_error(self):
        args=['evaluate','--fixture',str(DATA/'fixtures.json'),'--rules',str(DATA/'rules.json')]
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'result.json'
            self.assertEqual(main(args+['--output',str(out)]),0)
            before=out.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(args+['--output',str(out)]),2)
            self.assertIn('output_exists',err.getvalue());self.assertEqual(out.read_bytes(),before)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(['validate','--fixture',str(Path(tmp)/'SECRET_BODY'),'--rules',str(DATA/'rules.json')]),2)
            self.assertNotIn('SECRET_BODY',err.getvalue())

    def test_cross_process_reproducibility(self):
        args=[sys.executable,'-B','-m','experiments.persona_decision.cli','evaluate','--fixture',str(DATA/'fixtures.json'),'--rules',str(DATA/'rules.json')]
        outputs=[]
        for seed in ('1','12345'):
            env=os.environ|{'PYTHONHASHSEED':seed,'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8'}
            outputs.append(subprocess.check_output(args,cwd=ROOT,env=env))
        self.assertEqual(*outputs)


if __name__=='__main__':unittest.main()
