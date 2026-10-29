"""Scrollable workbench navigation with an isolated, offline Tk window."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tkinter import ttk
from tests import test_desktop as desktop_base


class NavigationTests(unittest.TestCase):
    setUp=desktop_base.DesktopTests.setUp
    window=desktop_base.DesktopTests.window

    def doCleanups(self):
        import gc
        for w in self.windows:
            w.hub.pool.shutdown(wait=True,cancel_futures=True);w.hub.drain()
        result=super().doCleanups();self.windows.clear();gc.collect()
        return result

    def test_minimum_window_can_reach_and_click_last_page(self):
        w=self.window();w.geometry('780x560');w.update()
        nav=w.navigation;last=w.navigation_buttons['diagnostics']
        self.assertLess(nav.canvas.yview()[1],1)
        nav.canvas.yview_moveto(1);w.update()
        self.assertGreaterEqual(last.winfo_rooty(),nav.canvas.winfo_rooty())
        self.assertLessEqual(last.winfo_rooty()+last.winfo_height(),nav.canvas.winfo_rooty()+nav.canvas.winfo_height())
        with patch.object(w,'read_logs'):
            last.invoke();self.assertEqual(w.current_page,'diagnostics')

    def test_wheel_over_button_only_scrolls_navigation(self):
        w=self.window();w.geometry('780x560');w.update()
        canvas=w.pages[w.current_page].canvas
        with patch.object(w,'winfo_containing',return_value=w.navigation_buttons['home']),patch.object(canvas,'yview_scroll') as body:
            self.assertEqual(w.scroll_page(SimpleNamespace(x_root=0,y_root=0,delta=-30)),'break')
            body.assert_not_called()
        self.assertGreater(w.navigation.canvas.yview()[0],0)
        nav_view=w.navigation.canvas.yview()
        with patch.object(w,'winfo_containing',return_value=w.pages['home'].body),patch.object(canvas,'yview_scroll') as body:
            w.scroll_page(SimpleNamespace(x_root=0,y_root=0,delta=-120));body.assert_called_once_with(1,'units')
        self.assertEqual(nav_view,w.navigation.canvas.yview())

    def test_future_buttons_remain_scrollable_after_resize(self):
        w=self.window();w.geometry('900x600')
        extra=[ttk.Button(w.navigation.body,text='人工未来入口 '+str(i)) for i in range(20)]
        for b in extra:b.pack(fill='x',pady=5)
        w.update();w.navigation.canvas.yview_moveto(1);w.update()
        bottom=w.navigation.canvas.winfo_rooty()+w.navigation.canvas.winfo_height()
        self.assertLessEqual(extra[-1].winfo_rooty()+extra[-1].winfo_height(),bottom)



if __name__=='__main__':unittest.main()
