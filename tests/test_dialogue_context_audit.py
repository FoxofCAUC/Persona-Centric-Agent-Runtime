from copy import deepcopy
import unittest
from persona_runtime.archive import fingerprint
from persona_runtime.models import RuntimeErrorBase
from scripts.dialogue_context_audit import compare

def record(content):
    return {'messages':[{'role':'system','content':content}],'input_hash':fingerprint(content),
        'manifest':{'model_snapshot_hash':'same','prompt_assembly':{'blocks':[
            {'name':'persona_expression','message_index':0,'start_character':0,'characters':len(content),'content_hash':fingerprint(content)}]}}}

class Audit(unittest.TestCase):
    def test_same_request_is_reported_as_same(self):
        self.assertTrue(compare(record('人工'),record('人工'))['identical'])
    def test_changed_main_parameters_are_rejected(self):
        a=record('人工');b=deepcopy(a);b['manifest']['model_snapshot_hash']='changed'
        with self.assertRaisesRegex(RuntimeErrorBase,'parameter_difference'):compare(a,b)
    def test_unregistered_prompt_change_is_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'unexplained_block_difference'):compare(record('旧提示'),record('新提示'))
