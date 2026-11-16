"""完整8组离线会话验证预算、历史隔离和匿名审阅，不发送真实请求。"""
import json
from pathlib import Path
import tempfile
import unittest

from persona_runtime.chat import ModelCompletion
from persona_runtime.paths import RuntimePaths
from persona_runtime.security import protect
from experiments.expression_ab_conversation import study
from experiments.expression_ab_conversation.report import report
from experiments.expression_e3_review.common import read,write_once


class AB(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='expression-ab-',dir=RuntimePaths.from_environment().home/'tmp');self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'run';material=self.base/'material.json'
        write_once(material,{'url':'https://docs.python.org/3/library/stdtypes.html','summary':'离线人工夹具：字典保留插入顺序，整数可作为键。'})
        self.plan=study.prepare(self.root,material,trial_kind='offline_fixture')

    async def test_all_8_groups_use_independent_actual_histories_and_b_save_main(self):
        counter=0
        async def primary(model,messages,**kwargs):
            nonlocal counter
            counter+=1
            return ModelCompletion('源稿'+str(counter)+'。',{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2},{'finish_reason':'stop'})
        async def expression(model,messages,*,save_raw):
            body='角色说法：'+messages[1]['content']
            raw=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'body':body,'segments':'no_split'},ensure_ascii=False)}}],
                            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}},ensure_ascii=False).encode()
            save_raw(raw);return {'diagnostics':{}}
        async def jev(*args,**kwargs):return ModelCompletion('{"answers":{"route":{"choice":"web","confidence":0.99}}}',{'total_tokens':1},{})
        creds={'main':protect('offline-main-key-123'),'jev':protect('offline-jev-key-456'),'jev_public':{'base_url':'https://example.org','name':'fixture'},'files':{}}
        await study.execute(self.root,creds,overrides={'primary':primary,'notice':primary,'expression':expression,'expression_notice':expression,'jev':jev})
        value=report(self.root)
        self.assertEqual(value['completed_turns'],{'A':48,'B':48});self.assertEqual(value['client_requests'],0)
        self.assertLessEqual(value['offline_fixture_requests'],174);self.assertEqual(value['controlled_retrieval_positions'],4)
        self.assertEqual(value['counts'],study.LIMITS)
        data=read(self.root/'review-data.json')
        for group in data['groups']:
            a,b=group['chains']['A'],group['chains']['B']
            self.assertNotEqual(a[0]['turn_id'],b[0]['turn_id']);self.assertNotEqual(a[0]['body'],b[0]['body'])
            self.assertTrue(b[0]['body'].startswith('角色说法：'))
            self.assertFalse(b[0]['main_draft'].startswith('角色说法：'))
            aq=read(self.root/'positions'/a[1]['position']/'calls/primary.request.json')['input']
            bq=read(self.root/'positions'/b[1]['position']/'calls/primary.request.json')['input']
            self.assertIn(a[0]['body'],json.dumps(aq,ensure_ascii=False));self.assertNotIn(b[0]['body'],json.dumps(aq,ensure_ascii=False))
            self.assertIn(b[0]['body'],json.dumps(bq,ensure_ascii=False));self.assertNotIn(a[0]['body'],json.dumps(bq,ensure_ascii=False))
        before=len(list((self.root/'positions').glob('*/calls/*.dispatch.json')))
        await study.execute(self.root,creds,overrides={'primary':primary,'notice':primary,'expression':expression,'expression_notice':expression,'jev':jev})
        self.assertEqual(len(list((self.root/'positions').glob('*/calls/*.dispatch.json'))),before)
        for path in (self.root/'positions').glob('*/calls/expression*.request.json'):
            messages=read(path)['input'];self.assertEqual(len(messages),2)
            self.assertNotIn('selected_expression_ids',json.dumps(messages,ensure_ascii=False))

    async def test_duplicate_reservation_and_control_expression_stop_before_transport(self):
        slot=self.plan['positions'][0]
        study.reserve(self.root,self.plan,slot,'primary',{'messages':[]})
        with self.assertRaisesRegex(ValueError,'already_dispatched'):study.reserve(self.root,self.plan,slot,'primary',{'messages':[]})
        with self.assertRaisesRegex(ValueError,'no_expression'):study.reserve(self.root,self.plan,slot,'expression',{'messages':[]})
        with self.assertRaisesRegex(ValueError,'not_planned'):study.reserve(self.root,self.plan,slot,'notice',{'messages':[]})

    async def test_unsettled_position_stops_before_creating_instance_or_any_worker(self):
        slot=self.plan['positions'][0]
        write_once(self.root/'positions'/slot['id']/'turn.json',{'turn_id':'unknown-existing-turn'})
        with self.assertRaisesRegex(ValueError,'unsettled_position_not_replayed'):
            await study.execute(self.root,{},overrides={'primary':None})
        self.assertFalse((self.root/'instance').exists())
        self.assertFalse(list((self.root/'positions').glob('*/calls/*.dispatch.json')))

    async def test_verify_compares_credentials_with_original_start_record(self):
        write_once(self.root/'start.json',{'credential_file_hashes':{'source-file':'original-hash'}})
        with self.assertRaisesRegex(ValueError,'credentials_changed'):
            study.verify(self.root,{'files':{'source-file':'new-hash'}})


if __name__=='__main__':unittest.main()
