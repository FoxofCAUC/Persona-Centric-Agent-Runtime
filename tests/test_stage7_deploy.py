"""Scoped deployment and compensation; only artificial identities, no dispatch."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from persona_runtime.context_policy import compatibility_defaults
from persona_runtime.models import generic_persona
from tests import test_feature_defaults as fixture
from scripts.stage7_deploy import switch,idle


class DeploymentTests(unittest.TestCase):
    setUp=fixture.FeatureDefaultsTests.setUp

    def legacy_target(self):
        with patch('persona_runtime.context_policy.new_persona_defaults',side_effect=compatibility_defaults):
            pid=self.service.create_persona(generic_persona())['id']
        self.service.use_persona(pid)
        return pid,{c:self.ingress.current_policy(pid,c) for c in ('desktop','qq')}

    def test_two_channels_only_and_other_identity_bytes_unchanged(self):
        pid,heads=self.legacy_target()
        with self.store.connection() as db:before=[tuple(r) for r in db.execute('SELECT * FROM interaction_policies WHERE persona_id!=? ORDER BY id',(pid,))]
        results=switch(self.ingress,pid,heads)
        self.assertEqual(set(results),{'desktop','qq'})
        self.assertTrue(all(r['delivery']['source']=='release' for r in results.values()))
        with self.store.connection() as db:self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM interaction_policies WHERE persona_id!=? ORDER BY id',(pid,))])

    def test_second_channel_failure_appends_restoration_before_unpause(self):
        pid,heads=self.legacy_target();real=self.ingress.update_policy;records=[]
        def fail(*args,**kwargs):
            if args[1]=='qq':raise RuntimeError('injected QQ policy failure')
            return real(*args,**kwargs)
        with patch.object(self.ingress,'update_policy',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'injected QQ'):switch(self.ingress,pid,heads,record=records.append)
        now=self.ingress.current_policy(pid,'desktop')
        self.assertEqual(now['reply_mode'],heads['desktop']['reply_mode'])
        self.assertEqual(now['revision'],heads['desktop']['revision']+2)
        self.assertEqual(self.ingress.current_policy(pid,'qq')['id'],heads['qq']['id'])
        self.assertIn('rolled_back',records[-1])

    def test_explicit_off_and_stale_selection_are_refused(self):
        pid,heads=self.legacy_target();heads['desktop']['delivery']={'state':'disabled'}
        with self.assertRaisesRegex(ValueError,'fresh preview'):switch(self.ingress,pid,heads)
        self.assertEqual(self.ingress.current_policy(pid,'desktop')['id'],heads['desktop']['id'])
        heads['desktop']['delivery']=None;self.service.use_persona(self.a)
        with self.assertRaisesRegex(ValueError,'persona_selection_conflict'):switch(self.ingress,pid,heads)

    def test_pending_legacy_shadow_uses_parts_authority_without_hiding_unknown(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        for table in ('turns','organization_tasks','attempts','organization_attempts'):
            db.execute('CREATE TABLE '+table+' (status TEXT)')
        for table in ('input_batches','quota_reservations'):
            db.execute('CREATE TABLE '+table+' (state TEXT)')
        for table in ('deliveries','reply_delivery_parts'):
            db.execute('CREATE TABLE '+table+' (turn_id TEXT,state TEXT)')
        db.execute('CREATE TABLE legacy_delivery_dispositions (turn_id TEXT)')
        db.execute("INSERT INTO deliveries VALUES ('v2','pending')")
        with self.assertRaisesRegex(ValueError,'Unresolved work'):idle(db)
        db.execute("INSERT INTO reply_delivery_parts VALUES ('v2','sent')")
        self.assertFalse(any(idle(db).values()))
        db.execute("UPDATE reply_delivery_parts SET state='uncertain'")
        with self.assertRaisesRegex(ValueError,'Unresolved work'):idle(db)


if __name__=='__main__':unittest.main()
