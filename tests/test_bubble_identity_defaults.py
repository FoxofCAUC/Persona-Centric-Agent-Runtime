"""Identity entry points use the same new expression default; synthetic data only."""
import unittest
from tests.test_persona_management import ManagementTests
from persona_runtime.character import CharacterService
from persona_runtime.ingress import IngressService
from persona_runtime.models import identifier
from persona_runtime.chat import ChatEngine


class BubbleIdentityDefaultsTests(unittest.TestCase):
    setUp = ManagementTests.setUp
    clone = ManagementTests.clone

    def test_create_import_clone_preserve_source_and_default_to_v3(self):
        character = CharacterService(self.store)
        source = self.service.show_persona(self.a)['spec']
        package = {'format': 'pcar_character_v1', 'persona_spec': source,
                   'character': character.current(self.a)['data']}
        imported = character.import_package(self.a, package, identifier())['id']
        cloned = self.clone()['id']
        ingress = IngressService(ChatEngine(self.store, self.cfg))
        for pid in (self.a, imported, cloned):
            self.assertEqual(self.service.show_persona(pid)['spec'], source)
            for channel in ('desktop', 'qq'):
                policy = ingress.current_policy(pid, channel)
                self.assertEqual(policy['reply_mode'], 'natural_structured')
                self.assertEqual(policy['expression_policy']['version'], 3)
                self.assertEqual(policy['expression_policy']['presentation'], 'model-bubbles-v1')
                self.assertEqual(policy['mode'], 'natural_batch')
