"""Guard the exact deletion and verify the completed private comparison when requested."""
import json
import os
from pathlib import Path
import unittest

from experiments.en5_guidance_removal import HEADER,remove_instruction,build
from experiments.en5_guidance_review import bound
from persona_runtime.companion_im import INSTRUCTION
from persona_runtime.models import RuntimeErrorBase


class Deletion(unittest.TestCase):
    def test_only_the_existing_guidance_prefix_is_removed(self):
        tail='{"ID":"referenced"}\nID-2'
        wire={'model':'ID-M','stream':True,'thinking':{'type':'disabled'},
              'messages':[{'role':'system','content':INSTRUCTION+tail},
                          {'role':'assistant','content':'ID-1'},
                          {'role':'user','content':'ID-2'}]}
        updated,removed=remove_instruction(wire)
        self.assertEqual(removed+HEADER,INSTRUCTION)
        self.assertEqual(updated['messages'][0]['content'],HEADER+tail)
        self.assertEqual(updated['messages'][1:],wire['messages'][1:])
        self.assertEqual(updated|{'messages':wire['messages']},wire)
        self.assertEqual(wire['messages'][0]['content'],INSTRUCTION+tail)
        wrong=wire|{'messages':[{'role':'system','content':INSTRUCTION[:-1]+tail},*wire['messages'][1:]]}
        with self.assertRaisesRegex(RuntimeErrorBase,'guidance_boundary_changed'):remove_instruction(wrong)


@unittest.skipUnless(os.environ.get('PCAR_GUIDANCE_REVIEW_ROOT'),'private result not supplied')
class FixedHistory(unittest.TestCase):
    def test_original_request_scope_and_first_receipts(self):
        root=Path(os.environ['PCAR_GUIDANCE_REVIEW_ROOT'])
        view,manifest,_,_,audit,counts=bound(root)
        self.assertEqual(build(Path(manifest['prior']),root),
                         {k:v for k,v in manifest.items() if k!='preview_sha256'})
        self.assertEqual(len(view['cases']),10)
        self.assertEqual(len(audit),counts['visible_states']['completed'])
        self.assertEqual(len({a['request_sha256'] for a in audit}),len(audit))
        self.assertEqual(counts['reused_original_B'],10)
