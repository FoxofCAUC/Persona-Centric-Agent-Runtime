"""Isolated Tk rendering; no launcher, credentials, model or platform calls."""
import unittest
import gc
from tests import test_desktop as fixture


class SearchNoticeDesktop(unittest.TestCase):
    setUp=fixture.DesktopTests.setUp
    window=fixture.DesktopTests.window

    def doCleanups(self):
        for window in self.windows:
            window.hub.pool.shutdown(wait=True,cancel_futures=True)
            window.hub.drain()
        result=super().doCleanups()
        self.windows.clear();gc.collect()
        return result

    @staticmethod
    def turn():
        return {'id':'public-ui-turn','text':'帮我查查怎么炒土豆丝。','status':'queued','reply':None,
            'error_code':None,'cancelled_at':None,'phase':'knowledge_search_web','queue_position':1,
            'search_notice':{'text':'我先觉得别切太粗，等我去看看大家怎么做。'},'timings':{},'context':None}

    def test_pending_and_cancelled_turn_keep_the_real_persona_utterance_visible(self):
        window=self.window();turn=self.turn();window.turns=[turn]
        window.render_chat();window.update()
        text=window.transcript.get('1.0','end')
        self.assertIn('人格：'+turn['search_notice']['text'],text)
        self.assertIn('正在搜索公开资料',text);self.assertNotIn('主回复用量',text)
        turn.update(status='interrupted',phase='cancelled',cancelled_at='2026-11-15T00:00:00Z')
        window.render_chat()
        self.assertEqual(window.transcript.get('1.0','end').count(turn['search_notice']['text']),1)

    def test_completed_turn_displays_both_messages_without_merging_or_rewriting_them(self):
        window=self.window();turn=self.turn()
        turn.update(status='completed',reply='查到的做法里，先洗掉表面淀粉比较稳妥。',usage={'total_tokens':1})
        window.turns=[turn];window.render_chat();window.update()
        text=window.transcript.get('1.0','end')
        self.assertEqual(text.count(turn['search_notice']['text']),1)
        self.assertEqual(text.count(turn['reply']),1)
        self.assertLess(text.index(turn['search_notice']['text']),text.index(turn['reply']))


if __name__=='__main__':unittest.main()
