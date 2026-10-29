"""Real isolated Tk widgets; no formal runtime or paid provider."""
import unittest
from unittest.mock import patch
from tests import test_m72_desktop as base


class CharacterIntegrationDesktopTests(unittest.TestCase):
    setUp=base.ContextDesktopTests.setUp
    window=base.ContextDesktopTests.window
    doCleanups=base.ContextDesktopTests.doCleanups

    def test_widgets_default_and_policy_failure_keeps_draft(self):
        w=self.window();w.show_page('context');w.update()
        self.assertEqual(w.ctx_character.get(),'词法')
        self.assertTrue({'索引状态','重新准备'}<={b.cget('text') for b,_ in w.buttons})
        w.persona_id='a';w.ctx_policy={'id':'p','persona_id':'a','channel':'desktop','mode':'legacy_per_message'}
        w.ctx_character.set('语义增强');calls=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:calls.append(a)),patch.object(w.client,'manage',side_effect=OSError('failed')) as manage:
            w.save_context_policy()
            with self.assertRaises(OSError):calls[-1](None)
        self.assertEqual(w.ctx_character.get(),'语义增强')
        self.assertEqual(manage.call_args.kwargs['character_retrieval_mode'],'hybrid')

    def test_late_policy_read_does_not_overwrite_new_unsaved_choice(self):
        w=self.window();w.persona_id='a';pending=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:pending.append(success)):
            w.refresh_context_policy()
        w.ctx_character.set('语义增强')
        pending[-1]({'character_retrieval':{'mode':'lexical'},'context_mode':'working_set'})
        self.assertEqual(w.ctx_character.get(),'语义增强');self.assertIsNone(w.ctx_policy)

    def test_preview_and_index_late_scope_version_or_draft_is_rejected(self):
        w=self.window();w.persona_id='a';w.character_preview_input.insert('1.0','保留查询草稿');pending=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:pending.append((success,kw))),patch.object(w,'show_json') as show:
            w.preview_character();w.character_preview_channel.set('qq');pending[-1][0]({'private':'old'})
            show.assert_not_called();self.assertFalse(pending[-1][1]['error_current']())
            w.character_index('index-status');w.persona_id='b';pending[-1][0]({'private':'old'})
            show.assert_not_called()
        self.assertEqual(w.character_preview_input.get('1.0','end-1c'),'保留查询草稿')
