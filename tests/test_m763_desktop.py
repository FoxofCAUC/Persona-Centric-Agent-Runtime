"""Native author mapping UI, draft and stale-scope checks; isolated configuration."""
import unittest
from unittest.mock import patch
from tests import test_m761_desktop as base
from persona_runtime.archive import fingerprint


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class Desktop(unittest.TestCase):
    setUp=base.Desktop.setUp
    window=base.Desktop.window
    doCleanups=base.Desktop.doCleanups

    def test_mapping_selection_is_draft_until_author_confirm(self):
        w=self.window();w.show_page('character');w.update();w.persona_id=w.dispo_pid='artificial'
        text='作者设定：亲近的朋友'
        w.dispo_sources=[{'kind':'relationship','source_id':'source','version_id':'version','path':'description',
                         'entry_id':'','content_hash':fingerprint(text),'text':text}]
        w.dispo_document={'schema_version':1,'definitions':[],'source_versions':{}}
        with patch.object(w.client,'manage') as manage:
            w.edit_relationship_mapping();dialog=w.character_dialogs[-1];w.update()
            widgets=list(descendants(dialog));combo=next(x for x in widgets if x.winfo_class()=='TCombobox')
            combo.set('close');editor=next(x for x in widgets if x.winfo_class()=='Text')
            editor.tag_add('sel','1.0','1.end')
            next(x for x in widgets if x.winfo_class()=='TButton' and x.cget('text')=='保留到本地草案').invoke()
            self.assertTrue(w.dispo_dirty);self.assertEqual(w.dispo_document['relationship_mapping']['value'],'close')
            self.assertEqual(w.dispo_document['relationship_mapping']['source_ref']['fragment_hash'],fingerprint(text))
            manage.assert_not_called()

    def test_stale_mapping_dialog_preserves_original_draft(self):
        w=self.window();w.persona_id=w.dispo_pid='A';w.dispo_document={'schema_version':1,'definitions':[],'source_versions':{}}
        w.edit_relationship_mapping();dialog=w.character_dialogs[-1];w.persona_id='B'
        with patch('persona_runtime.desktop_disposition.messagebox.showwarning') as warning:
            next(x for x in descendants(dialog) if x.winfo_class()=='TButton' and x.cget('text')=='保留到本地草案').invoke()
            warning.assert_called_once()
        self.assertEqual(w.dispo_document['schema_version'],1);dialog.destroy()

    def test_shadow_understanding_off_by_default_and_preview_read_only(self):
        w=self.window();self.assertEqual(w.dispo_observation.get(),'disabled')
        w.persona_id=w.dispo_pid='A';w.dispo_state={'revision':1};w.dispo_preview_text.insert('1.0','给我建议')
        calls=[]
        with patch.object(w,'_dispo_run',side_effect=lambda name,work,done:calls.append(work)),patch.object(w.client,'manage',return_value={}) as manage:
            w.preview_disposition();calls[0]()
            self.assertEqual(manage.call_args.args[0],'disposition.preview')
            self.assertNotIn('observation',manage.call_args.kwargs)
