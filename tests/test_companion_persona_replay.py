"""Stage 4-E uses the normal replay runner and its failure accounting."""
from experiments import companion_c2 as replay
from experiments import companion_persona_purpose as purpose
from tests import test_companion_c2 as fixture


class PurposeReplay(fixture.Replay):
    replay_version = purpose.VERSION
    async def test_sixteen_runtime_targets_only_contract_diff_and_no_success_replay(self):
        root = await replay.prepare(self.home, version=purpose.VERSION)
        owner, plan = replay.verify(root)
        self.assertEqual(len(plan['positions']), 12)
        e2=next(c for c in plan['cases'] if c['id']=='E2')
        self.assertEqual(e2['category'],'known_hard_field')
        self.assertEqual(e2['field_id'],'identity.gender')
        self.assertTrue(all(r['only_persona_presentation_changed'] for r in plan['pair_checks']))
        client = replay.base.FixtureClient()
        result = await replay.execute(root, simulated=True, client=client)
        self.assertEqual(result['completed'], 12)
        self.assertEqual(result['physical_calls'], 0)
        self.assertEqual(result['simulated_calls'], 12)
        for position, call in zip(plan['order'], client.calls):
            record = replay.read(replay.data_root(owner) / 'positions' / position / 'request.json')
            self.assertEqual(record['wire'], call['wire'])
        with self.assertRaisesRegex(Exception, 'no_auto_resume'):
            await replay.execute(root, simulated=True, client=client)
