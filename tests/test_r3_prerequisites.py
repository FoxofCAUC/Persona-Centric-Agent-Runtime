"""Artificial v4 contracts, independent annotation validation, offline tool boundaries."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
import socket
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from tests.r3_prerequisite_fixture import fixture
from tests.r3_prerequisite_annotations import labels
from tests.r3_prerequisite_responses import response_plan
from scripts.r3_prerequisite_analysis import validate_labels,material_row,statistics
from scripts.r3_selector_prerequisites import offline,StopRun,main,new_policy
from persona_runtime.character_selector_protocol import prompt_for,messages,parse_output,PROMPT_VERSION,PREREQUISITE_RULE
from persona_runtime.prompt_assembly import AUXILIARY_ROLES,material_selection
from persona_runtime.models import RuntimeErrorBase,identifier
from persona_runtime.chat import ChatCompletionsClient
from tests import test_m754r2 as prior


def label(case,role,item):
    return next(r for r in labels()['rows'] if (r['case_id'],r['role_id'],r['material_id'])==(case,role,item))


class Materials(unittest.TestCase):
    def test_complete_individual_body_evidence(self):
        self.assertEqual(validate_labels(fixture(),labels()),{'cases':12,'roles':2,'labels':144,'body_keyed':True})
        self.assertEqual(len({r['reason'] for r in labels()['rows']}),144)
        self.assertFalse(labels()['old_labels_revised'])

    def test_roles_differ_only_in_example_bodies(self):
        a,b=fixture()['characters'].values()
        self.assertEqual({k:v for k,v in a.items() if k!='examples'},{k:v for k,v in b.items() if k!='examples'})
        for x,y in zip(a['examples'],b['examples']):
            self.assertEqual({k:v for k,v in x.items() if k!='text'},{k:v for k,v in y.items() if k!='text'})
            self.assertNotEqual(x['text'],y['text'])

    def test_process_migration_and_title_only_variant(self):
        data=fixture();data['characters']['detail']['examples'][0]['scenario']='任务还在进行中'
        self.assertEqual(validate_labels(data,labels())['labels'],144)
        self.assertTrue(label('P01','detail','e1')['allowed'])
        self.assertTrue(label('P03','detail','e1')['allowed'])
        self.assertTrue(label('P04','detail','e1')['allowed'])
        self.assertFalse(label('P04','stance','e1')['allowed'])

    def test_same_id_different_premise_and_local_completion(self):
        self.assertTrue(label('P07','detail','e2')['allowed'])
        self.assertFalse(label('P07','stance','e2')['allowed'])
        self.assertTrue(label('P07','stance','e3')['allowed'])
        self.assertTrue(label('P09','stance','e3')['allowed'])
        self.assertFalse(label('P08','stance','e3')['allowed'])
        self.assertNotEqual(label('P07','detail','e2')['body_hash'],label('P07','stance','e2')['body_hash'])

    def test_no_completion_or_celebration_by_assumption(self):
        for role in ('detail','stance'):
            self.assertFalse(label('P06',role,'e4')['allowed'])
            self.assertTrue(label('P05',role,'e4')['allowed'])
        self.assertFalse(label('P01','detail','e3')['allowed'])
        self.assertTrue(label('P02','detail','e3')['allowed'])

    def test_latest_state_and_wish_override_old_member_or_reference(self):
        self.assertFalse(label('P09','stance','e2')['allowed'])
        self.assertFalse(label('P10','stance','e6')['allowed'])
        self.assertTrue(label('P10','detail','e6')['allowed'])
        self.assertTrue(label('P11','detail','e5')['allowed'])
        self.assertFalse(label('P12','detail','e5')['allowed'])
        self.assertTrue(label('P12','stance','e6')['allowed'])

    def test_body_change_invalidates_existing_labels(self):
        data=fixture();data['characters']['detail']['examples'][0]['text']+='新增事实'
        with self.assertRaisesRegex(ValueError,'label_body_changed'):validate_labels(data,labels())

    def test_missing_duplicate_conflicting_or_unsupported_evidence_rejected(self):
        mutations=[(lambda a:a['rows'].pop(),'label_missing'),
          (lambda a:a['rows'].append(a['rows'][0]|{'allowed':False}),'label_key_invalid'),
          (lambda a:a['rows'][0].update(allowed=1),'label_type'),
          (lambda a:a['rows'][0].update(sample_quote='正文未出现的句子'),'sample_quote_invalid'),
          (lambda a:a['rows'][0].update(input_quotes=['错误证据']),'input_quote_invalid'),
          (lambda a:a['rows'][0].update(forbidden_facts=[]),'label_evidence_missing')]
        for mutate,error in mutations:
            a=labels();mutate(a)
            with self.subTest(error=error),self.assertRaisesRegex(ValueError,error):validate_labels(fixture(),a)

    def test_evaluation_fields_never_in_fixture_or_selector_input(self):
        data=fixture();pool={'payload':{'roots':{'example':['e1'],'lore':[]},'groups':{},
            'sources':[data['characters']['detail']['examples'][0]]}}
        payload=messages(pool,data['cases'][0]['members'],[],4,48000,prompt_version='character-selector-v4')
        for field in ('body_hash','input_support','allowed','forbidden_facts','required_premise'):
            self.assertNotIn(field,json.dumps(data));self.assertNotIn(field,payload[1]['content'])

    def test_legal_but_inapplicable_fixture_is_independent(self):
        pool={'payload':{'roots':{'example':['e3'],'lore':[]}}}
        with patch('tests.r3_prerequisite_annotations.labels',side_effect=AssertionError('annotation leak')):
            answer,missing=response_plan('P03','experiment',pool)
        self.assertEqual(answer['example_ids'],['e3']);self.assertFalse(missing)
        self.assertEqual(parse_output(json.dumps(answer),pool,4),answer)
        self.assertFalse(label('P03','detail','e3')['allowed'])

    def test_missing_candidate_does_not_get_injected(self):
        answer,missing=response_plan('P03','experiment',{'payload':{'roots':{'example':['e1']}}})
        self.assertEqual(answer['example_ids'],[]);self.assertEqual(missing,['e3'])


class Contracts(unittest.TestCase):
    def test_old_prompt_hashes_and_default_are_immutable(self):
        old={'v1':'d63dbfe7709ba88d8c005d4526b9203f1ac5358e4326ec2119ce3abe63247acf',
             'v2':'77cf21cc58a48dcca8b6e704ba7898a1bbe01628b9cbae05dbf0c229c06b02b5',
             'v3':'26c1ea79d5987ad3ad15f3e14fa371f705bca5ed0a6aa32798998a6f32c0ba68'}
        for version,digest in old.items():self.assertEqual(hashlib.sha256(prompt_for('character-selector-'+version).encode()).hexdigest(),digest)
        self.assertEqual(PROMPT_VERSION,'character-selector-v2')

    def test_only_expression_prerequisite_paragraph_added(self):
        v4=prompt_for('character-selector-v4')
        self.assertEqual(v4.count(PREREQUISITE_RULE),1)
        self.assertEqual(v4.replace(PREREQUISITE_RULE+'\n','',1),prompt_for('character-selector-v3'))
        with self.assertRaises(RuntimeErrorBase):prompt_for('character-selector-v999')

    def test_inventory_does_not_prove_effective_version(self):
        self.assertIn('character-selector-v4',AUXILIARY_ROLES[0]['versions'])
        policy={'character_retrieval':{'selector_contract':{'prompt':'character-selector-v4'}}}
        detail={'character':{'example_ids':[],'retrieval':{'effective_mode':'lexical','degrade_reason':'changed'}}}
        row=material_selection(policy,detail,{'status':'completed','prompt_version':'character-selector-v4','selected':{'example_ids':['e1']}})
        self.assertIsNone(row['applied_prompt']);self.assertEqual(row['returned_example_ids'],['e1'])
        self.assertEqual(row['included_example_ids'],[])

    def observed(self,case,role,roots,returned,included,valid=True):
        pool={'payload':{'roots':{'example':roots},'sources':[e|{'kind':'example'} for e in fixture()['characters'][role]['examples'] if e['id'] in roots]}}
        observation={'requested_prompt':'character-selector-v4','applied_prompt':'character-selector-v4' if valid else None,
          'effective_mode':'hybrid_llm' if valid else 'lexical','returned_example_ids':returned,
          'included_example_ids':included,'lore_ids':[],'fallback_reason':None if valid else 'changed'}
        return material_row({'key':case+'-'+role,'case':case,'role':role,'arm':'experiment'},pool,observation,labels())

    def test_empty_missing_dropped_and_fallback_not_mixed(self):
        rows=[self.observed('P01','detail',['e1'],[],[]),self.observed('P01','detail',['e4'],[],[]),
          self.observed('P01','detail',['e1'],['e1'],[]),self.observed('P01','detail',['e1'],None,['e1'],False)]
        s=statistics(rows)['experiment']
        self.assertEqual((s['requests'],s['valid_selector_requests'],s['eligible_requests']),(4,3,2))
        self.assertEqual((s['selected_requests'],s['included_requests'],s['dropped_requests']),(1,0,1))
        self.assertEqual((s['no_applicable_candidate'],s['fallback_requests']),(1,1))

    def test_role_specific_negative_is_reported_as_negative(self):
        row=self.observed('P07','stance',['e2'],['e2'],['e2'])
        self.assertEqual(row['inapplicable_included'],['e2'])
        self.assertEqual(self.observed('P07','detail',['e2'],['e2'],['e2'])['applicable_included'],['e2'])

    def test_changed_candidate_cannot_use_old_label(self):
        e=fixture()['characters']['detail']['examples'][0]|{'kind':'example','text':'变更'}
        with self.assertRaisesRegex(ValueError,'candidate_label_body_changed'):
            material_row({'key':'x','case':'P01','role':'detail','arm':'experiment'},
                {'payload':{'roots':{'example':['e1']},'sources':[e]}},{},labels())


class Policy(prior.SelectorTests):
    async def test_v4_policy_admission_is_frozen_and_mismatch_fails_before_call(self):
        self.enable()
        with patch.object(self.engine,'_schedule'):
            new_policy(self.engine,self.pid,'character-selector-v4')
            receipt=self.ingress.accept({'text':'书店','request_id':identifier()})
            b=self.ingress.get(receipt['batch_id']);tid=self.ingress.action(b['id'],'seal',b['revision'],identifier(),self.pid)['turn_id']
            before=self.raw(tid);new_policy(self.engine,self.pid,'character-selector-v3')
        self.assertEqual(before,self.raw(tid))
        self.assertIn('character-selector-v4',before['context_policy_snapshot'])
        pool=self.pool(self.version,{},['书店']);model=self.config.load()['character_selector']
        payload=messages(pool,['书店'],'',4,48000,prompt_version='character-selector-v3')
        with patch.object(self.model,'complete') as call:
            with self.assertRaisesRegex(ValueError,'selector_prompt_snapshot_mismatch'):
                await self.engine.selector._call(self.pid,before,model,payload,pool,4,prompt_version='character-selector-v3')
        call.assert_not_called();self.assertFalse(self.engine.selector.list(self.pid))

    async def test_unknown_policy_fails_before_admission(self):
        self.enable()
        with patch.object(self.engine,'_schedule'),patch.object(self.model,'complete') as call:
            with self.assertRaises(RuntimeErrorBase):new_policy(self.engine,self.pid,'character-selector-v999')
        call.assert_not_called()


class Network(unittest.TestCase):
    def test_http_dns_socket_datagram_and_real_client_are_blocked(self):
        import httpx
        with offline() as count:
            with self.assertRaises(StopRun):asyncio.run(ChatCompletionsClient().complete({},[]))
            with httpx.Client() as client:
                with self.assertRaises(StopRun):client.send(httpx.Request('GET','https://example.invalid'))
            with self.assertRaises(StopRun):socket.getaddrinfo('example.invalid',443)
            with socket.socket() as sock:
                with self.assertRaises(StopRun):sock.connect(('192.0.2.1',443))
            with socket.socket(type=socket.SOCK_DGRAM) as sock:
                with self.assertRaises(StopRun):sock.sendto(b'test',('192.0.2.1',443))
        self.assertEqual(count,{'external_connection_attempts':3,'http_attempts':1,'real_client_attempts':1,'real_calls':0})

    def test_no_real_execute_action(self):
        for action in ('execute','run','real'):
            with patch('sys.argv',['r3_selector_prerequisites.py',action]),redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:main()
                self.assertEqual(caught.exception.code,2)


def load_tests(loader,standard_tests,pattern):
    # The old helper base contains historical tests unrelated to this stage.
    suite=unittest.TestSuite(loader.loadTestsFromTestCase(c) for c in (Materials,Contracts,Network))
    suite.addTests(Policy(n) for n in Policy.__dict__ if n.startswith('test_'))
    return suite


if __name__=='__main__':unittest.main()
