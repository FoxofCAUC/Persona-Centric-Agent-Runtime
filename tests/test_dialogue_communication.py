"""CR3-A isolated pipeline and closure checks; no configured provider is used."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import dialogue_communication_audit as seal


async def instant(*_):
    pass


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 100
        self.temp = tempfile.TemporaryDirectory(dir='D:/Persona-Centric Agent Runtime/tmp', prefix='cr3-runtime-test-')
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path('D:/Persona-Centric Agent Runtime/tmp').resolve()))
        self.engines = []

    async def asyncTearDown(self):
        from scripts.dialogue_refactor import close
        for engine in self.engines:
            with engine.store.connection() as db:
                active = db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
                self.assertEqual(active, 0)
                statuses = [dict(r) for r in db.execute("SELECT id,status,error_code FROM turns WHERE id NOT LIKE 'cr2-%'")]
            folder = os.environ.get('CR3_TEST_EVIDENCE_DIR')
            if folder:
                from persona_runtime.config import atomic_json
                dest = Path(folder).resolve()
                allowed = Path('D:/Persona-Centric Agent Runtime/exports/acceptance/PCAR-dialogue-refactor/CR3-A-communication-consumption').resolve()
                self.assertTrue(dest.is_relative_to(allowed))
                atomic_json(dest / (self._testMethodName + '-' + str(self.engines.index(engine)) + '.json'), {
                    'test': self.id(), 'simulation': True, 'real_calls': 0, 'turns': statuses,
                    'counts': engine.client.counts, 'ledger': engine.client.ledger.rows, 'active_reservations': active})
            close(engine)
        self.temp.cleanup()

    async def make(self, ident='G08', fault=None):
        from persona_runtime.archive import fingerprint
        from persona_runtime.character import CharacterService, empty_character
        from persona_runtime.chat import ChatEngine
        from persona_runtime.config import DEFAULTS
        from persona_runtime.dialogue_communication_pipeline import CommunicationPipeline
        from persona_runtime.models import generic_persona, identifier
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.service import RuntimeService
        from persona_runtime.storage import Store
        from scripts import dialogue_communication as tool
        from scripts import dialogue_context as base
        from tests.dialogue_communication_fixture import materials
        case = deepcopy(next(c for c in materials()['cases'] if c['id'] == ident))
        store = Store(RuntimePaths(self.root / ('runtime-' + str(len(self.engines))))); store.initialize()
        models = {k: deepcopy(DEFAULTS[k]) for k in ('model', 'character_selector')}
        for model in models.values():
            model.update(base_url='https://example.invalid/v1', name='grok-4.7', encrypted_key='')
        models['model'].update(max_tokens=8192, max_input_chars=24000, timeout=180, hourly_limit=0)
        models['character_selector'].update(enabled=True, timeout=240)
        cfg = base.configure(store.paths, models, case)
        service = RuntimeService(store)
        spec = replace(generic_persona(), expression_style=materials()['roles']['process']['style'])
        pid = service.create_persona(spec)['id']; service.use_persona(pid)
        chars = CharacterService(store); data = empty_character(); data['identity']['species'] = '人类'
        chars.update(pid, data, chars.current(pid)['id'], identifier())
        base.write_once(store.paths.home / 'execution-contract.json', tool.execution_contract())
        attachment = await base.admit(store, cfg, pid, case, 'process')
        attachment['communication_contract_hash'] = base.sha(store.paths.home / 'execution-contract.json')
        pool = {'payload': {'roots': {'lore': [], 'example': []}, 'groups': {}, 'sources': []},
                'ranked': {'lore': [], 'example': []}, 'rejected': [], 'removed': [], 'fingerprint': fingerprint({})}
        attachment['pool_hash'] = fingerprint(pool)
        base.write_once(store.paths.home / 'artificial-cr2.json', attachment)
        ledger = tool.Ledger(self.root / ('receipts-' + str(len(self.engines))))
        client = tool.MockClient(ident, ident, 'candidate', 'process', ledger, fault)
        pipeline = CommunicationPipeline(attachment, pool, ledger.audit)
        engine = ChatEngine(store, cfg, client, dialogue_pipeline=pipeline, generation_audit_sink=ledger.audit)
        self.engines.append(engine)
        return engine, attachment, pipeline, client, ledger

    async def test_direct_preserves_old_request_and_one_call(self):
        from persona_runtime.dialogue_pipeline import DialoguePipeline
        e, a, p, c, ledger = await self.make('G01')
        new = await p.prepare(e, a['turn_id'])
        old = await DialoguePipeline(a, json.loads(p.pool_json)).prepare(e, a['turn_id'])
        self.assertEqual(new['messages'], old['messages'])
        await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'completed')
        self.assertEqual(c.counts, {'main': 1})
        self.assertIsNone(p.record['communication_spec'])
        self.assertEqual(ledger.rows[0]['status'], 'completed')

    async def test_joint_actual_main_consumes_spec_not_entire_frame(self):
        from persona_runtime.archive import fingerprint
        e, a, p, c, ledger = await self.make()
        await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'completed')
        self.assertEqual(c.counts, {'joint': 1, 'main': 1})
        self.assertIn('每袋放6块', c.received[0]['messages'][1]['content'])
        actual = c.received[-1]['messages']
        self.assertIn('按已给每袋数量和袋数计算', actual[0]['content'])
        self.assertNotIn('饼干口味没有给出', actual[0]['content'])
        self.assertNotIn('当前交流情境（状态资料）', actual[0]['content'])
        self.assertEqual(fingerprint(actual), p.record['input_hash'])
        with e.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM situation_observation_attempts').fetchone()[0], 0)

    async def test_aux_success_main_retry_does_not_replay_aux(self):
        from persona_runtime.models import RuntimeErrorBase
        def fault(kind, count, messages):
            if kind == 'main' and count == 1:
                raise RuntimeErrorBase('provider_timeout')
        e, a, p, c, ledger = await self.make(fault=fault)
        with patch('persona_runtime.generation_retry.retry_delay', new=instant):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'completed')
        self.assertEqual(c.counts, {'joint': 1, 'main': 2})
        self.assertEqual([v['status'] for v in ledger.rows], ['completed', 'failed', 'completed'])
        self.assertEqual(ledger.rows[1]['request_hash'], ledger.rows[2]['request_hash'])

    async def test_three_retries_exhaust_then_no_main(self):
        from persona_runtime.models import RuntimeErrorBase
        def fault(*_):
            raise RuntimeErrorBase('provider_timeout')
        e, a, p, c, ledger = await self.make(fault=fault)
        with patch('persona_runtime.generation_retry.retry_delay', new=instant):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'failed')
        self.assertEqual(c.counts, {'joint': 4})
        self.assertEqual(len({v['request_hash'] for v in ledger.rows}), 1)
        self.assertTrue(all(v['status'] == 'failed' for v in ledger.rows))

    async def test_revision_change_rejected_before_dispatch(self):
        e, a, p, c, ledger = await self.make()
        with e.store.connection(write=True) as db:
            db.execute('UPDATE turns SET execution_revision=execution_revision+1 WHERE id=?', (a['turn_id'],))
        await e._run(a['turn_id'])
        self.assertEqual(c.counts, {})
        self.assertEqual(e.get(a['turn_id'])['status'], 'failed')

    async def test_withdrawn_foreword_after_aux_stops_main(self):
        from persona_runtime.models import LOCAL_USER_ID
        e, a, p, c, ledger = await self.make()
        def fault(kind, count, messages):
            if kind == 'joint':
                with e.store.connection(write=True) as db:
                    db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',
                        ('cr2-G08-process-h0-e1', a['persona_id'], LOCAL_USER_ID, 'test', '2026-11-03T00:00:00+00:00'))
        c.fault = fault
        await e._run(a['turn_id'])
        self.assertEqual(c.counts, {'joint': 1})
        self.assertEqual(e.get(a['turn_id'])['error_code'], 'cr2_source_changed')

    async def test_invalid_reference_rejected_before_dispatch(self):
        e, a, p, c, ledger = await self.make('G10')
        with e.store.connection(write=True) as db:
            db.execute("UPDATE events SET text='changed' WHERE id='cr2-G10-process-h0-e1'")
        await e._run(a['turn_id'])
        self.assertEqual(c.counts, {})
        self.assertEqual(e.get(a['turn_id'])['status'], 'failed')

    async def test_legal_reference_without_duplicate_history_completes_both_arms(self):
        from persona_runtime.dialogue_pipeline import DialoguePipeline, collect
        for arm in ('control', 'candidate'):
            with self.subTest(arm=arm):
                e, a, p, c, ledger = await self.make('G10')
                if arm == 'control':
                    p = DialoguePipeline(a, json.loads(p.pool_json), ledger.audit)
                    e.dialogue_pipeline = p
                    c.arm = 'control'
                evidence = collect(e, a['turn_id'], a)['shared'].document()
                self.assertEqual(evidence['references'], ['references:accepted'])
                self.assertFalse(any(s['kind'] == 'history' and s['authority'] == 'user'
                                     for s in evidence['sources']))
                reference = next(s for s in evidence['sources'] if s['kind'] == 'reference')
                self.assertIn('海报弄好后我想发到社团群里', reference['text'])
                await e._run(a['turn_id'])
                self.assertEqual(e.get(a['turn_id'])['status'], 'completed', e.get(a['turn_id']).get('error_code'))
                self.assertEqual(c.counts, {'joint': 1, 'main': 1})
                self.assertTrue(all(row['status'] == 'completed' for row in ledger.rows))
                self.assertEqual(p.record['shared']['references'], ['references:accepted'])
                quoted = [ref['quote'] for rows in p.record['understanding']['frame'].values()
                          for item in rows for ref in item['evidence']
                          if ref['source_id'] == reference['id']]
                self.assertTrue(quoted)
                self.assertTrue(all(reference['text'].count(q) == 1 for q in quoted))

    async def test_expired_state_invalidates_frame_and_spec(self):
        from persona_runtime.dialogue_pipeline import collect, current
        from persona_runtime.models import RuntimeErrorBase
        from persona_runtime.profile import append_version
        e, a, p, c, ledger = await self.make()
        with e.store.connection(write=True) as db:
            append_version(db, a['persona_id'], 'state', {'text': '临时专注', 'expires_at': '2099-01-01T00:00:00+00:00'})
        gathered = collect(e, a['turn_id'], a)
        with patch('persona_runtime.cognitive.active_state', return_value=False), e.store.connection() as db:
            with self.assertRaisesRegex(RuntimeErrorBase, 'source_changed'):
                p.recheck(e, db, current(e, a['turn_id'], a, db), gathered)
        self.assertEqual(c.counts, {})

    async def test_input_overflow_stops_before_aux(self):
        e, a, p, c, ledger = await self.make()
        with e.store.connection(write=True) as db:
            db.execute('UPDATE events SET text=? WHERE id=(SELECT user_event_id FROM turns WHERE id=?)',
                       ('必要输入' * 10000, a['turn_id']))
        await e._run(a['turn_id'])
        self.assertEqual(c.counts, {})
        self.assertEqual(e.get(a['turn_id'])['status'], 'failed')

    async def test_contract_change_and_unknown_version_never_dispatch(self):
        from persona_runtime.config import atomic_json
        from scripts import dialogue_communication as tool
        e, a, p, c, ledger = await self.make()
        value = tool.execution_contract(); value['conditions']['candidate'] = 'unknown'
        atomic_json(e.store.paths.home / 'execution-contract.json', value)
        await e._run(a['turn_id'])
        self.assertEqual(c.counts, {})
        self.assertEqual(e.get(a['turn_id'])['error_code'], 'communication_execution_changed')

    async def test_reserve_write_failure_prevents_generation(self):
        from scripts import dialogue_context as base
        e, a, p, c, ledger = await self.make()
        with patch.object(base, 'write_once', side_effect=OSError('injected reserve failure')):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'failed')
        self.assertEqual(ledger.rows, [])
        self.assertTrue(ledger.broken)

    async def test_terminal_write_failure_is_not_success(self):
        from scripts import dialogue_context as base
        e, a, p, c, ledger = await self.make('G01')
        original = base.write_once
        def writer(path, value):
            if '.terminal.' in Path(path).name:
                raise OSError('injected terminal failure')
            original(path, value)
        with patch.object(base, 'write_once', side_effect=writer):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'failed')
        self.assertTrue(ledger.broken)
        self.assertEqual(ledger.rows[0]['status'], 'reserved')

    async def test_cancel_has_interrupted_terminal(self):
        def fault(*_):
            raise asyncio.CancelledError()
        e, a, p, c, ledger = await self.make(fault=fault)
        with self.assertRaises(asyncio.CancelledError):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'interrupted')
        self.assertEqual(ledger.rows[0]['status'], 'interrupted')

    async def test_semantically_bad_valid_specs_are_not_quality_retried(self):
        from scripts import dialogue_communication as tool
        from tests.dialogue_communication_responses import legal_bad_joint
        for case_id, kind in [('G09', 'condition_tightening'), ('G12', 'gap_inflation'), ('G07', 'stiff_reaction')]:
            with self.subTest(kind=kind):
                e, a, p, c, ledger = await self.make(case_id)
                def bad(cid, evidence, role, version=2):
                    return legal_bad_joint(cid, evidence, role, version, kind=kind)
                with patch.object(tool, 'joint', side_effect=bad):
                    await e._run(a['turn_id'])
                self.assertEqual(e.get(a['turn_id'])['status'], 'completed')
                self.assertEqual(c.counts, {'joint': 1, 'main': 1})

    async def test_spec_fields_change_actual_main_input_and_removal_leaves_no_reaction(self):
        from scripts import dialogue_communication as tool
        from tests.dialogue_communication_responses import joint
        edits = [('G08', 'required', '本轮要求仅给饼干总数与单位。'),
                 ('G07', 'constraint', '当前意愿只聊散场教室，不列发言规则。'),
                 ('G07', 'reaction', '留意人声退去后空椅子之间的落差。')]
        for case_id, field, text in edits:
            with self.subTest(field=field):
                e, a, p, c, ledger = await self.make(case_id)
                def changed(cid, evidence, role, version=2):
                    value = joint(cid, evidence, role, version)
                    if field == 'required':value['frame']['requests'][0]['text'] = text
                    elif field == 'constraint':value['frame']['willingness'][0]['text'] = text
                    else:value['communication_spec']['optional_reactions'][0]['text'] = text
                    return value
                with patch.object(tool, 'joint', side_effect=changed):
                    await e._run(a['turn_id'])
                self.assertEqual(e.get(a['turn_id'])['status'], 'completed')
                self.assertIn(text, c.received[-1]['messages'][0]['content'])
        e, a, p, c, ledger = await self.make('G07')
        def removed(cid, evidence, role, version=2):
            value = joint(cid, evidence, role, version); value['communication_spec']['optional_reactions'] = []
            return value
        with patch.object(tool, 'joint', side_effect=removed):
            await e._run(a['turn_id'])
        self.assertEqual(e.get(a['turn_id'])['status'], 'completed')
        self.assertNotIn(edits[-1][2], c.received[-1]['messages'][0]['content'])

    async def test_success_position_is_not_replayed(self):
        e, a, p, c, ledger = await self.make('G01')
        await e._run(a['turn_id']); await e._run(a['turn_id'])
        self.assertEqual(c.counts, {'main': 1})


class OfflineTests(unittest.TestCase):
    def test_real_client_and_external_network_blocked(self):
        from scripts.dialogue_refactor import offline
        from scripts.example_calibration_r2 import offline as secondary, StopRun
        with offline() as primary, secondary() as second:
            with socket.socket() as sock, self.assertRaisesRegex(StopRun, 'external_network_forbidden'):
                sock.connect(('203.0.113.1', 443))
            async def call():
                from persona_runtime.chat import ChatCompletionsClient
                with self.assertRaisesRegex(StopRun, 'real_client_forbidden'):
                    await ChatCompletionsClient().complete({}, [])
            asyncio.run(call())
        self.assertEqual(second['external_connection_attempts'], 1)
        self.assertEqual(second['real_client_attempts'], 1)

    def test_cli_has_no_real_execute_action(self):
        import sys
        from scripts import dialogue_communication as tool
        with patch.object(sys, 'argv', ['dialogue_communication.py', 'execute']), self.assertRaises(SystemExit):
            tool.main()


class SealTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='D:/Persona-Centric Agent Runtime/tmp', prefix='cr3-seal-test-')
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path('D:/Persona-Centric Agent Runtime/tmp').resolve()))
        self.addCleanup(self.temp.cleanup)
        self.pack = self.root / 'pack'; self.source = self.root / 'source'
        self.pack.mkdir(); self.source.mkdir()
        (self.source / 'example.py').write_bytes(b'pass\n')
        self.source_hash = seal.digest(b'pass\n')
        self.write('offline-v1/verification.json', {'passed': True, 'main_requests': 48, 'mock_generations': 72, 'real_calls': 0})
        self.write('offline-v1/freeze.json', {'sources': {'example.py': self.source_hash}})
        with zipfile.ZipFile(self.pack / 'source.zip', 'w') as z:
            z.write(self.source / 'example.py', 'example.py')
        self.make_seal()
        self.patcher = patch.object(seal, 'ROOT', self.source)
        self.patcher.start(); self.addCleanup(self.patcher.stop)

    def write(self, name, value):
        p = self.pack / name; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding='utf-8')

    def make_seal(self):
        hashes = {p.relative_to(self.pack).as_posix(): seal.digest(p.read_bytes())
                  for p in self.pack.rglob('*') if p.is_file() and p.name != 'closure.json'}
        self.write('closure.json', {'stage': 'CR3-A', 'stage_complete': True,
            'source_archive': 'source.zip', 'source_hashes': {'example.py': self.source_hash},
            'evidence_hashes': hashes, 'real_model_calls': 0, 'main_requests': 48,
            'corrected_user_verdicts': {'win': 5, 'tie': 1, 'loss': 6}})

    def test_valid_archive_verification_is_read_only(self):
        before = {p: p.read_bytes() for p in self.pack.rglob('*') if p.is_file()}
        self.assertTrue(seal.verify(self.pack, worktree=True)['passed'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.pack.rglob('*') if p.is_file()})

    def test_modified_evidence_is_rejected(self):
        self.write('offline-v1/verification.json', {})
        with self.assertRaisesRegex(ValueError, 'sealed_evidence_changed'):
            seal.verify(self.pack)

    def test_extra_evidence_is_rejected(self):
        (self.pack / 'extra.txt').write_text('later addition', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'inventory_changed'):
            seal.verify(self.pack)

    def test_mock_cannot_be_marked_real(self):
        self.write('offline-v1/verification.json', {'passed': True, 'main_requests': 48, 'mock_generations': 72, 'real_calls': 1})
        self.make_seal()
        with self.assertRaisesRegex(ValueError, 'required_work_incomplete'):
            seal.verify(self.pack)

    def test_later_worktree_does_not_replace_archived_history(self):
        (self.source / 'example.py').write_bytes(b'changed\n')
        self.assertTrue(seal.verify(self.pack)['passed'])
        with self.assertRaisesRegex(ValueError, 'worktree_changed'):
            seal.verify(self.pack, worktree=True)

    def test_path_escape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside_package'):
            seal.child(self.pack, '../neighbor.txt')


if __name__ == '__main__':
    unittest.main()
