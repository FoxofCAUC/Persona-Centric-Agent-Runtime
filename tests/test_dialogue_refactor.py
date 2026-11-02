"""CR0 contract, source lifecycle, consumption and durable simulation checks."""
import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.communication_spec import CommunicativeSpec
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_frame import DialogueEvidence, DialogueFrame, Statement, consumer_input
from scripts import dialogue_refactor as cr0
from tests.dialogue_refactor_fixture import fixture


class Contracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=cr0.HOME / 'tmp', prefix='cr0-test-')
        self.root = Path(self.temp.name)
        self.case = fixture()['cases'][3]
        with cr0.offline():
            self.engine, self.tid = await cr0.create_case(self.root / 'runtime', self.case, 'process')
            self.ev, self.prepared, self.aliases, self.model, self.old = cr0.collect(self.engine, self.tid)
        self.frame, self.spec = cr0.annotate(self.case, 'process', self.ev, self.aliases)
        self.record = {'id': 'F04-process', 'case_id': 'F04', 'runtime': str(self.root / 'runtime'), 'turn_id': self.tid,
                       'evidence': self.ev.document(), 'frame': self.frame.document(), 'spec': self.spec.document(),
                       'legacy_messages': self.prepared['messages'], 'legacy_manifest': self.prepared['manifest'],
                       'legacy_inputs': self.old, 'model': self.model}

    async def asyncTearDown(self):
        cr0.close(self.engine)
        self.temp.cleanup()

    def test_round_trip_and_immutability(self):
        self.assertEqual(DialogueEvidence.load(self.ev.document()), self.ev)
        self.assertEqual(DialogueFrame.load(self.frame.document()), self.frame)
        self.assertEqual(CommunicativeSpec.load(self.spec.document()), self.spec)
        with self.assertRaises(FrozenInstanceError):
            self.ev.turn_id = 'changed'
        body = self.ev.document(); body['current'][0]['text'] = 'changed'
        self.assertNotEqual(DialogueEvidence.load(body).digest, self.ev.digest)

    def test_ordinary_history_reaches_all_consumers(self):
        self.assertNotIn('84厘米', encoded(self.old['observation']))
        self.assertNotIn('84厘米', encoded(self.old['selection']))
        for name in ('selection', 'observation', 'expression'):
            payload = consumer_input(self.ev, self.frame, name)
            self.assertIn('84厘米', encoded(payload))
            self.assertIn('已在前文提供', encoded(payload))
        self.assertEqual([s.role for s in self.ev.history], ['user', 'assistant'])

    def test_willingness_and_missing_slot_changes_are_consumed(self):
        changed = replace(self.frame, willingness=(Statement('现在不要建议', (self.aliases['C0'],)),),
                          missing_slots=(Statement('仍缺纸张厚度', (), 'unknown'),))
        for name in ('selection', 'observation', 'expression'):
            self.assertNotEqual(consumer_input(self.ev, self.frame, name), consumer_input(self.ev, changed, name))
        with self.assertRaisesRegex(ValueError, 'communication_spec_stale'):
            self.spec.validate(self.ev, changed)

    def test_old_input_and_spec_removal(self):
        base = deepcopy(self.prepared['messages'])
        self.assertEqual(cr0.project(base, self.ev)[0], base)
        with_spec, _ = cr0.project(base, self.ev, self.frame, self.spec)
        no_spec, _ = cr0.project(base, self.ev, self.frame)
        self.assertNotIn('communication_spec', no_spec[0]['content'])
        self.assertIn('communication_spec', with_spec[0]['content'])
        self.assertEqual(with_spec[1:], base[1:])
        self.assertEqual(self.prepared['messages'], base)

    def test_unknown_versions_rejected(self):
        for obj in (self.ev, self.frame, self.spec):
            with self.subTest(type=type(obj).__name__), self.assertRaisesRegex(ValueError, 'dialogue_version_unknown'):
                replace(obj, version='unknown')

    def test_cross_role_refs_and_parent_hash_rejected(self):
        bad = replace(self.frame, requests=(Statement('假的请求', ('current:other-persona',)),))
        with self.assertRaisesRegex(ValueError, 'dialogue_source_out_of_scope'):
            bad.validate(self.ev)
        with self.assertRaisesRegex(ValueError, 'dialogue_frame_stale'):
            self.frame.validate(replace(self.ev, scope_json=encoded(json.loads(self.ev.scope_json) | {'persona_id': 'other'})))
        with self.assertRaisesRegex(ValueError, 'dialogue_projection_out_of_scope'):
            replace(self.spec, disposition_refs=('other-projection',)).validate(self.ev, self.frame)

    def test_optional_imagination_needs_no_source_but_obligations_do(self):
        replace(self.spec, optional_moves=('想象手上粘了木屑的小动作',)).validate(self.ev, self.frame)
        replace(self.frame, uncertainty=(Statement('也许想歇一会儿', (), 'interpretation'),)).validate(self.ev)
        with self.assertRaisesRegex(ValueError, 'dialogue_evidence_required'):
            Statement('把未说明的细节写成明确事实')

    async def test_creator_only_catalog_is_not_in_shared_input(self):
        from persona_runtime.character import CharacterService
        original = CharacterService.update
        canary = '人工隐藏正典CANARY-不应进入共享输入'
        def update(service, pid, data, expected, operation):
            data = deepcopy(data)
            data['lorebook'] = [{'id': 'hidden', 'title': '人工隐藏设定', 'category': 'background',
                                 'keywords': ['木条'], 'text': canary, 'priority': 1, 'enabled': True,
                                 'visibility': 'creator_only', 'dependencies': []}]
            return original(service, pid, data, expected, operation)
        with patch.object(CharacterService, 'update', update):
            engine, tid = await cr0.create_case(self.root / 'hidden', self.case, 'process')
        try:
            ev, prepared, aliases, *_ = cr0.collect(engine, tid)
            self.assertIn(canary, cr0.raw_turn(engine, tid)['character_snapshot'])
            frame, spec = cr0.annotate(self.case, 'process', ev, aliases)
            for consumer in ('selection', 'observation', 'expression'):
                self.assertNotIn(canary, encoded(consumer_input(ev, frame, consumer)))
            self.assertNotIn(canary, encoded(cr0.project(prepared['messages'], ev, frame, spec)))
        finally:
            cr0.close(engine)

    def test_source_withdrawal_invalidates_old_semantics(self):
        with self.engine.store.connection(write=True) as db:
            from tests.m71_fixtures import insert
            from persona_runtime.models import LOCAL_USER_ID
            insert(db, 'source_exclusions', {'event_id': 'cr0-history-0-e1', 'persona_id': json.loads(self.ev.scope_json)['persona_id'],
                                            'user_id': LOCAL_USER_ID, 'reason': 'artificial withdrawal', 'created_at': '2026-11-02'})
        fresh, *_ = cr0.collect(self.engine, self.tid)
        with self.assertRaisesRegex(ValueError, 'dialogue_evidence_stale'):
            self.ev.check_current(fresh)
        with self.assertRaisesRegex(ValueError, 'dialogue_frame_stale'):
            self.frame.validate(fresh)

    def test_expired_state_and_execution_revision_invalidate(self):
        from persona_runtime.profile import append_version
        with self.engine.store.connection(write=True) as db:
            append_version(db, json.loads(self.ev.scope_json)['persona_id'], 'state',
                           {'text': '人工已过期', 'expires_at': '2000-01-01T00:00:00+00:00'})
        fresh, *_ = cr0.collect(self.engine, self.tid)
        self.assertFalse(any(s.kind == 'state' for s in fresh.sources))
        with self.assertRaises(ValueError):
            self.ev.check_current(fresh)
        with self.engine.store.connection(write=True) as db:
            db.execute('UPDATE turns SET execution_revision=execution_revision+1 WHERE id=?', (self.tid,))
        revised, *_ = cr0.collect(self.engine, self.tid)
        self.assertNotEqual(revised.execution_revision, fresh.execution_revision)
        with self.assertRaises(ValueError):
            fresh.check_current(revised)

    async def test_reference_failure_blocks_before_simulation(self):
        engine, tid = await cr0.create_case(self.root / 'reference', fixture()['cases'][4], 'process')
        try:
            evidence, *_ = cr0.collect(engine, tid)
            self.assertTrue(evidence.references)
            from tests.m71_fixtures import insert
            from persona_runtime.models import LOCAL_USER_ID
            with engine.store.connection(write=True) as db:
                insert(db, 'source_exclusions', {'event_id': 'cr0-history-0-e1', 'persona_id': cr0.raw_turn(engine, tid)['persona_id'],
                                                'user_id': LOCAL_USER_ID, 'reason': 'artificial', 'created_at': '2026-11-02'})
            with self.assertRaisesRegex(ValueError, 'reference_'):
                cr0.collect(engine, tid)
        finally:
            cr0.close(engine)

    async def test_same_words_different_history_requires_reannotation(self):
        case = deepcopy(fixture()['cases'][2])
        a, tid = await cr0.create_case(self.root / 'words', case, 'process')
        try:
            old, *_ = cr0.collect(a, tid)
            case['history'] = [('正好在我生日当天收到朋友手做的信封。', '这份心意来得正是时候。')]
            b, bid = await cr0.create_case(self.root / 'words-alt', case, 'process')
            try:
                new, *_ = cr0.collect(b, bid)
                self.assertEqual(old.current[0].text, new.current[0].text)
                self.assertNotEqual([s.text for s in old.history], [s.text for s in new.history])
                with self.assertRaises(ValueError):
                    cr0.annotate(fixture()['cases'][2], 'process', old, cr0.collect(a, tid)[2])[0].validate(new)
            finally:
                cr0.close(b)
        finally:
            cr0.close(a)

    def test_three_paths_protocol_and_parameters(self):
        client = cr0.SimulatedClient()
        params = []
        for path in cr0.PATHS:
            result = cr0.run_position(self.root, self.record, path, client)
            self.assertEqual(result['input_hash'], client.received[-1]['input_hash'])
            payload, _ = cr0.build_request(self.model, result['messages'])
            params.append({k: v for k, v in payload.items() if k != 'messages'})
        self.assertEqual(params, [params[0]] * 3)
        self.assertEqual(len(client.received), 3)
        with self.assertRaisesRegex(ValueError, 'already_reserved'):
            cr0.run_position(self.root, self.record, 'legacy', client)
        self.assertEqual(len(client.received), 3)

    def test_cancelled_and_reservation_failure_never_dispatch(self):
        client = cr0.SimulatedClient()
        with self.assertRaisesRegex(ValueError, 'cr0_cancelled'):
            cr0.run_position(self.root, self.record, 'legacy', client, cancelled=True)
        self.assertEqual(cr0.read(self.root / 'receipts/F04-process-legacy.json')['status'], 'cancelled')
        def fail(*args):
            raise OSError('injected write failure')
        with self.assertRaises(OSError):
            cr0.run_position(self.root, self.record, 'frame', client, writer=fail)
        self.assertEqual(client.received, [])

    def test_result_and_terminal_write_failures_are_not_success(self):
        for terminal in (False, True):
            root = self.root / ('terminal' if terminal else 'result')
            calls = []
            def writer(path, value):
                calls.append(path)
                if len(calls) >= 3 if terminal else len(calls) == 2:
                    raise OSError('injected')
                atomic_json(path, value)
            client = cr0.SimulatedClient()
            with self.assertRaises(OSError):
                cr0.run_position(root, self.record, 'legacy', client, writer=writer)
            receipt = cr0.read(root / 'receipts/F04-process-legacy.json')
            self.assertEqual(receipt['status'], 'reserved' if terminal else 'failed')
            with self.assertRaisesRegex(ValueError, 'already_reserved'):
                cr0.run_position(root, self.record, 'legacy', client)
            self.assertEqual(len(client.received), 1)

    def test_input_budget_is_checked_before_dispatch(self):
        record = deepcopy(self.record)
        spec = replace(self.spec, optional_moves=('人工很长' * 20000,))
        record['spec'] = spec.document()
        client = cr0.SimulatedClient()
        with self.assertRaisesRegex(ValueError, 'cr0_input_budget'):
            cr0.run_position(self.root, record, 'spec', client)
        self.assertEqual(client.received, [])

    def test_report_preserves_unterminated_reservation_as_incomplete(self):
        atomic_json(self.root / 'requests/F04-process.json', self.record)
        atomic_json(self.root / 'freeze.json', {'tool': cr0.VERSION, 'sources': cr0.sources(), 'requests': ['F04-process'],
                                              'files': {'requests/F04-process.json': cr0.sha(self.root / 'requests/F04-process.json')}})
        calls = []
        def writer(path, value):
            calls.append(path)
            if len(calls) >= 3:
                raise OSError('injected terminal failure')
            atomic_json(path, value)
        with self.assertRaises(OSError):
            cr0.run_position(self.root, self.record, 'legacy', cr0.SimulatedClient(), writer=writer)
        result = cr0.report(self.root)
        self.assertEqual(result['completed'], 0)
        self.assertEqual(result['rows'][0]['status'], 'reserved')
        self.assertTrue(result['rows'][0]['result_without_completed_receipt'])
        with self.assertRaisesRegex(ValueError, 'cr0_simulation_incomplete'):
            cr0.verify(self.root)

    def test_report_canonicalizes_persisted_tuple_arrays_and_rejects_extra_blocks(self):
        atomic_json(self.root / 'requests/F04-process.json', self.record)
        atomic_json(self.root / 'freeze.json', {'tool': cr0.VERSION, 'sources': cr0.sources(), 'requests': ['F04-process'],
                                              'files': {'requests/F04-process.json': cr0.sha(self.root / 'requests/F04-process.json')}})
        for path in cr0.PATHS:
            cr0.run_position(self.root, self.record, path, cr0.SimulatedClient())
        self.assertEqual(cr0.report(self.root)['completed'], 3)
        path = self.root / 'results/F04-process-spec.json'
        damaged = cr0.read(path)
        damaged['messages'][0]['content'] += 'unregistered extra block'
        atomic_json(path, damaged)
        with self.assertRaisesRegex(ValueError, 'cr0_unexplained_message_diff'):
            cr0.report(self.root)


