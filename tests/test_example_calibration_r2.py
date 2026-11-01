import asyncio
from copy import deepcopy
import json
import socket
import unittest
from unittest.mock import patch
from tests.example_calibration_fixture import fixture,labels,rubric,text_hash
from tests.example_calibration_responses import response_plan
from scripts.example_calibration_analysis import validate_labels,material_row,statistics,attribution
from scripts.example_calibration_r2 import offline,StopRun
from persona_runtime.chat import ChatCompletionsClient
from persona_runtime.character_selector_protocol import messages


class Materials(unittest.TestCase):
    def test_complete_body_specific_labels(self):
        result=validate_labels(fixture(),labels(),rubric())
        self.assertEqual(result,{'labels':288,'cases':24,'roles':2,'reading_cases':4})
        rows=[r for r in labels()['rows'] if r['case_id']=='C09' and r['material_id']=='aftermath']
        self.assertEqual([r['allowed'] for r in rows],[True,False]);self.assertNotEqual(rows[0]['body_hash'],rows[1]['body_hash'])
    def test_only_example_text_differs_between_people(self):
        a,b=fixture()['characters'].values()
        self.assertEqual({k:v for k,v in a.items() if k!='examples'},{k:v for k,v in b.items() if k!='examples'})
        for x,y in zip(a['examples'],b['examples']):self.assertEqual({k:v for k,v in x.items() if k!='text'},{k:v for k,v in y.items() if k!='text'})
    def test_body_edit_invalidates_label(self):
        data=fixture();data['characters']['detail']['examples'][0]['text']+='变更'
        with self.assertRaisesRegex(ValueError,'label_body_changed'):validate_labels(data,labels(),rubric())
    def test_missing_duplicate_and_inconsistent_narrow_rejected(self):
        for change,code in ((lambda x:x['rows'].pop(),'label_missing'),
                           (lambda x:x['rows'].append(x['rows'][0]),'label_key_invalid'),
                           (lambda x:x['rows'][1].update(narrow_allowed=True),'narrow_outside_primary')):
            value=labels();change(value)
            with self.assertRaisesRegex(ValueError,code):validate_labels(fixture(),value,rubric())
    def test_no_old_scenarios_and_no_evaluation_in_runtime_fixture(self):
        text=json.dumps(fixture(),ensure_ascii=False)
        for term in ('杯子','薄荷','雨杉','被子','allowed','narrow_allowed','forbidden_facts','body_hash'):self.assertNotIn(term,text)
        self.assertEqual(fixture()['future_case_ids'],['C01','C03','C05','C09','C17','C18','C21','C23'])
    def test_reading_quotes_are_required_evidence(self):
        value=rubric();value['authored_reading_cases'][0]['reply_quote']='不存在的证据'
        with self.assertRaisesRegex(ValueError,'reading_reply_quote'):validate_labels(fixture(),labels(),value)
    def test_mock_answers_do_not_consult_evaluation_labels(self):
        pool={'payload':{'roots':{'example':['progress','aftermath']}}}
        with patch('tests.example_calibration_fixture.labels',side_effect=AssertionError('label read')):
            self.assertEqual(response_plan('C09','control',pool)[0]['example_ids'],['progress'])
        self.assertFalse(next(r for r in labels()['rows'] if (r['case_id'],r['role_id'],r['material_id'])==('C09','detail','progress'))['allowed'])
    def test_missing_candidates_not_injected(self):
        value,missing=response_plan('C05','experiment',{'payload':{'roots':{'example':['play']}}})
        self.assertEqual(value['example_ids'],[]);self.assertEqual(missing,['progress'])
    def test_labels_not_in_selector_messages(self):
        data=fixture();pool={'payload':{'roots':{'example':['progress'],'lore':[]},'sources':[data['characters']['detail']['examples'][0]],'groups':{}}}
        payload=messages(pool,data['cases'][0]['members'],[],4,48000,prompt_version='character-selector-v3')
        content=json.loads(payload[1]['content']);self.assertEqual(set(content),{'members','references','candidates','example_limit'})
        for field in ('allowed','narrow_allowed','body_hash','forbidden_facts'):self.assertNotIn(field,json.dumps(content))


class ReportContracts(unittest.TestCase):
    def observed(self,case,role,roots,returned,included):
        data=fixture();pool={'payload':{'roots':{'example':roots},'sources':[e|{'kind':'example'} for e in data['characters'][role]['examples'] if e['id'] in roots]}}
        item={'key':case+'-'+role,'case':case,'role':role,'arm':'experiment'}
        observation={'returned_example_ids':returned,'included_example_ids':included,'lore_ids':[],
            'applied_prompt':'character-selector-v3','fallback_reason':None}
        return material_row(item,pool,observation,labels())
    def test_empty_missing_and_dropped_have_distinct_denominators(self):
        rows=[self.observed('C17','detail',['progress'],[],[]),self.observed('C05','detail',['play'],[],[]),
              self.observed('C01','detail',['progress'],['progress'],[])]
        s=statistics(rows)['primary']['experiment']
        self.assertEqual((s['no_applicable_authority'],s['no_applicable_candidate'],s['eligible_requests']),(1,1,1))
        self.assertEqual((s['selected_requests'],s['included_requests'],s['dropped_requests']),(1,0,1))
    def test_wrong_role_cannot_borrow_allowed_label(self):
        row=self.observed('C09','stance',['aftermath'],['aftermath'],['aftermath'])
        self.assertEqual(row['primary']['inapplicable_included'],['aftermath'])
    def test_primary_and_narrow_both_reported(self):
        s=statistics([self.observed('C05','detail',['progress'],['progress'],['progress'])])
        self.assertEqual(s['primary']['experiment']['included_requests'],1)
        self.assertEqual(s['narrow']['experiment']['inapplicable_included_requests'],1)
    def test_attribution_categories(self):
        examples=[((False,False,True,True,['a','a']),'incomplete'),
          ((True,True,True,False,['a','a']),'identical_request_output_difference_only'),
          ((True,False,False,True,['a','a']),'confounded_request'),
          ((True,False,True,True,[None,'a']),'backend_unknown'),
          ((True,False,True,True,['a','b']),'backend_changed'),
          ((True,False,True,True,['a','a']),'examples_only_directional_analysis_possible')]
        for args,expected in examples:self.assertEqual(attribution(*args),expected)


class Network(unittest.TestCase):
    def test_external_connect_blocked_before_dispatch(self):
        with offline() as count:
            with socket.socket() as sock:
                with self.assertRaises(StopRun):sock.connect(('192.0.2.1',443))
        self.assertEqual(count['external_connection_attempts'],1);self.assertEqual(count['real_calls'],0)
    def test_real_model_and_http_clients_blocked(self):
        import httpx
        with offline() as count:
            with self.assertRaises(StopRun):asyncio.run(ChatCompletionsClient().complete({},[]))
            with httpx.Client() as client:
                with self.assertRaises(StopRun):client.send(httpx.Request('GET','https://example.invalid'))
        self.assertEqual(count['http_attempts'],1);self.assertEqual(count['real_client_attempts'],1)


if __name__=='__main__':unittest.main()
