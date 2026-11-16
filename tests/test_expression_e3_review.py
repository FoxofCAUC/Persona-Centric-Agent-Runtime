"""扩大审阅的调用边界、联网接线及回执报告；全部为离线客户端。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from experiments.expression_e3_review import common,runner
from experiments.expression_e3_review.report import report,billing
from persona_runtime.chat import ModelCompletion
from persona_runtime.deepseek_native_response import NativeResponse
from persona_runtime.paths import RuntimePaths
from persona_runtime.instance import InstanceLock
from persona_runtime.storage import Store
from persona_runtime.security import protect
from tests.test_m9r0_native_contract import response


class Budget(unittest.TestCase):
    def setUp(self):
        parent=RuntimePaths.from_environment().home/'tmp';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='expanded-review-',dir=parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'run'

    def test_public_plan_covers_48_turns_and_132_reserved_calls(self):
        plan=common.prepare(self.root)
        self.assertEqual(len(plan['positions']),48);self.assertEqual(len(plan['fixtures']['groups']),16)
        self.assertEqual(plan['physical_calls_max'],132);self.assertEqual(plan['limits']['native'],8)
        self.assertTrue(plan['author_review_required_before_E4'])
        key=common.read(self.root/'review-key.json')['left_source']
        self.assertEqual(list(key.values()).count('primary'),24)
        self.assertEqual(list(key.values()).count('expression'),24)

    def test_completed_or_unknown_reservation_cannot_dispatch_again(self):
        plan=common.prepare(self.root);common.reserve(self.root,plan,'G01-1','primary',{'request':'first'})
        with self.assertRaisesRegex(ValueError,'already_reserved'):common.reserve(self.root,plan,'G01-1','primary',{'request':'changed'})
        self.assertEqual(len(list((self.root/'positions').glob('*/calls/*.dispatch.json'))),1)

    def test_report_can_load_frozen_plan_but_run_stops_when_source_binding_changes(self):
        plan=common.prepare(self.root);plan['source_hashes']['persona_runtime/chat.py']='changed'
        path=self.root/'plan.json';path.write_text(json.dumps(plan),encoding='utf-8')
        (self.root/'plan-hash.json').write_text(json.dumps({'sha256':common.sha(path.read_bytes())}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'frozen_source_changed'):common.load(self.root)
        self.assertEqual(common.load(self.root,check_source=False)['version'],common.VERSION)

    def test_terminal_native_failure_keeps_known_usage_and_tool_cost(self):
        plan=common.prepare(self.root)
        common.reserve(self.root,plan,'G13-1','native',{})
        common.write_once(self.root/'positions/G13-1/calls/native.outcome.json',{
            'position':'G13-1','purpose':'native','ordinal':1,'state':'error',
            'diagnostics':{'request_headers_sent':True,'native_terminal':'failed','stream_complete':True},
            'usage':{'prompt_tokens':10,'completion_tokens':100,'total_tokens':110,
                     'server_tool_use':{'tool_search':1,'tool_fetch':2,'tool_image_search':0}}})
        value=billing(self.root,plan,[])
        self.assertEqual(value['known_tokens_all_providers'],110)
        self.assertEqual(value['known_tool_estimate_cny'],'0.015')
        self.assertEqual(value['unknown_tool_usage_records'],0)

    def test_local_verification_checks_original_config_hash(self):
        common.prepare(self.root);source=self.root.parent/'credential-config.json';source.write_text('first',encoding='utf-8')
        common.write_once(self.root/'start.json',{'credential_file_hashes':{str(source):common.sha(source.read_bytes())}})
        source.write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'source_configuration_changed'):runner.verify_local(self.root,{'files':{}})

    def test_partial_report_keeps_all_unsubmitted_positions(self):
        common.prepare(self.root);Store(RuntimePaths(self.root/'instance')).initialize()
        value=report(self.root)
        self.assertEqual(value['planned_pairs'],48);self.assertEqual(value['eligible_pairs'],0)
        self.assertEqual(value['cache_followup_count'],0)
        self.assertEqual(value['billing']['known_real_http_calls'],0)


class Pipeline(unittest.IsolatedAsyncioTestCase):
    setUp=Budget.setUp

    async def test_six_turns_real_pipeline_with_fake_clients_and_report_resume_no_extra_calls(self):
        fixtures=common.read(Path(common.__file__).with_name('fixtures.json'))
        fixtures['groups']=[fixtures['groups'][0],fixtures['groups'][12]];fixtures['asset_templates']=fixtures['asset_templates'][:1]
        plan=common.prepare(self.root,fixtures=fixtures,trial_kind='offline_fixture');seen=[]
        async def primary(model,messages,**kwargs):
            seen.append('primary');return ModelCompletion('离线夹具原稿 </script> 保持原样。',{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},{'finish_reason':'stop'})
        async def expression(model,messages,*,save_raw):
            seen.append('expression');body='离线夹具表达 </script> 保持原样。'
            raw={'choices':[{'message':{'content':json.dumps({'body':body,'segments':[body],'selected_expression_ids':[]},ensure_ascii=False)},'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':12,'completion_tokens':7,'total_tokens':19}}
            save_raw(json.dumps(raw,ensure_ascii=False).encode());return {'diagnostics':{'request_headers_sent':True,'response_headers_received':True}}
        async def jev(model,body,**kwargs):
            seen.append('jev');choice='none' if not body['state']['public_task_available'] else 'web'
            return ModelCompletion(json.dumps({'answers':{'route':{'choice':choice}}}),None,{'request_headers_sent':True})
        async def notice(model,messages,**kwargs):
            seen.append('notice');return ModelCompletion('我去翻翻这两页。',{'total_tokens':4},{'finish_reason':'stop'})
        async def native(model,messages,**kwargs):
            seen.append('native');parser=NativeResponse();parser.consume(response(content='公开合成查询资料。',tools=True));return parser.complete()
        creds={'main':protect('E3 artificial main unique credential 381024982'),
               'jev':protect('E3 artificial Jev unique credential 981024485'),
               'jev_public':{'base_url':'https://decision.fixture.invalid/v1','name':'Jev-fixture'},'files':{}}
        overrides={'primary':primary,'expression':expression,'jev':jev,'notice':notice,'native':native}
        lock=InstanceLock(self.root/'runner.lock')
        try:await runner.execute(self.root,creds=creds,overrides=overrides)
        finally:lock.close()
        value=report(self.root);count=len(seen)
        self.assertEqual(value['eligible_pairs'],6);self.assertEqual(value['native_request_count'],2)
        self.assertEqual(value['cache_followup_count'],1);self.assertEqual(value['billing']['known_real_http_calls'],0)
        self.assertGreaterEqual(value['billing']['unknown_usage_records'],3)
        self.assertIsNone(value['billing']['actual_total_cny']);self.assertIsNone(value['billing']['jev_fee_cny'])
        page=(self.root/'E3-48轮主稿与表达全文审阅.html').read_text(encoding='utf-8')
        payload=page.split('id="payload">',1)[1].split('</script>',1)[0]
        self.assertEqual(json.loads(payload)['cases'][0]['primary'],'离线夹具原稿 </script> 保持原样。')
        self.assertNotIn('</script>',payload)
        await runner.execute(self.root,creds=creds,overrides=overrides);report(self.root)
        self.assertEqual(len(seen),count)


if __name__=='__main__':unittest.main()
