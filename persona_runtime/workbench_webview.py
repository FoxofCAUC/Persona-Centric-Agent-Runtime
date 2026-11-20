"""Host the reviewed workbench UI in a Windows WebView2 window."""

import ctypes
import json
from base64 import b64encode
from ctypes import wintypes
from contextlib import ExitStack
from importlib.metadata import version
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import time
from urllib.parse import unquote

from .app_runtime import AppRuntime
from .config import atomic_json
from .workbench_import import WorkbenchData
from .lifecycle import ServiceController
from .models import RuntimeErrorBase
from .workbench_errors import report_failure
from .workbench_instance import acquire_or_wake, next_wakeup, wait_for_previous_components


WEBVIEW_VERSION = "6.2.1"
STARTUP_TIMEOUT = 25
WINDOW_TITLE = "PCAR 新版工作台"
APPEARANCE_FILE = Path("config", "workbench-appearance.json")
APPEARANCE_MODES = ("system", "light", "dark")
WINDOW_COLORS = {
    "light": ("#e6e8de", "#ebece2", "#24352f"),
    "dark": ("#1a211e", "#252f29", "#dce6dc"),
}
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


def read_appearance(paths):
    try:
        saved = json.loads((paths.home / APPEARANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "system"
    mode = saved.get("mode") if isinstance(saved, dict) else None
    return mode if mode in APPEARANCE_MODES else "system"


def initial_window_appearance(mode):
    if mode in WINDOW_COLORS:
        return mode
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as personalize:
            light = winreg.QueryValueEx(personalize, "AppsUseLightTheme")[0]
    except (ImportError, OSError):
        return "light"
    return "dark" if light == 0 else "light"


def workbench_html(appearance="system"):
    assets = Path(__file__).with_name("workbench_web")
    html = (assets / "index.html").read_text(encoding="utf-8")
    mode = appearance if appearance in APPEARANCE_MODES else "system"
    html = html.replace('data-appearance="system"', f'data-appearance="{mode}" data-native-workbench', 1)
    script = "\n".join((assets / name).read_text(encoding="utf-8") for name in ("desktop.js","persona_import.js"))
    body, mark, rest = html.rpartition("</body>")
    if not mark:
        raise RuntimeErrorBase("新版工作台页面不完整。")
    return body + "<script>\n" + script + "\n</script>" + mark + rest


def taskbar_available():
    find = ctypes.WinDLL("user32", use_last_error=True).FindWindowW
    find.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    find.restype = wintypes.HWND
    return bool(find("Shell_TrayWnd", None))


def local_document_matches(uri, encoded_document):
    uri = uri.split("#", 1)[0]
    if uri == "about:blank":
        return True
    header, separator, payload = uri.partition(",")
    if not separator or header.split(";", 1)[0].lower() != "data:text/html":
        return False
    return header.lower().endswith(";base64") and unquote(payload) == encoded_document


class DesktopControls(WorkbenchData):
    """Only these window and status operations are exposed to the local page."""

    def __init__(self, post, status, paths, window):
        super().__init__(paths,window)
        self._post = post
        self._status = status

    def ready(self):
        self._post(("ready", None))

    def runtime_status(self):
        return self._status()

    def hide_to_tray(self):
        self._post(("tray", None))

    def exit_workbench(self):
        self._post(("exit", None))

    def save_appearance(self, mode):
        if mode not in APPEARANCE_MODES:
            raise RuntimeErrorBase("外观选项无效。")
        atomic_json(self._config.paths.home / APPEARANCE_FILE, {"mode": mode}, durable=True)

    def set_window_appearance(self, theme):
        if theme not in ("light", "dark"):
            raise RuntimeErrorBase("窗口外观无效。")
        self._post(("appearance", theme))

    def presentation_visible(self, x=None, y=None):
        try:
            form = self._window().native
            hwnd = wintypes.HWND(form.Handle.ToInt64())
            user32 = ctypes.WinDLL("user32",use_last_error=True)
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return False
            if x is None or y is None:
                return True
            point = wintypes.POINT(int(x),int(y))
            if not user32.ClientToScreen(hwnd,ctypes.byref(point)):
                return False
            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = wintypes.HWND
            target = user32.WindowFromPoint(point)
            user32.IsChild.argtypes = [wintypes.HWND,wintypes.HWND]
            return bool(target and (target==hwnd.value or user32.IsChild(hwnd,target)))
        except (AttributeError, OSError, RuntimeError, ValueError, TypeError, OverflowError):
            return False


class WebViewHost:
    def __init__(self, controller, appearance="system"):
        self.controller = controller
        self.paths = controller.paths
        self.lease_token = controller.console_lease["token"]
        self.runtime = None
        self.window = None
        self.form = None
        self.tray = None
        self.menu = None
        self.timer = None
        self.queue = SimpleQueue()
        self.close_worker = None
        self.close_retry_at=None
        self.exit_step=None
        self.failure = None
        self.closed = False
        self.closing = False
        self.in_tray = False
        self.wake_id = None
        self.next_poll = 0
        self.initial_theme = initial_window_appearance(appearance)
        self.controls = DesktopControls(self.queue.put, self.runtime_status,self.paths,lambda:self.window)
        self.controls._repair_status=self.repair_status
        self.controls._client.endpoint_check=self.controller.service_endpoint_state
        self.controller.repair_notice=lambda record:self.queue.put(("repair",record))

    def repair_status(self):
        from .local_repair import public,read
        return self.runtime.repair.status() if self.runtime else public(read(self.paths))

    def run(self, webview, html):
        self.encoded_document = b64encode(html.encode("utf-8")).decode("ascii")
        self.window = webview.create_window(
            WINDOW_TITLE, html=html, js_api=self.controls,
            width=1120, height=780, min_size=(390, 560),
            background_color=WINDOW_COLORS[self.initial_theme][0],
        )
        self.window.events.initialized += self.check_renderer
        self.window.events.before_show += self.attach
        self.window.events.closing += self.on_closing
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        webview.start(
            gui="edgechromium", debug=False, http_server=False, private_mode=True,
            storage_path=str(self.paths.home / "cache/webview"),
        )
        if self.failure is not None:
            raise self.failure
        if not self.closed:
            raise RuntimeErrorBase("新版工作台窗口意外结束。")

    def check_renderer(self, renderer):
        if renderer != "edgechromium":
            self.failure = RuntimeErrorBase("无法使用 Microsoft Edge WebView2。")
            return False
        return True

    def keep_local_page(self, sender, args):
        if not local_document_matches(str(args.Uri), self.encoded_document):
            args.Cancel = True

    def attach(self):
        from System import Action
        from System.Windows.Forms import Timer

        self.form = self.window.native
        try:
            self.apply_window_appearance(self.initial_theme)
            self.timer = Timer()
            self.timer.Interval = 150
            self.timer.Tick += self.tick
            self.form.FormClosed += self.detach
            self.form.webview.NavigationStarting += self.keep_local_page
            self.deadline = time.monotonic() + STARTUP_TIMEOUT
            try:
                self.attach_tray()
            except Exception:
                self.release_tray()
            self.timer.Start()
        except Exception as error:
            self.form.BeginInvoke(Action(lambda failure=error: self.abort(failure)))

    def apply_window_appearance(self, theme):
        from System.Drawing import ColorTranslator, SystemColors
        from System.Windows.Forms import SystemInformation

        background, caption, text = WINDOW_COLORS[theme]
        high_contrast = SystemInformation.HighContrast
        color = SystemColors.Window if high_contrast else ColorTranslator.FromHtml(background)
        self.form.BackColor = color
        self.form.webview.DefaultBackgroundColor = color
        try:
            set_attribute = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
        except (AttributeError, OSError):
            return
        set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        hwnd = wintypes.HWND(self.form.Handle.ToInt64())
        for attribute, css in ((DWMWA_CAPTION_COLOR, caption), (DWMWA_TEXT_COLOR, text)):
            value = DWMWA_COLOR_DEFAULT if high_contrast else ColorTranslator.ToWin32(ColorTranslator.FromHtml(css))
            colorref = wintypes.DWORD(value)
            set_attribute(hwnd, attribute, ctypes.byref(colorref), ctypes.sizeof(colorref))

    def attach_tray(self):
        from System.Drawing import SystemIcons
        from System.Windows.Forms import ContextMenuStrip, MouseButtons, NotifyIcon

        self.menu = ContextMenuStrip()
        for text, action in (
            ("打开工作台", "show"), ("运行状态", "status"), ("退出", "exit"),
        ):
            self.menu.Items.Add(text).Click += (
                lambda sender, args, value=action: self.queue.put((value, None))
            )
        self.tray = NotifyIcon()
        self.tray.Icon = SystemIcons.Application
        self.tray.Text = WINDOW_TITLE
        self.tray.ContextMenuStrip = self.menu

        def open_from_tray(sender, args):
            if args.Button == MouseButtons.Left:
                self.queue.put(("show", None))

        self.tray.MouseClick += open_from_tray
        self.tray.Visible = True

    def release_tray(self):
        if self.tray is not None:
            self.tray.Visible = False
            self.tray.Dispose()
            self.tray = None
        if self.menu is not None:
            self.menu.Dispose()
            self.menu = None

    def detach(self, *_):
        if self.timer is not None:
            self.timer.Stop()
            self.timer.Dispose()
            self.timer = None
        self.release_tray()

    def run_script(self, script):
        if self.window.events.loaded.is_set():
            self.form.webview.ExecuteScriptAsync(script)

    def notice(self, message):
        self.run_script("window.pcarDesktop.notice(" + json.dumps(message) + ")")

    def record_display(self, state):
        if not self.closing:self.controller.update_console(display_state=state)

    def show(self):
        from System.Windows.Forms import FormWindowState

        self.form.Show()
        if self.form.WindowState == FormWindowState.Minimized:
            self.form.WindowState = FormWindowState.Normal
        self.form.Activate()
        self.in_tray = False
        self.run_script("window.pcarLive?.wake()")
        try:
            self.record_display("visible")
        except OSError:
            self.notice("窗口已打开，运行状态暂未保存。")

    def hide(self):
        if not self.tray or not self.tray.Visible or not taskbar_available():
            self.notice("托盘暂不可用，窗口保持可见。")
            return
        try:
            self.record_display("tray")
        except OSError:
            self.notice("无法保存窗口状态，窗口保持可见。")
            return
        self.form.Hide()
        self.in_tray = True

    def runtime_status(self):
        if self.closing:
            return {"label": "正在结算并释放本机组件。"}
        if self.controller.closing.is_set():
            return {"label": "正在核对组件的退出状态，工作台会自动复查。"}
        if self.runtime is None:
            return {"label": "工作台正在打开。"}
        try:
            running = self.controller.health("service")["state"] == "运行"
        except (OSError, ValueError, KeyError, RuntimeErrorBase):
            running = False
        return {"label": "本机人格服务已运行。" if running else "本机人格服务尚未就绪。"}

    def start_runtime(self):
        if self.runtime is None:
            self.record_display("visible")
            self.runtime = AppRuntime(self.controller)
            self.runtime.start()

    def on_closing(self):
        if self.closed:
            return True
        if not self.closing:
            self.closing = True
            self.run_script("window.pcarLive?.exitStatus('stop_admission')")
            self.controller.closing.set()
            if self.runtime is not None:
                self.runtime.stop()
            self.notice("正在退出并释放本机组件。")
            self.close_worker = Thread(target=self.shutdown, name="webview-shutdown", daemon=True)
            self.close_worker.start()
        return False

    def shutdown(self):
        try:
            with self.controller.lifecycle_lock:
                if self.controller.console_lease is not None or self.controller.exit_request:
                    report=self.controller.shutdown()
                    released = report["processes_released"]
                    if not released and report.get('retry_after'):
                        self.queue.put(('retry_exit',(report['retry_after'],report.get('blocker'))))
                        return
                else:
                    released = not self.controller.has_resident_processes()
        except Exception:
            self.queue.put(('retry_exit',(5,'component_ownership_or_release_unconfirmed')))
            return
        self.queue.put(("closed", released))

    def finish_close(self, released):
        if released:
            self.closed = True
            self.detach()
            self.form.Close()
            return
        self.queue.put(("retry_exit",(5,"component_ownership_or_release_unconfirmed")))

    def tick(self, *_):
        try:
            while not self.closed:
                try:
                    action, value = self.queue.get_nowait()
                except Empty:
                    break
                if action == "closed":
                    self.finish_close(value)
                elif action=="repair" and not self.closing and not self.closed:
                    self.run_script("window.pcarLive?.repairStatus("+json.dumps(value)+")")
                elif action=="retry_exit":
                    delay,blocker=value
                    self.close_retry_at=time.monotonic()+delay
                    self.run_script("window.pcarLive?.exitStatus("+json.dumps(blocker or 'releasing')+")")
                elif action == "exit":
                    self.on_closing()
                elif action in ("show", "status"):
                    self.show()
                    if action == "status":
                        self.run_script("window.pcarDesktop.status()")
                elif not self.closing and not self.controller.closing.is_set():
                    if action == "ready":
                        self.start_runtime()
                    elif action == "tray":
                        self.hide()
                    elif action == "appearance":
                        self.apply_window_appearance(value)
            if self.closing and self.close_retry_at is not None and time.monotonic()>=self.close_retry_at:
                self.close_retry_at=None
                self.close_worker=Thread(target=self.shutdown,name="webview-shutdown",daemon=True)
                self.close_worker.start()
            if self.closing and self.close_retry_at is None:
                step=getattr(self.controller,'shutdown_progress',{}).get('phase')
                if step and step!=self.exit_step:
                    self.exit_step=step;self.run_script("window.pcarLive?.exitStatus("+json.dumps(step)+")")
            if self.closed or self.closing:
                return
            now = time.monotonic()
            if self.runtime is None and now >= self.deadline:
                raise RuntimeErrorBase("WebView2 窗口未能加载工作台页面。")
            if now < self.next_poll:
                return
            self.next_poll = now + .5
            wake = next_wakeup(self.paths, self.lease_token, self.wake_id)
            if wake:
                self.wake_id, action = wake
                if action == "exit":
                    self.on_closing()
                else:
                    self.show()
            elif self.in_tray and not taskbar_available():
                self.show()
                self.notice("托盘暂不可用，已恢复工作台窗口。")
        except Exception as error:
            self.abort(error)

    def abort(self, error):
        self.failure = error
        self.closed = True
        self.detach()
        self.form.Close()

    def release(self):
        if self.runtime is not None:
            self.runtime.stop()
        if self.close_worker is not None:
            self.close_worker.join()
        if self.controller.console_lease is None:
            return
        try:
            if self.runtime is not None:
                self.controller.closing.set()
                with self.controller.lifecycle_lock:
                    if not self.controller.shutdown()["processes_released"]:
                        raise RuntimeErrorBase("本机组件仍在退出，请检查该实例的运行状态。")
        finally:
            if self.controller.console_lease is not None:
                self.controller.end_console()


def run(paths):
    stage = "initialize_paths"
    try:
        with ExitStack() as cleanup:
            paths.initialize()
            stage = "instance_lock"
            lock = acquire_or_wake(paths)
            if lock is None:
                return
            cleanup.callback(lock.close)
            stage = "webview_dependency"
            if version("pywebview") != WEBVIEW_VERSION:
                raise RuntimeErrorBase("新版窗口依赖的版本不匹配。")
            import webview

            stage = "webview_resources"
            appearance = read_appearance(paths)
            html = workbench_html(appearance)
            stage = "console_lease"
            controller = ServiceController(paths)
            lease = controller.start_console()
            host = WebViewHost(controller, appearance)
            cleanup.callback(host.release)
            stage = "cleanup_legacy"
            wait_for_previous_components(controller, lease)
            stage = "webview_runtime"
            host.run(webview, html)
    except Exception as error:
        report_failure(paths, error, stage)
        raise
