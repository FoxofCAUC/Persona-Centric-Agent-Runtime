"""Source portability and request boundaries, using protocol markers only."""
from copy import deepcopy
import unittest

from persona_runtime.corpus_files import CorpusError
from experiments.body_repair_material import projection_reason, repair_messages


class RepairMaterialTests(unittest.TestCase):
    def setUp(self):
        self.base = [{'role':'system','content':'ID-PERSONA'}, {'role':'user','content':'ID-CURRENT'}]
        self.binding = {'variables':{
            'expression_habits_block':{'prefix':'P','item_format':'{situation}/{style}','suffix_if_present':'\n\n'},
            'planner_reasoning':{'format':'U[{thought}]'}}}

    def test_long_source_text_is_not_injected_and_short_style_remains(self):
        first = {'id':'1','situation':'SCENE-A','style':'“FULL-SOURCE”','source_quote':'full source'}
        second = {'id':'2','situation':'SCENE-B','style':'“Y”','source_quote':'Y'}
        original = deepcopy([first,second])
        messages, kept, omitted = repair_messages(self.base,self.binding,'ID-FLASH',[first,second],'ID-CURRENT')
        self.assertEqual(kept,['2'])
        self.assertEqual(omitted,{'1':'source_literal_four_or_more'})
        self.assertEqual(messages,[self.base[0],{'role':'user','content':'PSCENE-B/“Y”\n\nU[ID-FLASH]'},self.base[1]])
        self.assertEqual([first,second],original)

    def test_reference_blocks_cannot_turn_the_current_message_into_two_messages(self):
        messages,_,_ = repair_messages(self.base,self.binding,'ID-CURRENT',[],'ID-CURRENT')
        self.assertEqual(messages,self.base)
        with self.assertRaises(CorpusError):
            repair_messages(self.base,self.binding,'ID-FLASH',[],'DIFFERENT-ID')

    def test_unbound_example_and_explicit_empty_parenthesis_instruction(self):
        self.assertEqual(projection_reason({'style':'“A…”','source_quote':'A-B'}),'unbound_example_slot')
        self.assertEqual(projection_reason({'style':'括号','source_quote':'ID（）'}),'source_blank_parenthetical_instruction')
        self.assertIsNone(projection_reason({'style':'ID','source_quote':'（DATA）'}))


if __name__ == '__main__':
    unittest.main()
