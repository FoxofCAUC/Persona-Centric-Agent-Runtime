"""表达和学习分别取得角色用途许可；全部使用人工私有夹具。"""
import unittest

from persona_runtime.models import RuntimeErrorBase
from persona_runtime.private_personas import guard_cloud_dispatch,provider_identity
from tests.test_private_copy_authorization import PrivateCopyAuthorizationTests


class PurposeGrants(unittest.TestCase):
    setUp=PrivateCopyAuthorizationTests.setUp
    copy=PrivateCopyAuthorizationTests.copy

    def test_main_grant_does_not_authorize_expression_and_each_purpose_is_provider_bound(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        private.authorize_main(store,pid,provider_identity(model),'main-only')
        for purpose in ('expression','expression_learning'):
            with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):guard_cloud_dispatch(store,pid,purpose,model)
        grant=private.authorize_model(store,pid,'expression',provider_identity(model),'expression-visible')
        self.assertEqual(private.authorize_model(store,pid,'expression',provider_identity(model),'expression-visible'),grant)
        guard_cloud_dispatch(store,pid,'expression',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):guard_cloud_dispatch(store,pid,'expression',model|{'name':'other'})
        with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):guard_cloud_dispatch(store,pid,'expression_learning',model)

    def test_learning_grant_is_independent_of_expression_grant(self):
        copied,paths,store,cfg,private=self.copy();pid=copied['persona_id'];model=cfg.load()['model']
        private.authorize_model(store,pid,'expression_learning',provider_identity(model),'learning-visible')
        guard_cloud_dispatch(store,pid,'expression_learning',model)
        with self.assertRaisesRegex(RuntimeErrorBase,'not_authorized'):guard_cloud_dispatch(store,pid,'expression',model)


if __name__=='__main__':unittest.main()
