"""Release defaults and 20->21 rollback with artificial data; no network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime import storage
from persona_runtime.companion import choose, RESTORE_KEY
from persona_runtime.context_policy import compatibility_defaults, initialize
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from tests.test_feature_defaults import FeatureDefaultsTests


class ReleasePolicyTests(FeatureDefaultsTests):
    def test_disable_reenable_and_versioned_release_origin(self):
        for channel in ('desktop','qq'):
            first=self.ingress.current_policy(self.a,channel)
            args=dict(persona_id=self.a,channel=channel,mode=first['mode'],expected_version=first['id'],operation_id=identifier(),companion_mode='disabled')
            off=self.ingress.update_policy(**args)
            self.assertEqual(self.ingress.update_policy(**args)['id'],off['id'])
            self.assertEqual(off['delivery']['state'],'disabled')
            self.assertEqual(off['reply_mode'],'natural_structured')
            self.assertEqual(off['character_retrieval']['mode'],'hybrid_llm')
            self.store.initialize()
            self.assertEqual(self.ingress.current_policy(self.a,channel)['id'],off['id'])
            active=self.ingress.update_policy(self.a,channel,off['mode'],off['id'],identifier(),
                companion_mode='minimal_im',reply_mode='context_full',delivery_source='release')
            self.assertEqual(active['companion'],choose('minimal_im'))
            self.assertEqual(active['delivery']['source'],'release')
            self.assertEqual(json.loads(active['data_json'])[RESTORE_KEY]['data'],json.loads(off['data_json']))

    def test_legacy_disabled_is_inferred_without_rewriting_history(self):
        p=self.legacy(self.a)
        with self.store.connection(write=True) as db:
            data=json.loads(p['data_json']);data.pop('delivery')
            db.execute('UPDATE interaction_policies SET data_json=? WHERE id=?',(json.dumps(data),p['id']))
            before=[tuple(r) for r in db.execute('SELECT * FROM interaction_policies ORDER BY id')]
            initialize(db);initialize(db,self.a,new_persona=True)
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM interaction_policies ORDER BY id')])
        self.assertEqual(self.ingress.current_policy(self.a,'desktop')['delivery']['state'],'disabled')

    def test_second_channel_failure_rolls_back_entire_creation(self):
        from persona_runtime.context_policy import new_persona_defaults
        with self.store.connection() as db:before=[tuple(r) for r in db.execute('SELECT * FROM personas')]
        count=0
        def fail(*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('second channel fault')
            return new_persona_defaults(*args,**kwargs)
        with patch('persona_runtime.context_policy.new_persona_defaults',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'second channel fault'):self.service.create_persona(generic_persona())
        with self.store.connection() as db:self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM personas')])


class MigrationTests(unittest.TestCase):
    def test_artificial_schema20_migration_and_restore(self):
        from scripts.verify_stage7_migration import verify
        home=RuntimePaths.from_environment().home
        with tempfile.TemporaryDirectory(dir=home/'exports/acceptance',prefix='stage7-artificial-') as folder:
            paths=RuntimePaths(Path(folder)/'source')
            with patch('persona_runtime.storage.SCHEMA_VERSION',20),patch('persona_runtime.context_policy.new_persona_defaults',side_effect=compatibility_defaults):
                store=storage.Store(paths);store.initialize();service=RuntimeService(store)
                pid=service.create_persona(generic_persona())['id'];service.use_persona(pid)
                session=service.create_session();service.append_event(session['id'],'user','人工迁移资料，不含私人内容。')
            with patch('persona_runtime.storage.SCHEMA_VERSION',21):
                result=verify(store.path,Path(folder)/'evidence')
            self.assertTrue(result['source_unchanged'])


if __name__=='__main__':unittest.main()
