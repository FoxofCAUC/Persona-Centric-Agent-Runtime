"""Workbench upgrades to the accepted v8 path, using artificial policies only."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from persona_runtime.companion import choose, read, RESTORE_KEY
from persona_runtime.models import RuntimeErrorBase, identifier
from tests import test_companion_policy_restore as restore_fixture
from tests import test_companion_workbench_desktop as desktop_fixture
from tests.test_companion_workbench_desktop import policy


PREVIOUS_MODES = ('current', 'minimal', 'minimal_structural', 'minimal_chat',
                  'minimal_persona', 'minimal_context_rules_off',
                  'minimal_experience', 'minimal_native')


class Upgrades(unittest.IsolatedAsyncioTestCase):
    setUp = restore_fixture.CompanionPolicyRestore.setUp
    current = restore_fixture.CompanionPolicyRestore.current
    enable = restore_fixture.CompanionPolicyRestore.enable
    disable_args = restore_fixture.CompanionPolicyRestore.disable_args
    normalized = restore_fixture.CompanionPolicyRestore.normalized
    install_data = restore_fixture.CompanionPolicyRestore.install_data
    raw = restore_fixture.CompanionPolicyRestore.raw
    turn = restore_fixture.CompanionPolicyRestore.turn
    save = restore_fixture.CompanionPolicyRestore.save

    async def test_all_previous_profiles_keep_first_restore_and_accepted_requests(self):
        for mode in PREVIOUS_MODES:
            with self.subTest(mode=mode):
                original = self.current()
                previous = self.enable(companion_mode=mode,
                                       reply_mode='context_full' if mode == 'minimal_native' else 'natural_structured')
                point = json.loads(previous['data_json'])[RESTORE_KEY]
                queued = await self.turn('人工升级前输入 ' + mode)
                accepted = self.raw(queued['id'])
                upgraded = self.enable(companion_mode='minimal_im', reply_mode='context_full')
                self.assertEqual(upgraded['companion'], choose('minimal_im'))
                self.assertEqual(json.loads(upgraded['data_json'])[RESTORE_KEY], point)
                self.assertEqual(self.raw(queued['id']), accepted)
                self.assertEqual(read(accepted), choose(mode))
                repeated = self.enable(companion_mode='minimal_im', reply_mode='context_full')
                self.assertEqual(repeated['id'], upgraded['id'])
                restored = self.ingress.update_policy(**self.disable_args())
                self.assertEqual(self.normalized(restored), self.normalized(original))
        self.model.complete.assert_not_awaited()

    async def test_v8_upgrade_with_missing_or_invalid_restore_writes_nothing(self):
        self.enable(companion_mode='minimal_chat')
        original = json.loads(self.current()['data_json'])
        for point in (None, {'version': 'unknown'}):
            data = deepcopy(original)
            if point is None:
                data.pop(RESTORE_KEY)
            else:
                data[RESTORE_KEY] = point
            before = self.install_data(data)
            with self.assertRaisesRegex(RuntimeErrorBase, 'companion_restore_missing'):
                self.enable(companion_mode='minimal_im', reply_mode='context_full')
            self.assertEqual(self.current(), before)
        self.model.complete.assert_not_awaited()

    async def test_v8_switch_resolves_collecting_input_and_replays_only_operation(self):
        self.enable(companion_mode='minimal_chat')
        receipt = self.ingress.accept({'text': '人工收集中输入', 'request_id': identifier(), 'channel': 'desktop'})
        with self.assertRaisesRegex(RuntimeErrorBase, 'pending_batch_resolution_required'):
            self.enable(companion_mode='minimal_im', reply_mode='context_full')
        operation = identifier()
        previous_id = self.current()['id']
        result = self.enable(companion_mode='minimal_im', reply_mode='context_full',
                             pending_action='cancel', operation_id=operation)
        self.assertEqual(self.ingress.get(receipt['batch_id'])['state'], 'cancelled')
        replay = self.ingress.update_policy(self.pid, 'desktop', 'natural_batch',
            previous_id, operation, companion_mode='minimal_im', reply_mode='context_full',
            expression_policy_version=3, conversation_policy_version=2, pending_action='cancel')
        self.assertEqual(replay['id'], result['id'])
        self.model.complete.assert_not_awaited()


class WorkbenchVersions(unittest.TestCase):
    setUp = desktop_fixture.CompanionDesktopTests.setUp
    window = desktop_fixture.CompanionDesktopTests.window
    ready = desktop_fixture.CompanionDesktopTests.ready
    doCleanups = desktop_fixture.CompanionDesktopTests.doCleanups

    def test_version_matrix_and_upgrade_command_target_v8(self):
        window = self.ready()
        for mode in (*PREVIOUS_MODES, 'minimal_im'):
            with self.subTest(mode=mode):
                value = policy(True)
                value['companion'] = choose(mode)
                data = json.loads(value['data_json'])
                data['companion'] = value['companion']
                value['data_json'] = json.dumps(data)
                value['reply_mode'] = 'context_full' if mode in ('minimal_native', 'minimal_im') else 'natural_structured'
                window.ctx_policy = value
                window._context_companion_controls(value)
                enabled = mode != 'minimal_im'
                self.assertEqual(str(window.ctx_companion_enable.cget('state')), 'normal' if enabled else 'disabled')
                self.assertEqual(window.ctx_companion_enable.cget('text'), '升级伴侣聊天' if enabled else '已启用伴侣聊天')
                jobs = []
                with patch.object(window, 'run_task', side_effect=lambda n, r, action, success=None, **kw: jobs.append(action)), \
                        patch.object(window.client, 'update_companion_policy') as update:
                    window.change_companion_mode(True)
                    if enabled:
                        jobs[0](None)
                        self.assertEqual(update.call_args.kwargs['companion_mode'], 'minimal_im')
                        self.assertEqual(update.call_args.kwargs['reply_mode'], 'context_full')
                    else:
                        self.assertEqual(jobs, [])

    def test_invalid_profile_and_restore_are_visible_and_blocked(self):
        value = policy(True)
        value['companion'] = {'version': 'unknown', 'mode': 'minimal'}
        data = json.loads(value['data_json'])
        data['companion'] = value['companion']
        value['data_json'] = json.dumps(data)
        window = self.ready(value)
        self.assertEqual(str(window.ctx_companion_enable.cget('state')), 'disabled')
        self.assertIn('方案版本或启用前恢复点无效', window.ctx_companion.get())
        with patch.object(window, 'run_task') as run:
            window.change_companion_mode(True)
            run.assert_not_called()
