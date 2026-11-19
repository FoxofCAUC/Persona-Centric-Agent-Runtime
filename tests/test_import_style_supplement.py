"""Local regression coverage for exact supplement prescreen evidence."""

import importlib.util
import unittest

from experiments.import_style_supplement import coverage, match_map, pick_requests


class SupplementTraceTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Optional local embedding dependency")
    def test_match_map_uses_best_distinct_pair_and_excludes_replay(self):
        import numpy as np

        messages = []
        fragments = []
        for first, text, split, group in (
                (1, "100", "learn", "prior"),
                (3, "101", "learn", "learn1"),
                (5, "102", "learn", "learn2"),
                (7, "200", "learn", "learn3"),
                (9, "200", "dev_review", "dev1"),
                (11, "300", "dev_review", "dev2")):
            fragment = str(first)
            fragments.append({"id":fragment, "group_id":group})
            messages.extend(({"message_id":str(first), "fragment_id":fragment, "text":text,
                              "role":"B", "split":split, "target_candidate":True},
                             {"message_id":str(first+1), "fragment_id":fragment, "text":"900",
                              "role":"A", "split":split, "target_candidate":True}))
        view = {"messages":messages, "fragments":fragments}
        wires = {
            "A-0001.json":{"role":"A", "records":[{"id":str(i)} for i in (1,2)]},
            "A-0002.json":{"role":"A", "records":[{"id":str(i)} for i in (3,4,5,6,7,8)]},
        }
        vectors = {"100":[1.,0.], "101":[.83,0.], "102":[.95,0.], "200":[1.,0.], "300":[0.,1.]}

        class Provider:
            def embed_documents(self, texts):
                return np.array([vectors[text] for text in texts])

        provider = Provider()
        used = {"A":{"A-0001.json"}}
        trace = match_map(provider, view, wires, used, "A", .82)
        self.assertEqual(set(trace), {"A-0002.json"})
        self.assertEqual(trace["A-0002.json"], {"dev1":{
            "cue_id":"5", "reply_id":"6", "dev_cue_id":"9", "score":.95}})
        self.assertEqual(coverage(provider, view, wires, used, "A", .82), {"A-0002.json":{"dev1"}})

    def test_pick_requests_emits_only_new_group_matches_in_legacy_order(self):
        first = {"cue_id":"1", "reply_id":"2", "dev_cue_id":"8", "score":.9}
        second = {"cue_id":"3", "reply_id":"4", "dev_cue_id":"9", "score":.85}
        requests = {
            "A-0002.json":{"a":first, "b":first},
            "A-0003.json":{"a":second, "c":second},
        }
        self.assertEqual(pick_requests({name:set(groups) for name, groups in requests.items()}), [
            {"name":"A-0002.json", "additional_dev_groups":2},
            {"name":"A-0003.json", "additional_dev_groups":1},
        ])
        self.assertEqual(pick_requests(requests), [
            {"name":"A-0002.json", "additional_dev_groups":2,
             "matches":[{"dev_group_id":g, **first} for g in ("a","b")]},
            {"name":"A-0003.json", "additional_dev_groups":1,
             "matches":[{"dev_group_id":"c", **second}]},
        ])
