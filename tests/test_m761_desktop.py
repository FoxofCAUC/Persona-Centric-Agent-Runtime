"""Native widgets with controlled actions; no live service or model."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from tests import test_m72_desktop as base


class Desktop(unittest.TestCase):
    setUp=base.ContextDesktopTests.setUp
    window=base.ContextDesktopTests.window
    def doCleanups(self):
        # Artificial unsaved drafts are already asserted; never leave a modal
        # blocking test cleanup or schedule polls into a closed worker pool.
        for w in self.windows:
            w.dispo_dirty=False
            for timer in w.tk.splitlist(w.tk.call('after','info')):w.tk.call('after','cancel',timer)
        return base.ContextDesktopTests.doCleanups(self)

    def test_panel_present_and_stale_channel_response_rejected(self):
        w=self.window();w.show_page('character');w.update()
        labels={b.cget('text') for b,_ in w.buttons}
        self.assertTrue({'读取倾向与来源','编译所选来源','集中确认','预览Shadow（零模型）'}<=labels)
        callbacks=[];w.persona_id='A'
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append((success,kw))):w.refresh_dispositions()
        w.dispo_channel.set('qq')
        callbacks[-1][0]({'invalid':'stale'})
        self.assertIsNone(w.dispo_state);self.assertFalse(callbacks[-1][1]['error_current']())

    def test_failed_save_keeps_draft_and_reuses_operation(self):
        w=self.window();w.persona_id='A';w.dispo_pid='A';w.dispo_state={'revision':2}
        w.dispo_document={'schema_version':1,'source_versions':{},'definitions':[]};w.dispo_dirty=True
        actions=[];calls=[]
        def manage(action,**args):calls.append(args);raise OSError('lost response')
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:actions.append(a)),patch.object(w.client,'manage',side_effect=manage):
            for _ in range(2):
                w.save_disposition_draft()
                with self.assertRaises(OSError):actions[-1](None)
        self.assertTrue(w.dispo_dirty);self.assertEqual(calls[0],calls[1])
        self.assertEqual(w.dispo_document,{'schema_version':1,'source_versions':{},'definitions':[]})

    def test_refresh_requires_explicit_draft_discard(self):
        w=self.window();w.dispo_dirty=True
        with patch('persona_runtime.desktop_disposition.messagebox.askyesno',return_value=False),patch.object(w,'run_task') as run:
            w.refresh_dispositions();run.assert_not_called()

    def test_preview_late_query_does_not_replace_new_draft(self):
        w=self.window();w.dispo_pid='A';w.persona_id='A';w.dispo_state={'revision':1};w.dispo_preview_text.insert('1.0','first')
        callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append(success)),patch.object(w,'show_json') as show:
            w.preview_disposition();w.dispo_preview_text.insert('end',' next');callbacks[0]({'record':'stale'})
            show.assert_not_called()
        self.assertEqual(w.dispo_preview_text.get('1.0','end-1c'),'first next')

    def test_unsaved_projection_prevents_switch_and_source_read_revalidates(self):
        w=self.window();w.dispo_dirty=True
        with patch('persona_runtime.desktop_character.messagebox.askyesno',return_value=False):self.assertFalse(w.character_can_leave())
        self.assertTrue(w.dispo_dirty)
        w.dispo_pid='A';w.dispo_sources=[{'source_id':'source','version_id':'version','content_hash':'hash','text':'old'}]
        w.dispo_source_list.insert('end','source');w.dispo_source_list.selection_set(0);actions=[]
        with patch.object(w,'_dispo_run',side_effect=lambda name,work,done:actions.append(work)),patch.object(w.client,'manage',return_value={'sources':[]}):
            w.disposition_source()
            with self.assertRaisesRegex(ValueError,'不可读取'):actions[0]()
        w.dispo_dirty=False

    def test_editor_footer_and_open_draft_close_protection(self):
        import tkinter as tk
        from persona_runtime.disposition_core.contracts import ACTIONS
        w=self.window();w.dispo_pid='A';w.persona_id='A'
        w.dispo_document={'source_versions':{},'definitions':[{'id':'d','label':'人工倾向','priority':1,'enabled':True,'source_refs':[],'preferences':{a:0 for a in ACTIONS},'triggers':[]}]}
        w._render_disposition_defs();w.dispo_definition_list.selection_set(0);w.edit_disposition()
        dialog=w.character_dialogs[-1];dialog.geometry('500x320');w.update()
        def controls(widget):
            for child in widget.winfo_children():
                yield child
                yield from controls(child)
        buttons=[b for b in controls(dialog) if b.winfo_class()=='TButton' and b.cget('text')=='保留到本地草案']
        self.assertEqual(len(buttons),1)
        self.assertLessEqual(buttons[0].winfo_rooty()+buttons[0].winfo_height(),dialog.winfo_rooty()+dialog.winfo_height())
        entry=next(b for b in controls(dialog) if b.winfo_class()=='TEntry');entry.insert('end','修改')
        self.assertTrue(dialog.character_dirty())
        with patch('persona_runtime.desktop_disposition.messagebox.askyesno',return_value=False):self.assertFalse(w.character_can_leave())
        dialog.destroy()
