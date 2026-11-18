"""Offline presentation checks. These strings stay in the review artifact."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from experiments.body_repair_review import export_review, freeze_sides
from experiments.corpus_reply_study import read_private, save_once
from persona_runtime.corpus_files import CorpusError, digest


class RepairReviewTests(unittest.TestCase):
    def test_blinding_preserves_text_and_keeps_reuse_and_missing_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checked_hash = freeze_sides(root, [1, 2, 3])
            cases, rows = [], []
            for n in (1, 2, 3):
                def receipt(token):
                    text = token + str(n) + '\r\n</script> \n'
                    sha = digest(text.encode('utf-8'))
                    return {'body': text, 'request_hash': sha, 'raw_hash': sha, 'state': 'completed'}
                base, old = receipt('B'), receipt('S')
                reply = old if n == 2 else receipt('R')
                if n == 3:
                    reply.update(state='unknown', body=None, raw_hash=None)
                cases.append(dict(position=n, input=str(n), candidate=reply, baseline=base,
                                  previous=old, reused=n == 2))
                rows.append(dict(position=n, input=str(n), controls=dict(B=base, S=old),
                                 reuse=old if n == 2 else None, request_hash=reply['request_hash']))
            save_once(root / 'manifest.private.json', dict(version='reviewed-body-repair-v1',
                                                           blind_key_sha256=checked_hash, positions=rows))
            page, summary = export_review(root / 'review', cases, study_id='test')
            self.assertEqual(summary['comparisons']['S'], dict(different=1, identical=1, incomplete=1))
            self.assertEqual(summary['comparisons']['B'], dict(different=2, identical=0, incomplete=1))
            self.assertEqual((summary['new_completed'], summary['reused']), (1, 1))
            key = read_private(root / 'blind-key.private.json')['positions']
            html = (page / 'index.html').read_text(encoding='utf-8')
            payload = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>',
                                           html, re.DOTALL).group(1))
            for pos, row in enumerate(payload['cases']):
                self.assertEqual(set(row), {'position', 'input', 'pairs'})
                for g in ('B', 'S'):
                    side = key[str(pos+1)][g]
                    entry = row['pairs'][g]['sides'][0 if side == 'left' else 1]
                    self.assertEqual(entry['text'], cases[pos]['candidate']['body'])
            self.assertEqual(export_review(page, cases, study_id='test'), (page, summary))
            cases[0]['baseline'] = dict(cases[0]['baseline'], body='changed')
            with self.assertRaises(CorpusError):
                export_review(page, cases, study_id='test')

    def test_missing_preassigned_key_cannot_be_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_once(root / 'manifest.private.json', {})
            with self.assertRaises(CorpusError):
                freeze_sides(root, [1])

    def test_candidate_display_and_extra_control_keep_original_receipt_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = {g: g for g in ('B', 'S', 'X')}
            key_hash = freeze_sides(root, [1], names)
            raw, shown = 'ID:ABC', 'ABC'
            h = digest(raw.encode())
            original = dict(state='completed', request_hash=h, raw_hash=h, body=raw)
            controls = {g: original for g in names}
            row = dict(position=1, input='I', controls=controls, request_hash=h, reuse=original)
            save_once(root / 'manifest.private.json', dict(version='reviewed-body-repair-v1',
                      blind_key_sha256=key_hash, control_labels=names, positions=[row]))
            view = dict(body=raw, body_sha256=h, display_body=shown, display_sha256=digest(shown.encode()),
                        removed_spans=[dict(start=0, end=3, text='ID:')], version='V', coordinate='C', reason='R')
            with patch('experiments.body_repair_display.project_display', return_value=view) as projector:
                page, summary = export_review(root/'review', [dict(position=1, input='I', candidate=original,
                                controls=controls, reused=True)], study_id='T')
            projector.assert_called_once_with(raw, user_text='I')
            self.assertEqual(summary['presentation_changed_count'], 1)
            self.assertEqual(summary['comparisons']['X']['different'], 1)
            entry = read_private(page/'results.private.json')['cases'][0]
            self.assertEqual(entry['controls']['X']['body'], raw)
            self.assertEqual(entry['candidate']['raw_body_hash'], h)
            self.assertEqual(entry['candidate']['body_sha256'], digest(shown.encode()))
            self.assertEqual(entry['candidate']['removed_spans'], view['removed_spans'])
