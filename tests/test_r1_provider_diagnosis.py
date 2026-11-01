"""Artificial diagnostic classification and single-variable probe contracts."""
from copy import deepcopy
import unittest
from scripts.diagnose_r1_provider import content_stats,plans,CLARIFY,classify


class Diagnosis(unittest.TestCase):
    def test_blank_classes_are_distinct_and_body_free(self):
        expected=[(None,'null'),([], 'non_string'),('','empty_string'),('   ','ascii_spaces_only'),(' \r\n\t','whitespace_only')]
        for value,kind in expected:self.assertEqual(content_stats(value)['kind'],kind)
        stats=content_stats(' '*130)
        self.assertEqual(stats['whitespace_codepoints'],{'U+0020':130})
        self.assertEqual(stats['non_whitespace_characters'],0)
        self.assertNotIn('text',stats)
    def test_json_and_plaintext_distinct(self):
        self.assertTrue(content_stats('{"ok":true}')['json_parseable'])
        self.assertFalse(content_stats('自然聊天正文')['json_parseable'])
        self.assertEqual(content_stats('自然聊天正文')['kind'],'text')
    def test_probe_changes_are_single_variable(self):
        items,original=plans()
        self.assertEqual(len(items),5);self.assertEqual(items[2]['request'],original['request'])
        without=deepcopy(items[2]['request']);without.pop('response_format')
        self.assertEqual(items[3]['request'],without)
        reminder=deepcopy(items[2]['request']);reminder['messages'][0]['content']+=CLARIFY
        self.assertEqual(items[4]['request'],reminder)
        for item in items[2:]:
            self.assertEqual(item['timeout'],180);self.assertEqual(item['request']['max_tokens'],8192)
            self.assertEqual(item['request']['thinking'],{'type':'disabled'})
    def test_plaintext_ablation_never_treated_as_valid_chat(self):
        items,original=plans();body={'choices':[{'message':{'content':'想留就留。'},'finish_reason':'stop'}]}
        ok,result=classify(items[3],body,original['model']['_expression_snapshot'])
        self.assertFalse(ok);self.assertFalse(result['application_valid'])
        self.assertIn('application_error',result)
    def test_spaces_never_treated_as_successful_completion(self):
        items,original=plans();body={'choices':[{'message':{'content':' '*42},'finish_reason':'stop'}]}
        ok,result=classify(items[2],body,original['model']['_expression_snapshot'])
        self.assertFalse(ok);self.assertEqual(result['content']['kind'],'ascii_spaces_only')
        self.assertNotIn('application_reply',result)


if __name__=='__main__':unittest.main()
