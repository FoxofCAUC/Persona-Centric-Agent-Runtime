"""Synthetic report fixtures only. No provider, runtime dispatch or old evidence writes."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.archive import encoded, fingerprint
from scripts import dialogue_context_real_analysis as analysis


TMP = Path('D:/Persona-Centric Agent Runtime/tmp')


def position(case='S01',role='process',arm='automatic',replicate=1):
    return {'id':f'{case}-{role}-{arm}-r{replicate}','case_id':case,'role_id':role,
            'arm':arm,'replicate':replicate}


def result(pos, text='这一下挺有意思。', *, frame=False, real=False):
    messages=[{'role':'system','content':'共同人格'+('状态资料' if frame else '')}, {'role':'user','content':'共同输入'}]
    blocks=[{'name':'persona_expression','message_index':0,'start_character':0,'characters':4,
             'content_hash':fingerprint('共同人格')},
            {'name':'current_input','message_index':1,'start_character':0,'characters':4,
             'content_hash':fingerprint('共同输入')}]
    if frame:blocks.append({'name':'dialogue_context','message_index':0,'start_character':4,'characters':4,
                           'content_hash':fingerprint('状态资料')})
    raw=encoded({'schema_version':3,'messages':[{'content':text}]})
    value={**pos,'real':real,'status':'completed','reply':raw,'messages':messages,
           'input_hash':fingerprint(messages),'elapsed_seconds':2.0,'calls':[],
           'manifest':{'prompt_assembly':{'blocks':blocks,'material_selection':{
               'fact_paths':[],'lore_ids':[],'included_example_ids':[]}}}}
    value['pipeline']={'character':{'fact_paths':[],'lore_ids':[],'example_ids':[]},
                       'routing':{'path':'joint' if frame else 'direct'}} if pos['arm']=='automatic' else None
    return value


def receipt(value,number=1,*,metadata=True,status='completed',usage=None):
    model={'name':'grok-4.7','base_url':'https://example.invalid/v1','timeout':180,'max_tokens':8192}
    return {'receipt_id':f'{number:04d}','real':value['real'],'kind':'main','slot':value['id']+'/main',
            'model':model,'messages':value['messages'],
            'transport':{'usage':usage,'diagnostics':{'response_model':'synthetic-route','system_fingerprint':'synthetic-fp'} if metadata else {}},
            'terminal':{'status':status}}


def reading(value,score=4):
    text=analysis.text_of(value)
    return {'position_id':value['id'],'output_hash':fingerprint(value['reply']),
            'dimensions':{name:{'score':score,'reason':'人工编写的单元阅读依据','excerpt':text} for name in analysis.DIMENSIONS},
            'obligations':[],'boundaries':[],'task_status':'satisfied'}


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')


class Assessments(unittest.TestCase):
    def test_no_score_is_invented_for_literal_control(self):
        value=result(position('S02'),text='432')
        self.assertEqual(analysis.literal_controls(value)['status'],'passed')
        self.assertEqual(analysis.validate_assessments([], {value['id']:value}),{})
        self.assertIsNone(analysis.assessment_template({value['id']:value})[0]['dimensions']['im_naturalness']['score'])

    def test_other_tasks_are_reading_required_not_keyword_scored(self):
        value=result(position('S14'),text='别庆祝，名单现在已经核对好了。')
        self.assertEqual(analysis.literal_controls(value)['status'],'needs_reading')
        equivalent=result(position('S11'),text='126 cm²')
        self.assertEqual(analysis.literal_controls(equivalent)['status'],'passed')

    def test_output_hash_excerpt_and_range_are_validated(self):
        value=result(position());base=reading(value)
        self.assertEqual(len(analysis.validate_assessments([base],{value['id']:value})),1)
        for change in ('hash','excerpt','range'):
            row=deepcopy(base)
            if change=='hash':row['output_hash']='changed'
            if change=='excerpt':row['dimensions']['im_naturalness']['excerpt']='从未出现的文字'
            if change=='range':row['dimensions']['im_naturalness']['score']=6
            with self.assertRaises(ValueError):analysis.validate_assessments([row],{value['id']:value})

    def test_frame_requires_actual_result_and_input_binding(self):
        value=result(position('S13'),frame=True)
        frame={'progress':[{'text':'只完成局部'}]}
        value['pipeline'].update(understanding=frame,shared={'sources':[{'text':'开头四格完成'}]})
        row={'position_id':value['id'],'result_hash':fingerprint(frame),'critical_items':[
            {'status':'correct','reason':'局部与整体区分','input_excerpt':'开头四格完成','frame_excerpt':'只完成局部'}]}
        self.assertEqual(len(analysis.validate_frames([row],{value['id']:value})),1)
        row['critical_items'][0]['input_excerpt']='不存在的输入'
        with self.assertRaisesRegex(ValueError,'input_excerpt'):analysis.validate_frames([row],{value['id']:value})


class Attribution(unittest.TestCase):
    def test_same_request_preference_is_not_component_benefit(self):
        a=result(position());b=result(position(arm='legacy'))
        rows=[receipt(a),receipt(b,2)]
        actual=analysis.attribution(a,b,rows)
        self.assertTrue(actual['same_main_request'])
        self.assertEqual(actual['category'],'same_main_request')
        self.assertFalse(actual['frame_projection_condition_subset'])

    def test_frame_only_requires_known_matching_backend_and_parameters(self):
        a=result(position(),frame=True);b=result(position(arm='legacy'))
        rows=[receipt(a),receipt(b,2)]
        actual=analysis.attribution(a,b,rows)
        self.assertEqual(actual['category'],'frame_projection_only')
        self.assertTrue(actual['frame_projection_condition_subset'])
        rows[0]['transport']['diagnostics']={}
        self.assertFalse(analysis.attribution(a,b,rows)['frame_projection_condition_subset'])

    def test_changed_backend_or_main_parameter_does_not_qualify(self):
        a=result(position(),frame=True);b=result(position(arm='legacy'));rows=[receipt(a),receipt(b,2)]
        rows[0]['transport']['diagnostics']['system_fingerprint']='different'
        self.assertFalse(analysis.attribution(a,b,rows)['frame_projection_condition_subset'])
        rows[0]['transport']['diagnostics']=rows[1]['transport']['diagnostics']
        rows[0]['model']['max_tokens']=4096
        self.assertEqual(analysis.attribution(a,b,rows)['category'],'unexplained_or_parameter_difference')

    def test_changed_persona_block_is_unexplained_and_forged_hash_rejected(self):
        a=result(position(),frame=True);b=result(position(arm='legacy'));rows=[receipt(a),receipt(b,2)]
        a['messages'][0]['content']='不同人格状态资料'
        a['manifest']['prompt_assembly']['blocks'][0]['content_hash']=fingerprint('不同人格')
        actual=analysis.attribution(a,b,rows)
        self.assertIn('persona_expression',actual['unexplained_blocks'])
        a['manifest']['prompt_assembly']['blocks'][0]['content_hash']='forged'
        with self.assertRaisesRegex(ValueError,'block_hash'):analysis.attribution(a,b,rows)

    def test_single_arm_failure_is_not_an_automatic_win(self):
        a=result(position());prepared={'positions':[position(),position(arm='legacy')]}
        pairs=analysis.compare({a['id']:a},{a['id']:reading(a)},[receipt(a)],prepared)
        self.assertEqual(pairs[0]['verdict'],'unable')
        self.assertFalse(pairs[0]['complete_pair'])


class Review(unittest.TestCase):
    def test_fixed_twelve_cover_scenes_roles_paths_and_orientations(self):
        plan=analysis.review_plan()
        self.assertEqual(len({r['case_id'] for r in plan}),12)
        self.assertEqual(Counter(r['role_id'] for r in plan),{'process':6,'candid':6})
        self.assertEqual(Counter(r['route'] for r in plan),{'direct':6,'joint':6})
        self.assertEqual(sum(r['A']==r['candidate'] for r in plan),6)
        self.assertEqual(plan,analysis.review_plan())

    def test_missing_case_stays_missing_and_original_bubbles_are_preserved(self):
        pair=analysis.review_plan()[0];a=result(position(pair['case_id'],pair['role_id'],'automatic',pair['replicate']),text='原样```内容')
        b=result(position(pair['case_id'],pair['role_id'],'legacy',pair['replicate']),text='另一条')
        text,available=analysis.anonymous_markdown({a['id']:a,b['id']:b},{},real=True)
        self.assertEqual(available,1);self.assertEqual(text.count('缺少完整配对'),11)
        self.assertIn('原样```内容',text)
        self.assertNotIn(pair['candidate'],text)
        self.assertNotIn(pair['control'],text)

    def test_same_letter_can_choose_either_condition_and_reasons_are_not_invented(self):
        mapping=analysis.review_plan();results={}
        for pair in mapping:
            for arm in ('automatic','legacy'):
                p=position(pair['case_id'],pair['role_id'],arm,pair['replicate']);results[p['id']]=result(p)
        resolved=analysis.resolve_user_review([{'number':i,'choice':'B'} for i in range(1,13)],mapping,results)
        self.assertEqual(Counter(r['user_candidate_verdict'] for r in resolved),{'win':6,'loss':6})
        self.assertTrue(all(r['user_reason'] is None for r in resolved))

    def test_missing_pair_or_duplicate_vote_rejected(self):
        plan=analysis.review_plan()
        with self.assertRaisesRegex(ValueError,'missing_result'):analysis.resolve_user_review([{'number':1,'choice':'A'}],plan,{})
        with self.assertRaisesRegex(ValueError,'review_number'):analysis.resolve_user_review([{'number':1,'choice':'无法判断'}]*2,plan,{})


class Reporting(unittest.TestCase):
    def test_usage_does_not_double_add_reasoning_or_fill_unknown(self):
        value=result(position());one=receipt(value,usage={'total_tokens':10,'reasoning_tokens':5})
        two=receipt(value,2,status='failed',usage=None);two['slot']='other/main'
        out=analysis.aggregate_receipts([one,two])
        self.assertEqual(out['known_total_tokens'],10)
        self.assertEqual(out['unknown_usage_receipts'],['0002'])
        self.assertIsNone(out['amount'])

    def test_report_does_not_turn_simulation_into_real_or_missing_scores_into_zero(self):
        with tempfile.TemporaryDirectory(dir=TMP,prefix='cr21-analysis-') as folder:
            root=Path(folder);p=position('S02');value=result(p,text='432')
            write(root/'prepared.json',{'real':False,'positions':[p,position('S02',arm='legacy')],'items':{}})
            write(root/'results'/(p['id']+'.json'),value)
            out=analysis.report(root,write=False)
            self.assertEqual(out['scope'],'simulation')
            self.assertEqual(out['arms']['automatic']['dimension_means']['im_naturalness'],None)
            self.assertEqual(out['machine_pair_verdicts'],{'unable':1})
            self.assertFalse((root/'analysis').exists())

    def test_real_simulation_mixture_and_receipt_mismatch_rejected(self):
        with tempfile.TemporaryDirectory(dir=TMP,prefix='cr21-analysis-') as folder:
            root=Path(folder);p=position();value=result(p,real=True)
            write(root/'prepared.json',{'real':False,'positions':[p]})
            write(root/'results'/(p['id']+'.json'),value)
            with self.assertRaisesRegex(ValueError,'scope_mismatch'):analysis.report(root,write=False)
            write(root/'prepared.json',{'real':True,'positions':[p]})
            write(root/'receipts/0001.reserve.json',{'real':False,'kind':'main','slot':p['id']+'/main'})
            with self.assertRaisesRegex(ValueError,'receipt_scope'):analysis.report(root,write=False)
            write(root/'receipts/0001.reserve.json',{'real':True,'kind':'main','slot':p['id']+'/main'})
            write(root/'receipts/0001.transport.json',{'real':False,'status':'returned'})
            with self.assertRaisesRegex(ValueError,'receipt_scope'):analysis.report(root,write=False)

    def test_sealed_stage_writer_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TMP,prefix='cr21-analysis-') as folder:
            root=Path(folder);write(root/'closure.json',{})
            with self.assertRaisesRegex(ValueError,'stage_sealed'):analysis.report(root)


if __name__=='__main__':unittest.main()