class OfflineAndPrivacy(unittest.TestCase):
    def test_external_socket_and_http_are_blocked(self):
        with cr0.offline() as audit:
            with socket.socket() as connection, self.assertRaisesRegex(RuntimeError, 'network_forbidden'):
                connection.connect(('203.0.113.8', 443))
            async def request():
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    with self.assertRaisesRegex(RuntimeError, 'http_forbidden'):
                        await session.get('http://127.0.0.1:1')
            asyncio.run(request())
        self.assertEqual(audit['external_attempts'], 1)
        self.assertEqual(audit['http_attempts'], 1)

    def test_cli_has_no_run_action(self):
        with patch.object(sys := __import__('sys'), 'argv', ['dialogue_refactor.py', 'run']), self.assertRaises(SystemExit) as stop:
            cr0.main()
        self.assertEqual(stop.exception.code, 2)

    def test_synthetic_private_lifecycle_lock_boundary_and_complete_cleanup(self):
        from persona_runtime.private_personas import PrivatePersonas
        from persona_runtime.character import empty_character
        from persona_runtime.config import ConfigStore
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.models import generic_persona
        from persona_runtime.instance import InstanceLock
        with tempfile.TemporaryDirectory(dir=cr0.HOME / 'tmp', prefix='cr0-cleanup-') as temp:
            home = Path(temp); paths = RuntimePaths(home)
            ConfigStore(paths).initialize()
            source = home / 'artificial.json'; archive = home / 'artificial.md'
            atomic_json(source, {'format': 'pcar_character_v1', 'persona_spec': generic_persona().to_dict(), 'character': empty_character()})
            archive.write_text('人工隐藏稿，不进入副本。', encoding='utf-8')
            private = PrivatePersonas(paths)
            record = private.register(source, archive, '人工初始关系')
            original = cr0.sha(archive)
            copy = private.create_sandbox(record['id']); folder = Path(copy['home'])
            sibling = folder.parent / 'sibling'; sibling.mkdir(); (sibling / 'keep').write_text('keep')
            for name in ('inputs.json', 'replies.json', 'cache/index.bin', 'screenshots/one.png', 'exports/review.json', 'backups/db.zip'):
                p = folder / name;p.parent.mkdir(parents=True, exist_ok=True);p.write_text('人工验证正文', encoding='utf-8')
            self.assertFalse((folder / 'source-archive.md').exists())
            with self.assertRaises(ValueError):
                private.cleanup_sandbox(home)
            lock = InstanceLock(folder / 'runtime/service.lock')
            try:
                with self.assertRaises(ValueError):
                    private.cleanup_sandbox(folder)
                self.assertTrue(folder.exists())
            finally:
                lock.close()
            self.assertTrue(private.cleanup_sandbox(folder)['local_copy_deleted'])
            self.assertFalse(folder.exists())
            self.assertTrue((sibling / 'keep').exists())
            self.assertEqual(cr0.sha(archive), original)

    def test_reading_anchors_are_not_a_numeric_effect_claim(self):
        rules = fixture()['reading']
        self.assertEqual(len(rules['examples']), 6)
        self.assertIsNone(rules['thresholds'])
        self.assertEqual(rules['effect_status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
