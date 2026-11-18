"""D3 uses an isolated home's reviewed materials and separate per-call purpose."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from experiments.en5_d3_live_review import report
from persona_runtime.continuous_decision_use import check, current
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import provider_identity, sha


class ScopedUse(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        parent = Path(tmp.name)
        branch = parent/'branch'
        self.paths = RuntimePaths(branch/'home')
        marker = self.paths.home/'runtime/d3-binding.json'
        marker.parent.mkdir(parents=True)
        self.parent = parent/'author-use.private.json'
        self.parent.write_bytes(b'{}')
        (branch/'author-use.private.json').write_bytes(b'{}')
        data = {'home':str(self.paths.home.resolve()), 'persona_id':'p',
                'sessions':{'s':{'branch':'candidate'},'b':{'branch':'baseline'}},
                'author_use_sha256':sha(b'{}'), 'parent_use_sha256':sha(b'{}')}
        marker.write_text(json.dumps(data),encoding='utf-8')
        self.model = {'name':'id','base_url':'local'}
        self.policy = {'persona_id':'p', 'd3_binding_sha256':sha(marker.read_bytes())}
        self.context = {'home':data['home'], 'binding':self.policy['d3_binding_sha256'], 'session':'s',
                        'kind':'dialogue_decision', 'provider':provider_identity(self.model)}
        token = current.set(self.context)
        self.addCleanup(current.reset,token)

    def guard(self):
        return check(self.paths,self.policy,'dialogue_decision',self.model)

    def test_parent_withdrawal(self):
        self.assertIs(self.guard(),self.context)
        self.parent.write_bytes(b'{"approved":false}')
        with self.assertRaisesRegex(RuntimeErrorBase,'d3_use_scope_changed'):
            self.guard()

    def test_context_cannot_transfer(self):
        for ctx in (None, self.context|{'kind':'main'}, self.context|{'session':'b'},
                    self.context|{'session':'other'}, self.context|{'home':'other'}, self.context|{'provider':'other'}):
            with self.subTest(ctx=ctx):
                token=current.set(ctx)
                try:
                    with self.assertRaisesRegex(RuntimeErrorBase,'d3_use_scope_changed'):
                        self.guard()
                finally:
                    current.reset(token)


@unittest.skipUnless(os.environ.get('PCAR_EN5_D3_LIVE_ROOT'),'private D3 receipts not supplied')
class CompletedRun(unittest.TestCase):
    def test_export_reuses_first_receipts_and_blind_assignment(self):
        root = Path(os.environ['PCAR_EN5_D3_LIVE_ROOT'])
        page,counts = report(root)
        self.assertEqual((counts['baseline']['main'],counts['candidate']['main'],counts['candidate']['decision']),(30,30,30))
        original = page.read_bytes()
        again, repeated = report(root)
        self.assertEqual(counts,repeated)
        self.assertEqual(original,again.read_bytes())
