"""Exercise the offline review boundary using opaque local-only identifiers."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from experiments.corpus_reply_study import read_private, save_once
from experiments.fresh_scene_review import export, freeze_key
from persona_runtime.corpus_files import CorpusError, digest


def fixture(root):
    scenes, cases = [], []
    for scene_id in ('X1', 'X2'):
        turns = []
        for turn in range(1, 5):
            user = scene_id + '-' + str(turn)
            turns.append({'text': user})
            bodies = {}
            for chain in ('B', 'R', 'S'):
                text = (chain if turn != 3 else '=') + user + '\r\n</script>\n'
                h = digest(text.encode())
                bodies[chain] = dict(state='completed', body=text, raw_hash=h, request_hash=h)
            if turn == 4:
                bodies['S'] = dict(state='unknown', body=None)
            cases.append(dict(scene_id=scene_id, title=scene_id, turn=turn, input=user, bodies=bodies))
        scenes.append(dict(id=scene_id, title=scene_id, turns=turns))
    sha = freeze_key(root, [s['id'] for s in scenes])
    manifest_sha = save_once(root / 'manifest.private.json', dict(version='fresh-private-dialogue-v1',
                             study_id='T', blind_key_sha256=sha, scenes=scenes))
    result = dict(study_id='T', manifest_sha256=manifest_sha, cases=cases)
    save_once(root / 'results.private.json', result)
    return result


class FreshSceneReviewTests(unittest.TestCase):
    def test_full_scene_bindings_partial_receipts_and_stable_sides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture(root)
            page = export(root)
            self.assertEqual(page, export(root))
            markup = page.read_text(encoding='utf-8')
            match = re.search(r'<script type="application/json" id="data">(.*?)</script>', markup, re.S)
            view = json.loads(match.group(1))
            self.assertEqual(view['result_set_hash'], digest((root / 'results.private.json').read_bytes()))
            self.assertEqual(view['comparisons']['B'], dict(different=6, identical=2, incomplete=0))
            self.assertEqual(view['comparisons']['S'], dict(different=4, identical=2, incomplete=2))
            key_path = root / 'blind-key.private.json'
            key = read_private(key_path)['scenes']
            self.assertEqual(view['blind_key_sha256'], digest(key_path.read_bytes()))
            for group in ('B', 'S'):
                self.assertEqual({s[group] for s in key.values()}, {'left', 'right'})
                for scene in view['scenes']:
                    side = ['left', 'right'].index(key[scene['id']][group])
                    for turn in scene['turns']:
                        text = turn['pairs'][group]['sides'][side]['text']
                        self.assertEqual(text, ('=' if turn['turn'] == 3 else 'R')
                                         + turn['input'] + '\r\n</script>\n')
                        if turn['turn'] == 4 and group == 'S':
                            self.assertIsNone(turn['pairs'][group]['sides'][1-side]['text'])

    def test_invalid_source_and_missing_or_changed_key_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = fixture(root)
            path = root / 'results.private.json'
            result['cases'].pop(0)
            path.write_text(json.dumps(result), encoding='utf-8')
            with self.assertRaises(CorpusError):
                export(root)
            with self.assertRaises(CorpusError):
                freeze_key(root, ['X2', 'X1'])
            (root / 'blind-key.private.json').unlink()
            with self.assertRaises(CorpusError):
                freeze_key(root, ['X1', 'X2'])
