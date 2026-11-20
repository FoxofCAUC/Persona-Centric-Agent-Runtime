"""Exercise the new publication cap with preserved source offsets."""

import unittest

from persona_runtime.bubble_limits import (
    BubbleLimitError, display_bubbles, limit_ranges, save_display_boundaries,
)
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.scene_style import short_frames


class BubbleLimits(unittest.TestCase):
    def test_split_remainder_of_scene_plan_without_losing_text(self):
        text = ("R" * 25 + "。") * 22 + "R"
        plan = short_frames(text, True)
        self.assertGreater(plan[-1]["end"] - plan[-1]["start"], 75)
        limited = limit_ranges(text, plan)
        self.assertEqual("".join(text[item["start"]:item["end"]] for item in limited), text)
        self.assertTrue(all(0 < item["end"] - item["start"] <= 75 for item in limited))
        self.assertEqual(limited[:11], plan[:11])

    def test_split_uninterrupted_text_without_slicing_combining_characters(self):
        text = "R" * 74 + "x\u0301R"
        limited = limit_ranges(text, [{"start": 0, "end": len(text)}])
        parts = [text[item["start"]:item["end"]] for item in limited]
        self.assertEqual(parts, ["R" * 74, "x\u0301R"])

    def test_oversized_exact_reference_waits_for_detail(self):
        text = "https://example.invalid/" + "x" * 77
        with self.assertRaisesRegex(BubbleLimitError, "reply_bubble_detail_required"):
            limit_ranges(text, [{"start": 0, "end": len(text)}])

    def test_restored_layout_is_bounded_and_bound_to_original_display(self):
        text = "R" * 76
        state = {}
        self.assertIsNone(display_bubbles(text, state))
        save_display_boundaries(text, state)
        self.assertEqual(display_bubbles(text, state), ["R" * 75, "R"])
        with self.assertRaisesRegex(RuntimeErrorBase, "reply_bubble_limit"):
            display_bubbles("S" + text[1:], state)
        state["desktop_bubbles"]["ends"] = [76]
        with self.assertRaisesRegex(RuntimeErrorBase, "reply_bubble_limit"):
            display_bubbles(text, state)
