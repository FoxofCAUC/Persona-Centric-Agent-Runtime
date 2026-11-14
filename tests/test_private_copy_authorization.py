"""Artificial copy consent is explicit and does not transfer to other roots."""
import json
from pathlib import Path
import shutil
import unittest

from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas,guard_cloud_dispatch,provider_identity
from persona_runtime.storage import Store
from tests import test_pre_m8 as fixtures

class PrivateCopyAuthorizationTests(unittest.TestCase):
    setUp=fixtures.PrivateTests.setUp

    def copy(self):
        copied=self.private.create_sandbox(self.rec['id'],visible_only=True)
        self.addCleanup(self.private.cleanup_sandbox,copied['home'])
        paths=RuntimePaths(Path(copied['home']));cfg=ConfigStore(paths);cfg.initialize()
        return copied,paths,Store(paths),cfg,PrivatePersonas(paths)

    def test_explicit_main_grant_is_idempotent_and_provider_bound(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',model)
        grant=private.authorize_main(store,pid,provider_identity(model),'copy-explicit-main')
        self.assertEqual(private.authorize_main(store,pid,provider_identity(model),'copy-explicit-main'),grant)
        guard_cloud_dispatch(store,pid,'main',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',dict(model,name='other-model'))

    def test_retired_router_cannot_receive_new_cloud_grants(self):
        copied,paths,store,cfg,private=self.copy()
        with self.assertRaisesRegex(RuntimeErrorBase,'legacy_retrieval_frozen'):
            private.authorize_model(store,copied['persona_id'],'knowledge_router',provider_identity(cfg.load()['model']),'retired-router')

    def test_visible_copy_main_needs_own_grant_even_when_legacy_provider_matches(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',model)
        private.authorize_main(store,pid,provider_identity(model),'copy-own-main')
        guard_cloud_dispatch(store,pid,'main',model)
        other=paths.home.parent/(paths.home.name+'-copied-main')
        shutil.copytree(paths.home,other);self.addCleanup(self.private.cleanup_sandbox,other)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(Store(RuntimePaths(other)),pid,'main',model)

    def test_copy_instance_identity_remains_bound_after_config_change(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        private.authorize_main(store,pid,provider_identity(model),'copy-main-bound')
        data=cfg.load();data['instance_id']='different-instance';atomic_json(cfg.file,data)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
            guard_cloud_dispatch(store,pid,'main',model)

    def test_visible_only_excludes_creator_material_and_source_relationship(self):
        hidden=dict(self.package['character']['lorebook'][0],id='hidden',visibility='creator_only',text='Artificial hidden data')
        self.package['character']['lorebook'].append(hidden)
        self.source.write_text(json.dumps(self.package,ensure_ascii=False),encoding='utf-8')
        self.rec=self.private.register(self.source,self.archive,'人工初始关系，不是实际经历。')
        copied,paths,store,_,_=self.copy()
        from persona_runtime.character import CharacterService
        from persona_runtime.profile import head
        self.assertTrue(all(r['visibility']=='conversation' for r in CharacterService(store).current(copied['persona_id'])['data']['lorebook']))
        with store.connection() as db:
            self.assertNotEqual(head(db,copied['persona_id'],'relationship')['data']['description'],'人工初始关系，不是实际经历。')
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
        self.assertFalse((paths.home/'source-archive.md').exists())

    def test_background_grants_and_loose_markers_cannot_authorize_a_copy(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id']
        with self.assertRaisesRegex(RuntimeErrorBase,'private_copy_permission_kind_not_allowed'):
            private.authorize_model(store,pid,'organization',provider_identity(cfg.load()['model']),'deny-background')
        (paths.home/'private-sandbox.json').unlink()
        with self.assertRaisesRegex(RuntimeErrorBase,'private_scope_mismatch'):
            private.authorize_main(store,pid,provider_identity(cfg.load()['model']),'deny-loose-copy')
        # Restore ownership metadata solely so the normal scoped cleanup can run.
        atomic_json(paths.home/'private-sandbox.json',{'private_id':self.rec['id'],'persona_id':pid,'formal_home':str(self.paths.home.resolve())})

if __name__=='__main__':unittest.main()
