import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.archive import fingerprint
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.evaluation_assistant import (
    EvaluationAssistantStore, messages, official_main_model, parse_suggestion)
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths


SUGGESTION={'left_task':'pass','right_task':'pass','left_action':'support','right_action':'ask',
            'natural':'left','reason':'左侧完整回应并先倾听，右侧提出了不必要的追问，因此左侧更自然。'}


class FakeConfig:
    def __init__(self,base='https://api.deepseek.com/v1'):
        self.value={'model':{'base_url':base,'name':'deepseek-flash','encrypted_key':'protected',
                             'timeout':60,'max_tokens':2048,'max_input_chars':24000,'hourly_limit':30}}
    def load(self):return deepcopy(self.value)


class AssistantLedger(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name).resolve())
        self.reviews=EvaluationStore(self.paths);self.assisted=EvaluationAssistantStore(self.paths)
        cases=[{'id':str(i),'scenario':'人工场景'+str(i),'author_summary':'人工作者资料'} for i in range(100)]
        self.reviews.create('review-v1',cases,{'synthetic':True},simulated=True)
        for case in cases:
            for arm in 'BC':
                self.reviews.transition('review-v1',case['id'],arm,'not_called','accepted')
                self.reviews.transition('review-v1',case['id'],arm,'accepted','running')
                self.reviews.transition('review-v1',case['id'],arm,'running','success',
                                        attempt_id=f'artificial-{case["id"]}-{arm}',output=f'匿名{arm}回复')
        self.model=official_main_model(FakeConfig())
        self.assisted.prepare('review-v1',self.model)

    def complete_first(self):
        slot,row=self.assisted.pending('review-v1')[0]
        source=fingerprint({key:row[key] for key in ('case_id','scenario','author_summary','left','right')})
        self.assisted.begin('review-v1',row['case_id'],source)
        self.assisted.finish('review-v1',row['case_id'],SUGGESTION,{'total_tokens':20},{'finish_reason':'stop'})
        return row

    def test_strict_parser_and_official_main_model(self):
        self.assertEqual(parse_suggestion(json.dumps(SUGGESTION,ensure_ascii=False)),SUGGESTION)
        duplicate='{"left_task":"pass","left_task":"pass"}'
        with self.assertRaisesRegex(RuntimeErrorBase,'duplicate_key'):parse_suggestion(duplicate)
        with self.assertRaisesRegex(RuntimeErrorBase,'requires_official_deepseek'):
            official_main_model(FakeConfig('https://proxy.example/v1'))

    def test_suggestion_is_separate_until_explicit_approval(self):
        row=self.complete_first()
        self.assertEqual(self.reviews.status('review-v1')['reviewed'],0)
        ai=self.assisted.load('review-v1',1)['cases'][0]
        result=self.assisted.approve('review-v1',row['case_id'],SUGGESTION,0,'approve-op',ai['suggestion_hash'])
        self.assertEqual(result['revision'],1)
        saved=self.reviews.load('review-v1',1)['cases'][0]['review']
        self.assertEqual(saved,SUGGESTION)
        with self.reviews.connect('review-v1') as db:
            audit=db.execute('SELECT kind,body FROM audit ORDER BY seq DESC LIMIT 1').fetchone()
        self.assertEqual(audit['kind'],'review_ai_approved')
        self.assertEqual(json.loads(audit['body'])['suggestion_hash'],ai['suggestion_hash'])

    def test_modified_review_is_audited_and_approval_cannot_change_suggestion(self):
        row=self.complete_first();ai=self.assisted.load('review-v1',1)['cases'][0]
        changed=SUGGESTION|{'natural':'tie','reason':'人工复核后认为两侧自然度相当。'}
        with self.assertRaisesRegex(RuntimeErrorBase,'approval_changed'):
            self.assisted.approve('review-v1',row['case_id'],changed,0,'bad-op',ai['suggestion_hash'])
        self.assisted.save_modified('review-v1',row['case_id'],changed,0,'modify-op',ai['suggestion_hash'])
        with self.reviews.connect('review-v1') as db:
            self.assertEqual(db.execute('SELECT kind FROM audit ORDER BY seq DESC LIMIT 1').fetchone()[0],
                             'review_ai_modified')

    def test_running_slot_becomes_fee_unknown_and_is_not_pending(self):
        slot,row=self.assisted.pending('review-v1')[0]
        source=fingerprint({key:row[key] for key in ('case_id','scenario','author_summary','left','right')})
        self.assisted.begin('review-v1',row['case_id'],source)
        self.assertEqual(self.assisted.recover('review-v1'),1)
        self.assertEqual(self.assisted.load('review-v1',1)['cases'][0]['state'],'fee_unknown')
        self.assertNotIn(row['case_id'],[item[1]['case_id'] for item in self.assisted.pending('review-v1')])

    def test_prompt_is_blind_and_contains_no_arm_mapping(self):
        row=self.reviews.load('review-v1',1)['cases'][0]
        prompt=messages(row)
        self.assertNotIn('left_arm',json.dumps(prompt,ensure_ascii=False))
        self.assertNotIn('B臂',json.dumps(prompt,ensure_ascii=False))
        self.assertNotIn('C臂',json.dumps(prompt,ensure_ascii=False))


if __name__=='__main__':unittest.main()
