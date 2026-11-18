"""Offline branch isolation using protocol markers only."""
from datetime import datetime
import unittest

from experiments.corpus_reply_trial import public_model
from experiments.fresh_scene_assembly import Assembly, timestamp
from persona_runtime.corpus_files import CorpusError


class SceneAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.a = object.__new__(Assembly)
        self.a.persona = 'P'
        self.a.speaker = 'U'
        self.a.variables = {'time_block': {'prefix': '#'},
                            'dialogue_prompt': {'line_format': '[{time}]{speaker}={text}'},
                            'planner_reasoning': {'format': '<{thought}>'}}
        self.a.prompts = {'Flash-system.txt': 'F', 'Flash-user.template.txt': '{time_block}/{dialogue_prompt}/{extra_info_block}'}
        self.a.understanding_model = public_model('deepseek-flash', .3)
        self.a.selector_system = {'role': 'system', 'content': 'JSON'}
        self.a.selector_model = public_model('deepseek-flash', .2) | {'_json_output': True, 'max_tokens': 512}
        self.a.numbers = {1: 'A', 2: 'B'}
        self.first = '2020-01-01T00:00:00+08:00'
        self.second = '2020-01-01T00:01:00+08:00'

    def test_understanding_uses_exact_previous_speech_and_full_current(self):
        history = [('U{x}', 'P{dialogue_prompt}', self.first, self.second)]
        _, wire = self.a.understand('U{extra_info_block}', self.second, history)
        self.assertEqual(wire['messages'][1]['content'],
                         '#2020-01-01 00:01:00/[2020-01-01 00:00:00]U=U{x}\n'
                         '[2020-01-01 00:01:00]P=P{dialogue_prompt}\n'
                         '[2020-01-01 00:01:00]U=U{extra_info_block}/')
        _, other = self.a.understand('U{extra_info_block}', self.second, [('U{x}', 'P2', self.first)])
        self.assertNotIn('P{dialogue_prompt}', other['messages'][1]['content'])

    def test_selector_keeps_four_slots_and_rejects_unknown_candidates(self):
        _, wire = self.a.select('U2', self.second, [('U1', 'P1', self.first)], 'F2')
        self.assertEqual(wire['messages'], [self.a.selector_system, {'role': 'user', 'content': '#2020-01-01 00:01:00'},
                                           {'role': 'user', 'content': 'U2'}, {'role': 'user', 'content': '<F2>'}])
        self.assertEqual(self.a.parse_ids('{"selected_ids":[2,1]}'), ['B', 'A'])
        with self.assertRaises(CorpusError):
            self.a.parse_ids('{"selected_ids":[3]}')

    def test_history_requires_complete_speech_and_source_timezone(self):
        with self.assertRaises(CorpusError):
            self.a.understand('U2', self.second, [('U1', '', self.first)])
        with self.assertRaises(CorpusError):
            timestamp(datetime(2020, 1, 1).isoformat())


if __name__ == '__main__':
    unittest.main()
