"""Comparison inputs and real wire declarations cannot silently change v8 materials."""
from copy import deepcopy
import json
import unittest

from experiments.m9r_comparison import PrimaryExecution
from experiments.m9r_comparison_materials import SPEC,SCENARIOS
from persona_runtime.archive import fingerprint
from persona_runtime.companion import choose,GENERATION_PROFILE
from persona_runtime.provider_compat import build_request


class Candidate(unittest.TestCase):
    def setup_candidate(self,need):
        runner=type('Runner',(),{'plan':{'spec_hash':fingerprint(SPEC)}})()
        execution=PrimaryExecution(runner,'single_stage',SCENARIOS[1])
        execution.current={'turn':0,'id':'position'};execution.decision={'need_web':need}
        model={'base_url':'https://api.deepseek.com/v1','name':'deepseek-flash','encrypted_key':'',
            'max_tokens':1024,'max_input_chars':24000,'timeout':60,'_companion_profile':choose('minimal_im'),
            '_generation_profile':GENERATION_PROFILE,'_reply_mode':'context_full','_expression_snapshot':None}
        return execution,model

    def test_single_stage_keeps_persona_text_but_changes_only_declared_native_wire(self):
        execution,model=self.setup_candidate(True)
        messages=[{'role':'system','content':'公开人工人物材料'},{'role':'user','content':'查公开资料'}]
        before=deepcopy((model,messages));actual,wire=execution.actual(model,messages)
        payload,contract=build_request(actual,wire)
        self.assertEqual(wire,messages);self.assertEqual((model,messages),before)
        self.assertEqual(contract['response_format'],'text');self.assertIn('tools',payload)
        self.assertNotIn('_companion_profile',actual);self.assertNotIn('temperature',payload)

    def test_none_decision_does_not_declare_tools_or_replace_ordinary_v8_profile(self):
        execution,model=self.setup_candidate(False)
        actual,messages=execution.actual(model,[{'role':'user','content':'闲聊'}])
        self.assertEqual(actual,model)
        self.assertNotIn('tools',build_request(actual,messages)[0])

    def test_manifest_matches_actual_late_reply_snapshot_fields(self):
        execution,model=self.setup_candidate(True)
        turn={'model_snapshot':json.dumps({key:value for key,value in model.items() if key not in ('_reply_mode','_expression_snapshot')}),
            'reply_mode_snapshot':'context_full','expression_snapshot':None}
        prepared={'messages':[{'role':'user','content':'公开问题'}],'manifest':{}}
        execution.before_dispatch(None,turn,prepared)
        actual,messages=execution.actual(model,prepared['messages'])
        self.assertEqual(prepared['manifest']['provider_request_contract'],build_request(actual,messages)[1])
        self.assertEqual(prepared['manifest']['model_snapshot_hash'],fingerprint({key:value for key,value in actual.items() if key!='encrypted_key'}))

    def test_private_or_changed_material_contract_is_rejected(self):
        execution,model=self.setup_candidate(True);execution.runner.plan['spec_hash']='changed'
        with self.assertRaisesRegex(ValueError,'public_fixture_required'):
            execution.actual(model,[{'role':'user','content':'公开问题'}])

    def test_cached_source_is_not_reported_as_newly_fetched_material(self):
        from experiments.m9r_comparison import newly_acquired_material
        record={'sources':[{'id':'cached'}],'retrieval':{'executor':'deepseek-native-web-v1','needed':False,'material_acquired':False}}
        self.assertFalse(newly_acquired_material(record))
        record['retrieval']['material_acquired']=True
        self.assertTrue(newly_acquired_material(record))


if __name__=='__main__':unittest.main()
