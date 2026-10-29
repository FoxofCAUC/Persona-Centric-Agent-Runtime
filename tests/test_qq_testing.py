"""Local navigation and artificial QQ identities; never contact a platform."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona,identifier,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_testing import QQTesting
from persona_runtime.quotas import QuotaService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class QQIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='qq-policy-test-',dir=root);self.addCleanup(self.tmp.cleanup)
        self.paths=RuntimePaths(Path(self.tmp.name));self.store=Store(self.paths);self.store.initialize()
        self.cfg=ConfigStore(self.paths);self.cfg.initialize();self.cfg.update({'bot_qq':'33333','owner_qq':'11111'})
        self.service=RuntimeService(self.store);self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.policy=QQTesting(self.paths);self.policy.configure('11111','22222')
        self.fake=SimpleNamespace(complete=AsyncMock(return_value=('模拟回复',{})))
        self.engine=ChatEngine(self.store,self.cfg,self.fake);self.ingress=IngressService(self.engine)
        self.addCleanup(self.engine.cognitive.characters.close)

    def acceptance(self):
        atomic_json(self.paths.home/'runtime/acceptance-quotas.json',{'status':'active'})

    async def test_daily_usage_allowed_outside_acceptance(self):
        self.policy.check_runtime('11111')
        self.assertEqual(self.cfg.load()['owner_qq'],'11111')

    async def test_acceptance_rejects_daily_on_both_ingress_versions_without_writes(self):
        self.acceptance()
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_not_ready'):
            self.ingress.accept({'channel':'qq','text':'人工测试','request_id':identifier(),'bot':'33333','sender':'11111','platform_message_id':'fake'})
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_not_ready'):
            await self.engine.submit('qq','人工测试',identifier(),origin={'bot':'33333','sender':'11111'})
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
        self.fake.complete.assert_not_called()

    async def test_enter_restore_are_explicit_and_keep_old_identity(self):
        other=self.service.create_persona(generic_persona())['id']
        with patch.object(self.policy,'_stopped'):
            self.policy.enter(other);self.policy.require_ready('22222')
            self.assertEqual(self.service.show_persona()['id'],other)
            self.assertTrue(self.policy.enter(other)['already_applied'])
            with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_forbidden'):self.policy.check_runtime('11111')
            with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_forbidden'):self.policy.check_runtime('44444')
            self.policy.restore()
        self.assertEqual(self.cfg.load()['owner_qq'],'11111')
        self.assertEqual(self.service.show_persona()['id'],self.pid)
        self.policy.check_runtime('11111')

    async def test_unresolved_work_prevents_switch(self):
        with patch.object(self.engine,'_schedule'):
            await self.engine.submit('desktop','人工未决消息',identifier())
        with patch.object(self.policy,'_stopped'),self.assertRaisesRegex(RuntimeErrorBase,'qq_test_unresolved_work'):
            self.policy.enter(self.pid)
        self.assertEqual(self.cfg.load()['owner_qq'],'11111')

    async def test_recover_after_partial_entry_and_restore(self):
        v=self.policy.load();v.update(state='entering',restore_persona_id=self.pid,test_persona_id=self.pid)
        atomic_json(self.policy.file,v)
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_not_ready'):self.policy.check_runtime('11111')
        with patch.object(self.policy,'_stopped'):
            self.policy.enter(self.pid);self.policy.restore()
        self.assertEqual(self.policy.load()['state'],'inactive')

    async def test_broken_policy_fails_closed(self):
        self.policy.file.write_text('{broken',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_policy_invalid'):self.policy.check_runtime('11111')

    async def test_missing_policy_during_acceptance_fails_closed(self):
        self.policy.file.unlink();self.acceptance()
        with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_policy_missing'):self.policy.check_runtime('11111')

    async def test_test_peer_is_accepted_only_in_test_scope(self):
        with patch.object(self.policy,'_stopped'):self.policy.enter(self.pid)
        with patch.object(self.engine,'_schedule'):
            receipt=self.ingress.accept({'channel':'qq','text':'人工测试','request_id':identifier(),
                'bot':'33333','sender':'22222','platform_message_id':'artificial-id'})
        with self.store.connection() as db:
            row=db.execute('SELECT peer,persona_id FROM input_batches WHERE id=?',(receipt['batch_id'],)).fetchone()
            self.assertEqual(tuple(row),('22222',self.pid))
        self.fake.complete.assert_not_called()

    async def test_private_original_cannot_be_selected_for_qq_acceptance(self):
        from persona_runtime.private_personas import PrivatePersonas
        private=PrivatePersonas(self.paths)
        private.save_record({'id':'artificial-private-id','persona_id':self.pid})
        with patch.object(self.policy,'_stopped'),self.assertRaisesRegex(RuntimeErrorBase,'private_persona_acceptance_forbidden'):
            self.policy.enter(self.pid)
        self.assertEqual(self.cfg.load()['owner_qq'],'11111')

    async def test_bridge_suppresses_forbidden_peer_and_delivery(self):
        from persona_runtime.qq_bridge import QQBridge
        bridge=QQBridge(self.paths);self.acceptance();send=AsyncMock()
        with patch('persona_runtime.qq_bridge.allowed_message',return_value={'sender':'11111'}),patch.object(bridge,'request',new_callable=AsyncMock) as request,patch.object(bridge.journal,'remember') as journal:
            self.assertFalse(await bridge.handle(object(),send));journal.assert_not_called();request.assert_not_called();send.assert_not_called()
            with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_not_ready'):await bridge.deliver({},send)
            with self.assertRaisesRegex(RuntimeErrorBase,'qq_test_account_not_ready'):await bridge.deliver_stream({},send)
            request.assert_not_called();send.assert_not_called()


if __name__=='__main__':unittest.main()
