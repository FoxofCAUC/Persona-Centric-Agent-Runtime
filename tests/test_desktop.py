"""Tk layout and responsive-task checks, without model calls or QQ startup."""
import gc
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from persona_runtime.desktop import Workbench
from persona_runtime.paths import RuntimePaths
from persona_runtime.lifecycle import ServiceController
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.models import generic_persona
from persona_runtime.profile import ProfileService


class DesktopTests(unittest.TestCase):
    @staticmethod
    def widgets(parent):
        for child in parent.winfo_children():
            yield child
            yield from DesktopTests.widgets(child)

    def setUp(self):
        self.windows = []
        base = RuntimePaths.from_environment().home / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="desktop-test-", dir=base)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))

    def test_text_dialog_save_stays_visible_in_small_window(self):
        window = self.window()
        with patch.object(window, "poll"), patch.object(window, "refresh_personas"), patch.object(window, "read_logs"):
            window.text_dialog("布局回归测试", "", Mock())
            dialog = next(child for child in window.winfo_children() if child.winfo_class() == "Toplevel")
            dialog.geometry("420x240")
            window.update()
            save = next(child for child in self.widgets(dialog)
                        if child.winfo_class() == "TButton" and child.cget("text") == "保存")
            self.assertTrue(save.winfo_ismapped(), "Save must not be pushed out by the editor")
            self.assertLessEqual(save.winfo_rooty()+save.winfo_height(), dialog.winfo_rooty()+dialog.winfo_height())

    def test_delivery_selection_detail_survives_refresh_and_stale_persona_response(self):
        window=self.window();window.persona_id='persona-a';window.view_epoch=1
        part={'id':'part-a','state':'uncertain','semantic_index':1,'part_index':1,'attempt':1,
              'platform_message_id':None,'text_hash':'hash','text':'test delivery detail','error':'timeout','attempts':[]}
        stream={'id':'stream-a','persona_id':'persona-a','channel':'qq','state':'blocked',
                'blocked_reason':'uncertain','parts':[part]}
        callbacks=[]
        with patch.object(window,'run_task',side_effect=lambda *a,**kw: callbacks.append(a[3])):
            window.refresh_deliveries()
        callbacks[0]([stream]);window.delivery_tree.selection_set('stream-a');window.show_delivery_parts()
        window.delivery_part_tree.selection_set('part-a');window.show_delivery_detail()
        self.assertIn('test delivery detail',window.delivery_detail.get('1.0','end'))
        window.show_delivery_parts()
        self.assertEqual(window.selected_delivery_part()['id'],'part-a')
        actions=[]
        with patch.object(window,'run_task',side_effect=lambda *a,**kw:actions.append(a[2])), patch.object(window.client,'call',return_value={}) as call:
            window._delivery_action(dict(stream,revision=3),part,'retry',True)
            actions[0](None)
            self.assertEqual(call.call_args.args[2]['args']['action'],'retry')
        window.view_epoch=2;window.persona_id='persona-b'
        callbacks[0]([{'invalid':'stale'}])
        self.assertEqual(window.delivery_rows,[stream])

    def test_send_ack_does_not_erase_next_typed_message(self):
        window = self.window()
        window.persona_id = "test-persona"
        window.session_id = "test-session"
        window.chat_input.insert("1.0", "第一条")
        callbacks = []
        def capture(name, resources, action, success=None, **kwargs):
            callbacks.append(success)
            return object()
        with patch.object(window, "run_task", side_effect=capture), patch.object(window, "load_chat"), patch.object(window, "watch_turn"):
            window.send_chat()
            window.chat_input.delete("1.0", "end")
            window.chat_input.insert("1.0", "正在写的第二条")
            callbacks[0]({"id": "ack", "session_id": "test-session"})
        self.assertEqual(window.chat_input.get("1.0", "end-1c"), "正在写的第二条")

    def test_fast_refresh_is_not_a_service_identity_scan(self):
        window = self.window()
        window.current_page = "chat"
        window.session_id = "test-session"
        window.chat_version = 10
        callbacks = []
        actions = []
        def capture(name, resources, action, success=None, **kwargs):
            actions.append(action)
            callbacks.append(success)
            return object()
        with patch.object(window, "run_task", side_effect=capture), patch.object(window.client, "manage", return_value={"version": 10, "turns": None}) as manage, patch.object(window.controller, "snapshot") as snapshot:
            window.refresh_conversation()
            callbacks[0](actions[0](None))
            manage.assert_called_once_with("chat.snapshot", session_id="test-session", version=10)
            snapshot.assert_not_called()

    def test_interaction_controls_keep_new_behaviors_opt_in(self):
        window = self.window()
        self.assertFalse(window.allow_queue_cancel.get())
        self.assertFalse(window.qq_wait_notice.get())
        self.assertTrue(any(w.winfo_class() == "TButton" and w.cget("text") == "保存私聊设置" for w in self.widgets(window)))

    def test_m4_daily_schedule_and_review_controls_are_accessible(self):
        window = self.window()
        self.assertIn('organization',window.pages)
        self.assertTrue(window.daily_enabled.get())
        self.assertFalse(window.org_reuse.get())
        labels={w.cget('text') for w in self.widgets(window) if w.winfo_class()=='TButton'}
        for label in ('立即执行一次全量更新','继续受阻的每日更新','编辑后确认新记忆','保存整理配置','预览整理范围'):
            self.assertIn(label,labels)

    def test_m4_failed_candidate_save_keeps_text_and_target_version(self):
        window=self.window()
        window.org_records['candidates']=[{'id':'candidate','status':'pending'}]
        window.org_lists['candidates'].insert('end','待审候选')
        window.org_lists['candidates'].selection_set(0)
        candidate={'id':'candidate','text':'虚构待审偏好'}
        with patch.object(window,'run_task') as run, patch.object(window,'text_dialog') as editor:
            window.review_candidate()
            callback=run.call_args.kwargs.get('success') or run.call_args.args[3]
            callback(candidate)
            self.assertEqual(editor.call_args.args[1],candidate['text'])
            self.assertEqual(editor.call_args.kwargs['save_label'],'确认生效')

    def test_m4_preview_response_does_not_replace_new_selection(self):
        window=self.window()
        window.org_session_rows=[{'id':'session'}]
        window.org_session_box['values']=['session']
        window.org_session_box.current(0)
        with patch.object(window,'run_task') as run:
            window.preview_organization()
            callback=run.call_args.args[3]
            window.org_mode.set('仅候选')
            callback({'rows':[1]})
        self.assertIsNone(window.org_preview)

    def window(self):
        window = Workbench(self.paths)
        self.windows.append(window)
        for method in ("poll", "refresh_personas", "read_logs"):
            self.enterContext(patch.object(window, method))
        self.addCleanup(lambda: window.close_window() if not window.closed else None)
        original_scale = float(window.tk.call("tk", "scaling"))
        self.addCleanup(lambda: window.tk.call("tk", "scaling", original_scale) if not window.closed else None)
        return window

    def doCleanups(self):
        # Tk objects must be collected on their creating thread, not by the next
        # test's worker triggering cyclic GC after an earlier interpreter closes.
        for window in self.windows:
            window.hub.pool.shutdown(wait=True, cancel_futures=True)
            window.hub.drain()
        result = super().doCleanups()
        self.windows.clear()
        if "window" in locals():
            del window
        gc.collect()
        return result

    def pump(self, window, predicate, timeout=4):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            window.update()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Timed out waiting for the isolated desktop action")

    def dialog(self, window, title):
        return next((widget for widget in window.winfo_children()
                     if isinstance(widget, tk.Toplevel) and widget.title() == title), None)

    def control(self, dialog, label):
        return next(widget for widget in self.widgets(dialog)
                    if widget.winfo_class() == "TButton" and widget.cget("text") == label)

    def editor(self, dialog):
        return next(widget for widget in self.widgets(dialog) if isinstance(widget, tk.Text))

    def test_edit_actions_visible_at_four_scales_and_minimum_size(self):
        window = self.window()
        from persona_runtime.models import CATEGORIES
        for scale in (1, 1.25, 1.5, 2):
            window.tk.call("tk", "scaling", 96/72*scale)
            for title, categories, label in (("人工笔记", None, "保存"), ("关系说明", None, "保存"),
                    ("修订记忆", None, "保存"), ("显式录入记忆", CATEGORIES, "保存记忆")):
                with self.subTest(scale=scale, dialog=title):
                    dialog = window.text_dialog(title, "", Mock(), categories=categories, save_label=label)
                    dialog.geometry("420x240")
                    window.update()
                    for text in (label, "取消"):
                        button = self.control(dialog, text)
                        self.assertTrue(button.winfo_ismapped())
                        self.assertGreaterEqual(button.winfo_rootx(), dialog.winfo_rootx())
                        self.assertLessEqual(button.winfo_rootx()+button.winfo_width(), dialog.winfo_rootx()+dialog.winfo_width())
                        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), dialog.winfo_rooty()+dialog.winfo_height())
                    self.assertGreater(self.editor(dialog).winfo_height(), 15)
                    self.assertTrue(dialog.bind("<Control-Return>"))
                    dialog.destroy()

    def test_failed_save_keeps_draft_and_busy_rejection_does_not_close(self):
        window = self.window()
        calls = []
        def action(text):
            calls.append(text)
            if len(calls) == 1:
                raise ValueError("simulated save failure")
            return {"saved": True}
        dialog = window.text_dialog("失败保存回归", "", action)
        editor = self.editor(dialog)
        editor.insert("1.0", "不会丢失的虚构笔记🙂")
        button = self.control(dialog, "保存")
        button.invoke()
        self.pump(window, lambda: len(calls) == 1 and not window.hub.active)
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(editor.get("1.0", "end-1c"), "不会丢失的虚构笔记🙂")
        self.assertNotIn("disabled", button.state())
        with patch.object(window, "run_task", return_value=None):
            button.invoke()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(len(calls), 1)
        button.invoke()
        self.pump(window, lambda: not dialog.winfo_exists())
        self.assertEqual(calls, ["不会丢失的虚构笔记🙂"]*2)

    def test_empty_input_and_discard_confirmation(self):
        window = self.window()
        action = Mock()
        dialog = window.text_dialog("关闭回归", "", action)
        self.control(dialog, "保存").invoke()
        action.assert_not_called()
        self.assertTrue(dialog.winfo_exists())
        self.editor(dialog).insert("1.0", "未保存")
        with patch("persona_runtime.desktop.messagebox.askyesno", return_value=False):
            self.control(dialog, "取消").invoke()
            self.assertTrue(dialog.winfo_exists())
        with patch("persona_runtime.desktop.messagebox.askyesno", return_value=True):
            window.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
        self.assertFalse(dialog.winfo_exists())
        action.assert_not_called()

    def test_note_add_memory_revision_and_relationship_persist_through_dialogs(self):
        store = Store(self.paths)
        store.initialize()
        service = RuntimeService(store)
        persona = service.create_persona(generic_persona())
        service.use_persona(persona["id"])
        session = service.create_session()
        window = self.window()
        handlers = {"session.list": service.list_sessions, "session.show": service.show_session,
                    "event.append": service.append_event, "memory.add": service.add_memory,
                    "memory.search": service.search_memories, "memory.revise": service.revise_memory,
                    "relationship.show": service.show_relationship, "relationship.set": service.set_relationship}
        profile=ProfileService(store)
        handlers.update({'profile.current':profile.current,'profile.update':profile.update,'relationship.proposals':profile.proposals,'persona.show':service.show_persona})
        window.client.manage = lambda name, **args: handlers[name](**args)

        chosen = []
        window.source_dialog(chosen.append)
        self.pump(window, lambda: self.dialog(window, "选择来源事件") is not None and not window.hub.active)
        source = self.dialog(window, "选择来源事件")
        for scale in (1, 1.25, 1.5, 2):
            window.tk.call("tk", "scaling", 96/72*scale)
            source.geometry("480x260")
            window.update()
            for label in ("选择事件", "在此会话添加人工笔记", "关闭"):
                button = self.control(source, label)
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(), source.winfo_rooty()+source.winfo_height())
        self.control(source, "在此会话添加人工笔记").invoke()
        note_title = "人工笔记（外部知识请注明出处与获知时间）"
        note = self.dialog(window, note_title)
        self.editor(note).insert("1.0", "M2虚构笔记：喜欢温梨水。")
        self.control(note, "保存").invoke()
        self.pump(window, lambda: not note.winfo_exists() and not window.hub.active)
        events = service.show_session(session["id"])["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["role"], "note")
        self.assertIn("温梨水", events[0]["text"])
        tree = next(widget for widget in self.widgets(source) if widget.winfo_class() == "Treeview")
        self.assertIn(events[0]["id"], tree.get_children())
        tree.selection_set(events[0]["id"])
        self.control(source, "选择事件").invoke()
        self.assertEqual(chosen, [events[0]["id"]])

        def choose_source():
            self.pump(window, lambda: self.dialog(window, "选择来源事件") is not None and not window.hub.active)
            source = self.dialog(window, "选择来源事件")
            tree = next(widget for widget in self.widgets(source) if widget.winfo_class() == "Treeview")
            tree.selection_set(events[0]["id"])
            self.control(source, "选择事件").invoke()

        window.add_memory()
        choose_source()
        memory_dialog = self.dialog(window, "显式录入记忆")
        self.editor(memory_dialog).insert("1.0", "M2虚构记忆：喜欢温梨水。")
        self.control(memory_dialog, "保存记忆").invoke()
        self.pump(window, lambda: not memory_dialog.winfo_exists() and not window.hub.active)
        memories = service.search_memories("M2虚构")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["source_event_id"], events[0]["id"])
        window.memories.selection_set(memories[0]["id"])
        window.revise_memory()
        choose_source()
        revision = self.dialog(window, "修订记忆（产生新版本）")
        self.editor(revision).delete("1.0", "end")
        self.editor(revision).insert("1.0", "M2虚构记忆：更正为常温柚子水。")
        self.control(revision, "保存").invoke()
        self.pump(window, lambda: not revision.winfo_exists() and not window.hub.active)
        current = service.search_memories("M2虚构")[0]
        self.assertEqual(current["revision"], 2)
        self.assertEqual(len(service.show_memory(current["id"])["history"]), 2)

        window.edit_relationship()
        self.pump(window, lambda: self.dialog(window, "关系说明") is not None and not window.hub.active)
        relationship = self.dialog(window, "关系说明")
        self.editor(relationship).insert("1.0", "虚构验收关系")
        self.control(relationship, "保存").invoke()
        self.pump(window, lambda: not relationship.winfo_exists() and not window.hub.active)
        self.assertEqual(RuntimeService(Store(self.paths)).show_relationship()["description"], "虚构验收关系")

    def test_six_pages_at_three_scales_and_responsive_callbacks(self):
        window = self.window()
        # No OS process scans or HTTP during component rendering checks.
        with patch.object(window, "poll"), patch.object(window, "refresh_personas"), patch.object(window, "read_logs"), patch.object(window,'refresh_organization'),patch.object(window,'refresh_profile'):
            for scale in (1, 1.25, 1.5):
                window.tk.call("tk", "scaling", 96/72*scale)
                window.geometry("1000x700")
                for key in window.pages:
                    window.show_page(key)
                    window.update()
                    window.update_idletasks()
                    self.assertGreater(window.pages[key].body.winfo_width(), 500)
                    for button, _ in window.buttons:
                        parent = button.master
                        if getattr(parent, "flow_buttons", False) and button.winfo_ismapped():
                            self.assertLessEqual(button.winfo_x()+button.winfo_width(), parent.winfo_width()+2,
                                                 f"{scale}: {key}: {button.cget('text')}")
            marker = []
            window.run_task("slow simulated action", {"slow"}, lambda _: time.sleep(.4))
            window.after(50, lambda: marker.append(True))
            deadline = time.monotonic()+.2
            while time.monotonic() < deadline:
                window.update()
                time.sleep(.005)
            self.assertTrue(marker, "A slow background operation must not block Tk callbacks")

    def test_m5_core_editor_actions_visible_and_failed_save_keeps_content(self):
        window=self.window()
        version={'id':'core-v1','data':generic_persona().to_dict()}
        version['data'].pop('format_version');version['data'].pop('expression_style')
        dialog=window.core_editor(version)
        for scale in (1,1.25,1.5):
            window.tk.call('tk','scaling',96/72*scale);dialog.geometry('480x300');window.update()
            save=self.control(dialog,'保存核心设定')
            self.assertTrue(save.winfo_ismapped())
            self.assertLessEqual(save.winfo_rooty()+save.winfo_height(),dialog.winfo_rooty()+dialog.winfo_height())
        editor=dialog.core_editors['name'];editor.delete('1.0','end');editor.insert('1.0','保留的虚构名字')
        with patch.object(window.client,'manage',side_effect=ValueError('profile_version_conflict')):
            self.control(dialog,'保存核心设定').invoke()
            self.pump(window,lambda:not window.hub.active)
        self.assertTrue(dialog.winfo_exists());self.assertEqual(editor.get('1.0','end-1c'),'保留的虚构名字')
        self.assertEqual(str(editor.cget('state')),'normal')

    def test_m5_page_has_explicit_state_and_version_actions(self):
        window=self.window();self.assertIn('profile',window.pages)
        self.assertEqual(window.state_hours.get(),'24')
        labels={w.cget('text') for w in self.widgets(window) if w.winfo_class()=='TButton'}
        for label in ('编辑当前人格核心','与当前版本比较','恢复选中版本','设置临时状态','从来源创建关系建议','编辑并确认关系建议'):
            self.assertIn(label,labels)

    def test_external_service_and_stale_pid_are_never_stopped(self):
        from persona_runtime.config import ConfigStore
        ConfigStore(self.paths).initialize()
        controller = ServiceController(self.paths)
        with patch.object(controller, "owned", return_value=False), patch.object(controller, "health", return_value={"state": "外部程序占用"}):
            with self.assertRaises(ValueError):
                controller.stop("service")
        saved = {"instance_id": controller.config.load()["instance_id"], "host": {"ProcessId": 123, "Created": "old", "ExecutablePath": "python.exe"}}
        with patch.object(controller, "record", return_value=saved), patch("persona_runtime.lifecycle.process_info", return_value={"ProcessId": 123, "Created": "new", "ExecutablePath": "python.exe"}):
            self.assertFalse(controller.owned("service"))

    def test_dashboard_opens_with_stale_bridge_heartbeat_but_checks_listener(self):
        from persona_runtime.config import ConfigStore
        ConfigStore(self.paths).initialize()
        controller = ServiceController(self.paths)
        identity = {"ProcessId": 123, "Created": "current", "ExecutablePath": "python.exe"}
        with patch.object(controller, "owned", return_value=True), \
             patch.object(controller, "record", return_value={"host": identity}), \
             patch.object(controller, "health", return_value={"state": "等待验证", "owned": True}) as health, \
             patch("persona_runtime.lifecycle.listener", return_value=identity) as listening:
            self.assertEqual(controller.management_url("astrbot"), "http://127.0.0.1:6285")
            health.assert_not_called()
            listening.return_value = identity | {"Created": "reused-pid"}
            with self.assertRaisesRegex(ValueError, "其他进程"):
                controller.management_url("astrbot")
            listening.return_value = None
            with self.assertRaisesRegex(ValueError, "尚未监听"):
                controller.management_url("astrbot")
        with patch.object(controller, "owned", return_value=False), \
             patch("persona_runtime.lifecycle.listener") as listening:
            with self.assertRaisesRegex(ValueError, "属于本项目"):
                controller.management_url("astrbot")
            listening.assert_not_called()

    def test_management_button_reports_browser_failure_and_runs_in_worker(self):
        window = Mock()
        window.controller.management_url.return_value = "http://127.0.0.1:6285"
        Workbench.open_panel(window, "astrbot")
        action = window.run_task.call_args.args[2]
        with patch("persona_runtime.desktop.webbrowser.open", return_value=True) as browser:
            action(None)
            browser.assert_called_once_with("http://127.0.0.1:6285")
        with patch("persona_runtime.desktop.webbrowser.open", return_value=False):
            with self.assertRaisesRegex(ValueError, "未接受"):
                action(None)
        with patch("persona_runtime.desktop.webbrowser.open", side_effect=OSError("sensitive detail")):
            with self.assertRaisesRegex(ValueError, "无法启动默认浏览器") as error:
                action(None)
            self.assertNotIn("sensitive", str(error.exception))

    def test_m6_character_form_failed_save_keeps_draft_and_blocks_discard(self):
        from persona_runtime.character import CharacterService
        window=self.window();store=Store(self.paths);store.initialize();service=RuntimeService(store)
        persona=service.create_persona(generic_persona());service.use_persona(persona['id'])
        value=CharacterService(store).current(persona['id'])
        dialog=window.character_editor(persona['id'],value)
        box=dialog.character_editors[('appearance','height_cm')];box.insert('1.0','162')
        with patch.object(window,'run_task',return_value=None):dialog.character_save()
        self.assertEqual(box.get('1.0','end-1c'),'162')
        with patch('persona_runtime.desktop_character.messagebox.askyesno',return_value=False):
            self.assertFalse(window.character_can_leave())
            self.assertTrue(dialog.winfo_exists())
        with patch('persona_runtime.desktop_character.messagebox.askyesno',return_value=True):
            self.assertTrue(window.character_can_leave())

    def test_m6_character_form_async_save_and_repeat_noop(self):
        from persona_runtime.character import CharacterService
        window=self.window();store=Store(self.paths);store.initialize();service=RuntimeService(store)
        persona=service.create_persona(generic_persona());service.use_persona(persona['id']);characters=CharacterService(store)
        dialog=window.character_editor(persona['id'],characters.current(persona['id']))
        box=dialog.character_editors[('appearance','height_cm')];box.insert('1.0','162')
        with patch.object(window.client,'manage',side_effect=lambda action,**args:getattr(characters,action.split('.')[1])(**args)),patch.object(window,'refresh_character'):
            dialog.character_save()
            self.assertEqual(str(box.cget('state')),'disabled')
            self.pump(window,lambda:not dialog.character_busy())
            self.assertEqual(characters.fact(persona['id'],'appearance.height_cm')['value'],162)
            self.assertFalse(dialog.character_dirty())
            dialog.character_save();self.pump(window,lambda:not dialog.character_busy())
            self.assertEqual(len(characters.history(persona['id'])),2)
        dialog.character_close()

    def test_m6_character_page_management_actions_are_mapped(self):
        window=self.window()
        with patch.object(window,'refresh_character'):
            window.show_page('character');window.update()
        for label in ('读取角色资料','编辑角色资料','导出角色包','导入为新角色','编辑姓名与人格核心'):
            button=self.control(window.pages['character'],label)
            self.assertTrue(button.winfo_ismapped(),label)

    def test_m6_character_form_footer_visible_at_three_scales(self):
        from persona_runtime.character import CharacterService
        window=self.window();store=Store(self.paths);store.initialize();service=RuntimeService(store)
        persona=service.create_persona(generic_persona());service.use_persona(persona['id'])
        dialog=window.character_editor(persona['id'],CharacterService(store).current(persona['id']))
        for scale in (1,1.25,1.5):
            window.tk.call('tk','scaling',96/72*scale);dialog.geometry('520x370');window.update()
            save=self.control(dialog,'保存角色资料')
            self.assertTrue(save.winfo_ismapped())
            self.assertLessEqual(save.winfo_rooty()+save.winfo_height(),dialog.winfo_rooty()+dialog.winfo_height())
        dialog.character_close()

    def test_m6_stale_character_read_does_not_replace_new_selection(self):
        window=self.window();callbacks=[]
        with patch.object(window,'run_task',side_effect=lambda name,resources,action,success=None,**kw:callbacks.append(success)):
            window.refresh_character()
        window.view_epoch+=1
        callbacks[0](('old',{'invalid':'must not be read'}))
        self.assertIsNone(window.character_data)

    def test_force_stop_is_available_on_both_pages_and_uses_urgent_lane(self):
        window = self.window()
        force_buttons = [button for button, _ in window.buttons if button.cget("text") == "强制停止全部相关程序"]
        self.assertEqual(len(force_buttons), 2)
        with patch("persona_runtime.desktop.messagebox.askyesno", return_value=True), patch.object(window, "run_task") as task:
            window.force_stop_everything()
            self.assertTrue(task.call_args.kwargs["urgent"])
            self.assertEqual(task.call_args.args[1], {"emergency"})
            with patch.object(window.controller, "force_stop_all", return_value={"success": True}) as force:
                task.call_args.args[2](None)
                force.assert_called_once()


    def test_m61_buffer_controls_and_stale_read(self):
        window=self.window();window.persona_id='persona-a';window.view_epoch=1
        callbacks=[]
        with patch.object(window,'run_task',side_effect=lambda *a,**k:callbacks.append(a[3])):
            window.refresh_batches()
        window.view_epoch=2;window.persona_id='persona-b'
        callbacks[0](([],{}))
        self.assertEqual(window.batch_policies,{})
        labels={c.cget('text') for c in self.widgets(window) if c.winfo_class()=='TButton'}
        for text in ('切换窗口模式','切换 QQ 模式','立即回复','继续中断输入','取消未生成批次'):self.assertIn(text,labels)

    def test_m61_receipt_ack_keeps_new_draft(self):
        window=self.window();window.persona_id='persona-a';window.session_id='session-a'
        window.chat_input.insert('1.0','first');callbacks=[]
        with patch.object(window,'run_task',side_effect=lambda *a,**k:callbacks.append(a[3])),patch.object(window,'load_chat'),patch.object(window,'refresh_batches'):
            window.send_chat();window.chat_input.delete('1.0','end');window.chat_input.insert('1.0','next')
            callbacks[0]({'receipt_id':'r','batch_id':'b','turn_id':None,'session_id':'session-a','status':'collecting'})
        self.assertEqual(window.chat_input.get('1.0','end-1c'),'next');self.assertIsNone(window.selected_request)

    def test_m61_linked_batch_does_not_duplicate_old_collecting_view(self):
        window=self.window();window.persona_id='p';window.session_id='s'
        window.turns=[{'id':'t','text':'input','reply':'reply','usage':None,'status':'completed','input_batch_id':'b'}]
        window.chat_batches=[{'id':'b','session_id':'s','persona_id':'p','members':[{'text':'input'}],'status':'collecting','remaining_ms':4900}]
        window.render_chat();text=window.transcript.get('1.0','end')
        self.assertEqual(text.count('input'),1);self.assertNotIn('收集中',text)


if __name__ == "__main__":
    unittest.main()
