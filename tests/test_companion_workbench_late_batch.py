"""Policy-read ordering without Tk, a service, or generation."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from persona_runtime.companion import choose
from persona_runtime.desktop_batches import BatchUI


class LateBatchReadTests(unittest.TestCase):
    def test_older_same_target_policy_cannot_replace_enable_or_restore_result(self):
        old={'persona_id':'artificial','channel':'desktop','id':'old','revision':1,
             'mode':'legacy_per_message','reply_mode':'legacy_full'}
        enabled=old|{'id':'enabled','revision':2,'mode':'natural_batch',
                     'reply_mode':'natural_structured','companion':choose('minimal_structural')}
        restored=old|{'id':'restored','revision':3}
        for current in (enabled,restored):
            with self.subTest(version=current['id']):
                pending=[]
                window=SimpleNamespace(persona_id='artificial',view_epoch=1,
                    client=Mock(),batch_policies={'desktop':deepcopy(old)},
                    companion_policies={'desktop':deepcopy(old)},batch_rows=[],chat_batches=[],
                    batch_policy_text=Mock(),reply_policy_text=Mock(),
                    batch_tree=Mock(),session_id='session',current_page='batches',
                    remember_companion_policy=Mock(),show_batch_members=Mock(),
                    run_task=lambda name,resources,action,success=None,**kwargs:pending.append(success))
                window.batch_tree.selection.return_value=()
                window.batch_tree.get_children.return_value=()
                BatchUI.refresh_batches(window)
                # Enabling/restoring finishes while the earlier read is delayed.
                window.companion_policies={'desktop':deepcopy(current)}
                window.batch_policies={'desktop':deepcopy(current)}
                saved=deepcopy(window.batch_policies)
                pending[0](([],{'desktop':old}))
                self.assertEqual(window.batch_policies,saved)
                window.batch_policy_text.set.assert_not_called()
                window.reply_policy_text.set.assert_not_called()
                window.remember_companion_policy.assert_not_called()
                # A fresh same-version result is still shown normally.
                pending[0](([],{'desktop':current}))
                window.batch_policy_text.set.assert_called_once()
                window.reply_policy_text.set.assert_called_once()
                self.assertEqual(window.batch_policies,{'desktop':current})
                # The existing changed-persona boundary remains earlier still.
                window.persona_id='another';window.view_epoch+=1
                pending[0](([],{'desktop':old}))
                self.assertEqual(window.batch_policies,{'desktop':current})
                self.assertEqual(window.batch_policy_text.set.call_count,1)


if __name__=='__main__':unittest.main()
