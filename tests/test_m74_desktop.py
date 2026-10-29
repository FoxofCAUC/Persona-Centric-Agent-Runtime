"""Native controls, failed saves and stale persona/target responses."""
import gc
import unittest
from unittest.mock import patch
from tests import test_desktop as base


class NaturalDesktopTests(unittest.TestCase):
    setUp=base.DesktopTests.setUp
    window=base.DesktopTests.window
    def doCleanups(self):
        for w in self.windows:w.hub.pool.shutdown(wait=True,cancel_futures=True);w.hub.drain()
        result=unittest.TestCase.doCleanups(self);self.windows.clear();gc.collect();return result

    def test_fourth_mode_and_concrete_preview_cancel_controls(self):
        from persona_runtime.desktop_batches import REPLY_LABELS
        self.assertIn('natural_structured',REPLY_LABELS)
        w=self.window();w.show_page('delivery');w.update()
        labels={b.cget('text') for b,_ in w.buttons}
        self.assertTrue({'表达与节奏预览','取消本批可省略余段'}<=labels)

    def test_failed_policy_save_keeps_dialog_and_draft(self):
        w=self.window();w.persona_id='a';w.batch_policies={'qq':{'persona_id':'a','id':'v1','reply_mode':'legacy_full'}}
        w.chat_input.insert('1.0','保留草稿');w.change_reply_mode('qq')
        dialog=next(c for c in w.winfo_children() if c.winfo_class()=='Toplevel')
        widgets=list(base.DesktopTests.widgets(dialog))
        next(c for c in widgets if c.winfo_class()=='TRadiobutton' and c.cget('value')=='natural_structured').invoke()
        callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append(kw['failure'])):
            next(c for c in widgets if c.winfo_class()=='TButton' and c.cget('text')=='保存').invoke()
        callbacks[0](RuntimeError('conflict'))
        self.assertTrue(dialog.winfo_exists());self.assertEqual(w.chat_input.get('1.0','end-1c'),'保留草稿')

    def test_preview_late_persona_response_ignored_and_draft_retained(self):
        w=self.window();w.persona_id='a';w.preview_reply()
        dialog=next(c for c in w.winfo_children() if c.winfo_class()=='Toplevel')
        widgets=list(base.DesktopTests.widgets(dialog));texts=[c for c in widgets if c.winfo_class()=='Text']
        texts[0].insert('1.0','人工草稿');callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append(success)):
            next(c for c in widgets if c.winfo_class()=='TButton' and c.cget('text')=='预览填写内容').invoke()
        w.persona_id='b';w.view_epoch+=1;callbacks[0]({'secret':'old-target'})
        self.assertNotIn('old-target',texts[1].get('1.0','end'));self.assertEqual(texts[0].get('1.0','end-1c'),'人工草稿')

    def test_preview_late_target_response_ignored(self):
        w=self.window();w.persona_id='a';selected={'id':'first','reply_batch_id':'batch-a'}
        with patch.object(w,'selected_delivery_part',side_effect=lambda: selected):
            w.preview_reply()
            dialog=next(c for c in w.winfo_children() if c.winfo_class()=='Toplevel')
            widgets=list(base.DesktopTests.widgets(dialog));callbacks=[]
            with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append(success)):
                next(c for c in widgets if c.winfo_class()=='TButton' and c.cget('text')=='预览选中批次').invoke()
            selected={'id':'second','reply_batch_id':'batch-b'};callbacks[0]({'private':'old-target'})
            texts=[c for c in widgets if c.winfo_class()=='Text']
            self.assertNotIn('old-target',texts[1].get('1.0','end'))
