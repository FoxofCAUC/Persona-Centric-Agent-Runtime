"""Native workbench rendering and explicit same-mode expression upgrades."""
import gc
import unittest
from unittest.mock import patch, Mock
from tests import test_desktop as base


class BubbleDesktopTests(unittest.TestCase):
    setUp=base.DesktopTests.setUp
    window=base.DesktopTests.window

    def doCleanups(self):
        for w in self.windows:w.hub.pool.shutdown(wait=True,cancel_futures=True);w.hub.drain()
        result=unittest.TestCase.doCleanups(self);self.windows.clear();gc.collect();return result

    def test_six_bubbles_keep_commas_long_body_and_draft(self):
        w=self.window();w.chat_input.insert('1.0','未发送草稿')
        texts=['收到','等雨小一点，再走','你到家了吗？','版本v1.2.3','他说“慢慢来。”','长'*80]
        w.turns=[{'status':'completed','text':'通用输入','usage':None,'reply':'原语义不可改',
                  'display_messages':[{'text':t,'diagnostics':{'quality_findings':['long_bubble_review'] if len(t)>50 else []}} for t in texts]}]
        w.render_chat();w.update()
        shown=w.transcript.get('1.0','end')
        self.assertEqual(shown.count('人格：'),6)
        for text in texts:self.assertIn('人格：'+text+'\n\n',shown)
        self.assertIn('第6条',shown)
        self.assertEqual(w.chat_input.get('1.0','end-1c'),'未发送草稿')
        self.assertEqual(w.turns[0]['reply'],'原语义不可改')

    def test_same_natural_mode_can_upgrade_v2_without_changing_buffer(self):
        w=self.window();w.persona_id='a'
        w.batch_policies={'qq':{'persona_id':'a','id':'old','mode':'natural_batch',
            'reply_mode':'natural_structured','expression_policy':{'version':2}}}
        w.change_reply_mode('qq')
        dialog=next(c for c in w.winfo_children() if c.winfo_class()=='Toplevel')
        actions=[]
        with patch.object(w,'run_task',side_effect=lambda name,res,action,*a,**kw:actions.append(action)):
            next(c for c in base.DesktopTests.widgets(dialog) if c.winfo_class()=='TButton' and c.cget('text')=='保存').invoke()
        self.assertEqual(len(actions),1)
        with patch.object(w.client,'manage',return_value={}) as manage:
            actions[0](Mock())
        self.assertEqual(manage.call_args.args[0],'reply.update')
        self.assertEqual(manage.call_args.kwargs['reply_mode'],'natural_structured')
        self.assertEqual(manage.call_args.kwargs['expected_version'],'old')
