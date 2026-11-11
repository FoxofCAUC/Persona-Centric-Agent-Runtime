import unittest
from unittest.mock import patch
from persona_runtime.knowledge_routing import DEFAULT
from persona_runtime.knowledge_admin import overview
from tests import test_knowledge_routing as routing
from tests import test_knowledge_web as web
from tests import test_m4 as fixtures

class SelectedDefaultTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=routing.cleanup
    transport=web.WebTests.transport
    async def test_new_web_only_role_needs_no_ann_package_or_modules(self):
        self.assertEqual(DEFAULT['variant'],'web_only');self.assertFalse(DEFAULT['enabled'])
        web.WebTests.enable(self);routing.configure(self)
        policy=self.org.routing.policy(self.persona['id'])
        self.org.routing.update_policy(self.persona['id'],policy['data']|{'variant':DEFAULT['variant']},policy['revision'],'selected-default')
        self.chat.knowledge.maintenance=self.org.knowledge
        self.choice='web'
        with patch('persona_runtime.knowledge_processing.search_turn',side_effect=AssertionError('ANN was used')):
            result=await web.WebTests.ask(self,'公开验收灯塔几点开放？')
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(result['context']['knowledge']['routing']['choice'],'web')
        self.assertTrue(all(e['kind']=='web_document' for e in result['context']['knowledge']['evidence']))
        with patch('persona_runtime.knowledge_admin.importlib.util.find_spec',return_value=None):data=overview(self.chat.knowledge,self.persona['id'])
        self.assertIsNone(data['registry']['data']['active'])
        self.assertFalse(data['dependencies']['ready']);self.assertFalse(data['dependencies']['required_for_selected_route'])
    async def test_explicit_existing_local_variant_survives_default_change(self):
        routing.RoutingTests.enable(self)
        self.chat.knowledge.maintenance=self.org.knowledge
        self.assertEqual(self.org.routing.policy(self.persona['id'])['data']['variant'],'local_web')
        self.assertEqual(overview(self.chat.knowledge,self.persona['id'])['dependencies']['required_for_selected_route'],True)
