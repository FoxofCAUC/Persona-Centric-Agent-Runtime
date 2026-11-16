"""40位续批的预算、版本、审阅绑定和不重放；只用离线人工回执。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from experiments.expression_persona_draft import study,continuation
from experiments.expression_persona_draft.review import report
from persona_runtime.im_punctuation import GUIDANCE,EXPRESSION_PROFILE
from tests import test_expression_persona_draft as fixtures


class Continuation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=fixtures.Study('test_unknown_request_is_not_replayed_or_filled')
        with patch.object(study,'prepare',return_value=None):self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source,self.first=self.fixture.source,self.fixture.root
        data=study.read(self.source/'review-data.json');known={c['id']:c for c in data['cases']}
        all_cases=[];database=self.source/'instance/data/database/runtime.sqlite3'
        groups=study.read(study.REPO/'experiments/expression_e3_review/fixtures.json')['groups']
        with closing(sqlite3.connect(database)) as db,db:
            for group in groups:
                role=data['personas'][group['persona']]
                for index in range(1,4):
                    key=group['id']+'-'+str(index)
                    if key in known:case=known[key]|{'role':role}
                    else:
                        draft='本地保存主稿 '+key+'。';tid='turn-'+key
                        profile={'core':{'id':'core-'+key,'data':role},'expression':{'id':'expression-'+key,'data':{'style':role['expression_style']}}}
                        db.execute('INSERT INTO turns VALUES(?,?)',(tid,json.dumps(profile)))
                        db.execute('INSERT INTO expression_main_receipts VALUES(?,?,?,?,?,?)',('receipt-'+key,tid,1,draft,study.sha(draft.encode()),'physical-'+key))
                        case={'id':key,'persona':group['persona'],'turn_id':tid,'title':group['title'],'primary':draft,'primary_hash':study.sha(draft.encode()),'role':role}
                    all_cases.append(case)
        data['cases']=all_cases;(self.source/'review-data.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        study.prepare(self.first,self.source,trial_kind='offline_fixture')
        first_client=fixtures.Client();await study.execute(self.first,client=first_client);report(self.first)
        self.first_data=study.read(self.first/'review-data.json');key=study.read(self.first/'review-key.json')
        self.review=self.fixture.base/'review.json'
        rows=[]
        for c in self.first_data['cases']:
            left=key['left_source'][c['id']]
            rows.append({k:c[k] for k in ('id','kind','primary_hash','expression_hash')}|{'left_source':left,'choice':'left' if left=='expression' else 'right'})
        study.write_once(self.review,{'plan_sha256':key['plan_sha256'],'cases':rows})
        self.root=self.fixture.base/'continuation'

    async def test_exact_remaining_40_uses_v2_and_never_repeats_first_eight(self):
        original_hash=study.sha((self.first/'review-data.json').read_bytes())
        plan=continuation.prepare(self.root,self.first,self.review,author_continues=True,trial_kind='offline_fixture')
        self.assertEqual(len(plan['positions']),40);self.assertEqual(plan['profile'],EXPRESSION_PROFILE)
        self.assertEqual(plan['max_physical_calls'],40)
        excluded=set(plan['parent_first_batch']['excluded_source_cases'])
        selected={study.read(self.root/'inputs'/(p['id']+'.json'))['source']['old_case_id'] for p in plan['positions']}
        self.assertFalse(excluded&selected);self.assertEqual(len(excluded|selected),48)
        client=fixtures.Client();await study.execute(self.root,client=client)
        self.assertEqual(len(client.calls),40)
        for messages in client.calls:self.assertEqual(len(messages),2);self.assertEqual(messages[0]['content'].count(GUIDANCE),1)
        value=report(self.root);self.assertEqual(value['planned'],40);self.assertEqual(value['completed'],40)
        self.assertEqual(value['real_expression_calls'],0)
        await study.execute(self.root,client=client);self.assertEqual(len(client.calls),40)
        self.assertEqual(study.sha((self.first/'review-data.json').read_bytes()),original_hash)

    async def test_duplicate_continuation_plan_is_rejected(self):
        continuation.prepare(self.root,self.first,self.review,author_continues=True,trial_kind='offline_fixture')
        with self.assertRaisesRegex(ValueError,'already_claimed'):
            continuation.prepare(self.fixture.base/'another',self.first,self.review,author_continues=True,trial_kind='offline_fixture')

    async def test_review_tampering_or_missing_author_instruction_stops_before_plan(self):
        with self.assertRaisesRegex(ValueError,'author_gate_required'):
            continuation.prepare(self.root,self.first,self.review,trial_kind='offline_fixture')
        review=study.read(self.review);review['cases'][0]['expression_hash']='tampered'
        self.review.write_text(json.dumps(review),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'review_body_mismatch'):
            continuation.prepare(self.root,self.first,self.review,author_continues=True,trial_kind='offline_fixture')
        self.assertFalse(self.root.exists())

    async def test_wrong_claim_cannot_launch_already_prepared_plan(self):
        continuation.prepare(self.root,self.first,self.review,author_continues=True,trial_kind='offline_fixture')
        claim=self.first.parent/'remaining40-claim.json';data=study.read(claim);data['root']='other';claim.write_text(json.dumps(data),encoding='utf-8')
        client=fixtures.Client()
        with self.assertRaisesRegex(ValueError,'claim_mismatch'):await study.execute(self.root,client=client)
        self.assertEqual(client.calls,[])

    async def test_complete_body_recovers_locally_and_only_unattempted_positions_dispatch(self):
        plan=continuation.prepare(self.root,self.first,self.review,author_continues=True,trial_kind='offline_fixture')
        position=plan['positions'][0];folder=self.root/'positions'/position['id']
        raw={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'body':'新正文。','segments':[{'text':'新正文。'}],'no_split':False},ensure_ascii=False)}}],
             'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
        (folder/'response.raw.json').write_bytes(json.dumps(raw).encode())
        study.write_once(folder/'dispatch.json',{'id':position['id']})
        original={'id':position['id'],'state':'unusable','response_sha256':study.sha((folder/'response.raw.json').read_bytes()),'artifact':None,'usage':raw['usage'],'error_code':'persona_response_fields_invalid'}
        study.write_once(folder/'result.json',original);before=study.sha((folder/'result.json').read_bytes())
        value=study.recover_segmentation(self.root,position['id'])
        self.assertEqual(value['artifact']['body'],'新正文。');self.assertTrue(value['artifact']['plan_fallback'])
        self.assertEqual(study.sha((folder/'result.json').read_bytes()),before)
        client=fixtures.Client();await study.execute(self.root,client=client)
        self.assertEqual(len(client.calls),39);self.assertEqual(report(self.root)['completed'],40)

    async def test_retained_protected_failure_requires_explicit_continue_and_is_never_replayed(self):
        plan=continuation.prepare(self.root,self.first,self.review,author_continues=True,trial_kind='offline_fixture')
        position=plan['positions'][0];folder=self.root/'positions'/position['id']
        raw={'choices':[{'finish_reason':'stop','message':{'content':'missing code'}}]}
        (folder/'response.raw.json').write_bytes(json.dumps(raw).encode())
        study.write_once(folder/'dispatch.json',{'id':position['id']})
        original={'id':position['id'],'state':'unusable','response_sha256':study.sha((folder/'response.raw.json').read_bytes()),
                  'artifact':None,'usage':None,'error_code':'protected_text_changed'}
        study.write_once(folder/'result.json',original);before=study.sha((folder/'result.json').read_bytes())
        parser=study.parse_received
        def parse(root,p,raw):return original if p['id']==position['id'] else parser(root,p,raw)
        with patch.object(study,'parse_received',side_effect=parse):
            self.assertEqual(study.retain_protected_failure(self.root,position['id'])['new_model_calls'],0)
            client=fixtures.Client();value=await study.execute(self.root,client=client)
            self.assertEqual(value['stopped_at'],position['id']);self.assertEqual(client.calls,[])
            await study.execute(self.root,client=client,continue_after_known_unusable=True)
        self.assertEqual(len(client.calls),39)
        self.assertEqual(study.reconcile(self.root,position)['state'],'unusable')
        self.assertEqual(report(self.root)['completed'],39)
        self.assertEqual(study.sha((folder/'result.json').read_bytes()),before)


if __name__=='__main__':unittest.main()
