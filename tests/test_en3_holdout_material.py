"""Lossless holdout extraction using marker-only messages and frozen desktop boundaries."""
import unittest

from experiments.en3_holdout_material import scene_window, user_batches
from persona_runtime.corpus_files import CorpusError, digest


def message(ordinal, epoch, role, text, kind='text'):
    return {'ordinal':ordinal,'epoch':epoch,'role':role,'kind':kind,'message_id':f'm{ordinal}',
            'text':text,'text_hash':digest(text.encode()),'verbatim':True,'source_candidate':True,
            'target_candidate':True,'redactions':[],'flags':[],'edge':False}


def fixture():
    rows = [message(1,0,'A','A0'),message(2,4,'A','A1'),message(3,8,'B','B0')]
    rows.extend(message(n+4,9+6*n,'A',f'A{n+2}') for n in range(5))
    return rows


def window(rows, first=1, used=None):
    return scene_window({'id':'F','group_id':'G'},rows,{'title':'T','first_ordinal':first},1,
                        set() if used is None else used)


class Boundaries(unittest.TestCase):
    def test_clock_and_complete_members(self):
        rows=fixture()
        actual=window(rows)
        self.assertEqual(actual['turn_count'],6)
        self.assertEqual(actual['turns'][0]['text'],'A0\nA1')
        self.assertEqual(actual['turns'][0]['message_ids'],['m1','m2'])
        self.assertEqual(actual['omitted_counterpart_turns'][0],
                         {'after_turn':1,'seconds':5,'reason':'quiet','message_ids':['m3'],'kinds':['text']})
        self.assertNotIn('B0',str(actual))
        self.assertEqual([len(b) for b in user_batches(rows,'A')],[2,1,1,1,1,1])

    def test_disallows_cut_batches_gaps_and_repeated_inputs(self):
        with self.assertRaisesRegex(CorpusError,'partial_batch_start'):
            window(fixture(),2)
        with self.assertRaisesRegex(CorpusError,'incomplete_window'):
            window(fixture()[:-1])
        with self.assertRaisesRegex(CorpusError,'repeated_input'):
            window(fixture(),used={'a3'})
        rows=fixture()
        rows[2]['kind']='voice'
        with self.assertRaisesRegex(CorpusError,'attachment'):
            window(rows)
        rows=fixture()
        rows[4]['flags']=['shared_short_text_context_only']
        with self.assertRaisesRegex(CorpusError,'ineligible'):
            window(rows)

    def test_early_response_does_not_split_or_disappear_inside_input(self):
        rows=fixture()
        rows.insert(1,message(0,1,'B','B2'))
        with self.assertRaisesRegex(CorpusError,'inside_batch'):
            window(rows)
        rows=fixture()
        rows[3]['epoch']=2
        with self.assertRaisesRegex(CorpusError,'clock_order'):
            window(rows)


if __name__=='__main__':
    unittest.main()
