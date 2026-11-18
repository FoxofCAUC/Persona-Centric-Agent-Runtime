"""No transport: verify the exact review surface and dynamic receipt dependencies."""
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest

from experiments.en3_holdout_packet import (BRANCHES, ReplayAssembly, opening_requests,
                                            report, target)
from experiments.corpus_reply_study import read_private
from persona_runtime.corpus_files import CorpusError
from persona_runtime.turn_input import render_input


def position(tokens, number=1):
    bubbles = [{'message_id':str(i+1), 'text':text, 'text_hash':sha256(text.encode()).hexdigest(),
                'accepted_at':f'2026-01-01T12:00:0{i}+08:00'} for i,text in enumerate(tokens)]
    return {'turn_number':number,'text':'\n'.join(tokens),'accepted_at':bubbles[-1]['accepted_at'],'bubbles':bubbles}


class ReviewPacket(unittest.TestCase):
    def test_batched_originals_keep_order_and_only_study_local_ids(self):
        item = position(['<1>', '2\n3'])
        self.assertEqual(target(position(['1'])), '1')
        value = target(item)
        decoded = json.loads(value.split('\n',1)[1])
        self.assertEqual([row['text'] for row in decoded], ['<1>', '2\n3'])
        ids = [r['event_id'] for r in decoded]
        self.assertTrue(all(len(k) == 36 for k in ids))
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(value, target(item))
        self.assertEqual(value, render_input([{'id':r['event_id'],'text':r['text']} for r in decoded]))
        with self.assertRaisesRegex(CorpusError, 'bubble_projection'):
            target(item | {'text':'1'})

    def test_future_candidates_require_a_real_prior_result(self):
        local = object.__new__(ReplayAssembly)
        for stage in ('selection','reply'):
            with self.subTest(stage=stage), self.assertRaisesRegex(CorpusError,'first_understanding_required'):
                local.request(stage,{},None,[])

    def test_review_escapes_source_and_previews_full_requests(self):
        item = position(['<i>&1</i>'])
        scene = {'id':'01','title':'X','turns':[item],
                 'omitted_counterpart_turns':[{'message_ids':['HIDDEN-ID'],'kinds':['text']}]}
        contract = {'planned_dialogue_requests':60,'source_bundle_sha256':'0'*64,
                    'opening_requests':{'01':[{'branch':k,'stage':'understanding' if k=='R' else 'body',
                                               'wire':{'model':k,'messages':[{'role':'user','content':item['text']}]}}
                                              for k in BRANCHES]}}
        bundle = {'selector_system':{'content':'<1>'},'flash_user':'<2>','variables':{}}
        html = report({'scenes':[scene]}, contract, bundle, Path('/0'))
        self.assertIn('&lt;i&gt;&amp;1&lt;/i&gt;', html)
        self.assertNotIn('<i>&1</i>', html)
        self.assertNotIn('HIDDEN-ID', html)
        self.assertEqual(html.count('&quot;messages&quot;'),3)
        self.assertNotIn('<script',html)


@unittest.skipUnless(os.environ.get('PCAR_DIALOGUE_REPLAY_STATE'),'approved private offline material not supplied')
class PrivateOpening(unittest.IsolatedAsyncioTestCase):
    async def test_review_rebuilds_all_three_openings_and_uses_only_confirmed_history(self):
        state_path = Path(os.environ['PCAR_DIALOGUE_REPLAY_STATE'])
        state = read_private(state_path)
        previous = read_private(Path(state['fresh_scene_study_root'])/'materials.private.json')
        sample = previous['scenes'][0]['turns'][0]
        # Frozen development evidence is enough to exercise the real persona and templates here.
        item = position([sample['text']]) | {'accepted_at':sample['accepted_at']}
        item['bubbles'][0]['accepted_at'] = item['accepted_at']
        with tempfile.TemporaryDirectory() as tmp:
            with ReplayAssembly(state_path, Path(tmp), Path(state['el5_equivalence']).with_name('source-bundle.private.json')) as assembly:
                scene = {'id':'ID-S', 'turns':[item]}
                actual = await opening_requests(assembly, scene)
                self.assertEqual([r['stage'] for r in actual],['body','body','understanding'])
                self.assertEqual([r['wire']['model'] for r in actual], ['deepseek-flash','deepseek-v4-pro','deepseek-flash'])
                self.assertEqual(actual[0]['wire']['messages'],actual[1]['wire']['messages'])
                self.assertEqual(actual[0]['wire']['messages'][-1]['content'],item['text'])
                self.assertTrue(all('tools' not in r['wire'] for r in actual))
                replay = await opening_requests(assembly, scene)
                self.assertEqual(actual,replay)
                history = [(item,'ID-RECEIPT',item['accepted_at'])]
                second = position(['ID-T2'],2)
                baseline = await assembly.baseline(scene['id'],2,second,history,'R')
                with self.assertRaisesRegex(CorpusError, 'unconfirmed_history'):
                    assembly.candidate_material(scene['id'],2,second,[(item,'ID-OTHER',item['accepted_at'])],baseline)
                mat = assembly.candidate_material(scene['id'],2,second,history,baseline)
                self.assertIn('ID-RECEIPT', assembly.request('understanding',mat)[1]['messages'][-1]['content'])


if __name__ == '__main__':
    unittest.main()
