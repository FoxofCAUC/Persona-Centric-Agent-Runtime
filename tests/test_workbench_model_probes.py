import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,AsyncMock,patch

from persona_runtime.models import RuntimeErrorBase,identifier
from persona_runtime.character_selector import probe_material
from persona_runtime.desktop_organization import OrganizationUI
from scripts.m763_provider_gate import DirectoryGate
from tests import test_m754r2 as selector_fixture,test_m4 as organization_fixture


class SelectorProbe(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_probe_allowed_but_real_upload_guard_preserved(self):
        c=selector_fixture.SelectorTests('test_probe_durable_no_turn_and_idempotent');c.setUp()
        try:
            c.enable()
            with patch('persona_runtime.private_personas.guard_cloud_dispatch',side_effect=RuntimeErrorBase('private_cloud_provider_not_authorized')) as guard:
                with patch.object(c.model,'complete',return_value=('{"schema_version":1,"lore_ids":["probe"],"example_ids":[]}',{})) as call:
                    op=identifier();result=await c.engine.selector.probe(c.pid,op)
                    await c.engine.selector.probe(c.pid,op)
                    self.assertIsNone(result['error']);self.assertEqual(call.call_count,1);guard.assert_not_called()
                turn=await c.turn('书店');model=c.config.load()['character_selector']
                pool,payload=probe_material(model)
                with c.store.connection(write=True) as db,self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):
                    c.engine.selector._start(db,c.pid,turn,model,payload,pool)
                with c.store.connection(write=True) as db,self.assertRaisesRegex(RuntimeErrorBase,'selector_probe_material_invalid'):
                    c.engine.selector._start(db,c.pid,None,model,payload+[{'role':'user','content':'private'}],pool)
        finally:c.doCleanups()


class OrganizationProbe(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_reused_and_final_result_has_usage(self):
        c=organization_fixture.M4Tests();c.setUp()
        try:
            op=identifier();first=c.org.test_model(op)
            self.assertEqual(first,c.org.test_model(op));self.assertEqual(first['status'],'queued')
            await c.org.run_next();shown=c.org.show(first['id'])
            self.assertEqual(shown['status'],'completed');self.assertEqual(len(shown['attempts']),1)
            self.assertEqual(c.org.test_model(op)['id'],first['id'])
            await c.org.run_next();self.assertEqual(len(c.model.calls),1)
            self.assertNotIn('encrypted_key',json.dumps(shown))
        finally:
            await c.asyncTearDown();c.chat.cognitive.shadow.close();c.chat.cognitive.characters.close();c.doCleanups()


class OrganizationDisplay(unittest.TestCase):
    def ui(self):
        u=SimpleNamespace(persona_id='artificial',view_epoch=1,closed=False,notice=Mock(),show_json=Mock(),refresh_quotas=Mock(),after=Mock(),run_task=Mock(),client=Mock())
        u.org_model_test_pending={'key':('artificial',1),'request_id':'stable'}
        u.watch_organization_test=lambda value,key:OrganizationUI.watch_organization_test(u,value,key)
        return u

    def test_queued_is_polled_and_not_presented_as_final(self):
        u=self.ui();u.watch_organization_test({'id':'task','status':'queued'},('artificial',1))
        u.show_json.assert_not_called();u.after.call_args.args[1]()
        u.run_task.call_args.args[2](None)
        u.client.manage.assert_called_once_with('organization.show',task_id='task')
        u.watch_organization_test({'id':'task','status':'completed','attempts':[{'usage_json':'{}'}]},('artificial',1))
        self.assertIsNone(u.org_model_test_pending);u.show_json.assert_called_once()

    def test_late_response_and_closed_window_are_ignored(self):
        for values in ({'view_epoch':2},{'closed':True},{'persona_id':'other'}):
            u=self.ui()
            for k,v in values.items():setattr(u,k,v)
            u.watch_organization_test({'id':'task','status':'completed'},('artificial',1))
            u.show_json.assert_not_called();u.after.assert_not_called()

    def test_quota_wait_is_visible_without_endless_poll_or_resubmit(self):
        u=self.ui();u.watch_organization_test({'id':'task','status':'waiting_quota'},('artificial',1))
        u.after.assert_not_called();u.client.manage.assert_not_called()
        self.assertEqual(u.org_model_test_pending['task_id'],'task')


class CatalogDedup(unittest.IsolatedAsyncioTestCase):
    async def test_same_connection_has_one_get_with_two_bound_results(self):
        gate=object.__new__(DirectoryGate)
        model={'name':'deepseek-flash','base_url':'https://example.invalid/v1','encrypted_key':'one'}
        gate.approved={'model':model,'character_selector':model|{'timeout':60,'encrypted_key':'two'}}
        gate.identities={'model':'main-fingerprint','character_selector':'auxiliary-fingerprint'}
        gate._check=AsyncMock(return_value={'kind':'main','normal':True,'model_fingerprint':'main-fingerprint','checked_epoch':1})
        with patch('scripts.m763_provider_gate.unprotect',return_value='same-secret'):
            result=await gate.check_all()
        gate._check.assert_awaited_once_with('main','model')
        self.assertEqual(result[1]['model_fingerprint'],'auxiliary-fingerprint')
        self.assertEqual(result[1]['reused_from_kind'],'main')
        self.assertNotIn('same-secret',json.dumps(result))

    async def test_distinct_credentials_never_share_proof(self):
        gate=object.__new__(DirectoryGate)
        model={'name':'deepseek-flash','base_url':'https://example.invalid/v1','encrypted_key':'one'}
        gate.approved={'model':model,'character_selector':model|{'encrypted_key':'two'}}
        gate.identities={'model':'main','character_selector':'aux'}
        gate._check=AsyncMock(side_effect=[{'kind':'main','normal':True},{'kind':'auxiliary','normal':False}])
        with patch('scripts.m763_provider_gate.unprotect',side_effect=lambda x:x):result=await gate.check_all()
        self.assertEqual(gate._check.await_count,2);self.assertFalse(result[1]['normal'])
