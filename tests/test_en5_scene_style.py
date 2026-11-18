"""Check the approved presentation repair without generating new speech."""
import unittest

from experiments.en5_scene_style import BUBBLE_TARGET, SOCIAL_LIMIT, short_frames, speech, without_directions


class PresentationRepair(unittest.TestCase):
    def test_roles_are_removed_and_literal_parentheses_are_preserved(self):
        self.assertEqual(without_directions('（眨眨眼，语气轻快）甲。（立刻凑近屏幕）乙。'),'甲。乙。')
        self.assertEqual(without_directions('(轻笑)甲。\n（乙。）'),'甲。\n乙。')
        literal='甲“（笑）”乙。f(x) 等于 （2+3）。'
        self.assertEqual(without_directions(literal),literal)
        self.assertEqual(without_directions('\x60（轻笑）\x60 是原样文本。'),'\x60（轻笑）\x60 是原样文本。')

    def test_social_limit_stops_at_sentence_boundary_and_keeps_help(self):
        first='甲'*40+'。'
        second='乙'*30+'。'
        original=first+second
        self.assertEqual(len(original),SOCIAL_LIMIT)
        self.assertEqual(speech(original+'丙。','daily'),original)
        self.assertEqual(speech(original+'丙。','help'),original+'丙。')
        self.assertEqual(speech(original+'https://example.invalid/path','daily'),original+'https://example.invalid/path')

    def test_packing_uses_pauses_and_preserves_spoken_text(self):
        text=('甲'*18+'，'+'乙'*15+'。')*3
        frames=short_frames(text,True)
        self.assertEqual(''.join(x['text'] for x in frames),text)
        self.assertTrue(all(len(x['text'])<=BUBBLE_TARGET for x in frames))
        quotation='“'+'甲'*39+'”！'
        frames=short_frames('乙。'+quotation+'丙。',True)
        self.assertEqual(''.join(x['text'] for x in frames),'乙。'+quotation+'丙。')
        self.assertTrue(any(quotation in x['text'] for x in frames))
