"""Native Windows tray hosted by the existing Tk thread, with no resident helper."""
import ctypes
from ctypes import wintypes as w
from collections import deque
from uuid import uuid4


class Tray:
    MESSAGE=0x8000+73

    def __init__(self,window):
        self.window=window;self.active=False;self.events=deque();self.poll_id=None;self.hwnd=None;self.closed=False;self.menu_widget=None
        self.user=ctypes.WinDLL('user32',use_last_error=True)
        self.shell=ctypes.WinDLL('shell32',use_last_error=True)
        self.proc_type=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
        class Data(ctypes.Structure):
            _fields_=[('size',w.DWORD),('hwnd',w.HWND),('id',w.UINT),('flags',w.UINT),
                ('message',w.UINT),('icon',w.HICON),('tip',w.WCHAR*128),('state',w.DWORD),
                ('state_mask',w.DWORD),('info',w.WCHAR*256),('version',w.UINT),
                ('info_title',w.WCHAR*64),('info_flags',w.DWORD),('guid',ctypes.c_byte*16),('balloon',w.HICON)]
        # Never subclass a Tk HWND: calling Tcl's saved WndProc through ctypes
        # re-enters Tcl outside _tkinter's saved thread-state contract and can
        # abort Python. A separate invisible HWND on this SAME UI thread only
        # queues events; Tk's normal after callback performs all Tk operations.
        self.user.DefWindowProcW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]
        self.user.DefWindowProcW.restype=ctypes.c_ssize_t
        class WindowClass(ctypes.Structure):
            _fields_=[('style',w.UINT),('proc',self.proc_type),('class_extra',ctypes.c_int),('window_extra',ctypes.c_int),
                ('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('brush',w.HANDLE),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetModuleHandleW.argtypes=[w.LPCWSTR];kernel.GetModuleHandleW.restype=w.HMODULE
        self.instance=kernel.GetModuleHandleW(None);self.class_name='PCAR.Tray.'+uuid4().hex
        self.callback=self.proc_type(self.dispatch)
        self.window_class=WindowClass();self.window_class.proc=self.callback
        self.window_class.instance=self.instance;self.window_class.name=self.class_name
        self.user.RegisterClassW.argtypes=[ctypes.POINTER(WindowClass)];self.user.RegisterClassW.restype=w.WORD
        self.user.UnregisterClassW.argtypes=[w.LPCWSTR,w.HINSTANCE];self.user.UnregisterClassW.restype=w.BOOL
        self.user.CreateWindowExW.argtypes=[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,ctypes.c_void_p]
        self.user.CreateWindowExW.restype=w.HWND
        self.user.DestroyWindow.argtypes=[w.HWND];self.user.DestroyWindow.restype=w.BOOL
        if not self.user.RegisterClassW(ctypes.byref(self.window_class)):raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd=self.user.CreateWindowExW(0x80,self.class_name,'PCAR Tray',0,0,0,0,0,None,None,self.instance,None)
        if not self.hwnd:
            self.user.UnregisterClassW(self.class_name,self.instance)
            raise ctypes.WinError(ctypes.get_last_error())
        self.user.RegisterWindowMessageW.argtypes=[w.LPCWSTR];self.user.RegisterWindowMessageW.restype=w.UINT
        self.recreate=self.user.RegisterWindowMessageW('TaskbarCreated')
        self.user.LoadIconW.argtypes=[w.HINSTANCE,ctypes.c_void_p];self.user.LoadIconW.restype=w.HICON
        self.shell.Shell_NotifyIconW.argtypes=[w.DWORD,ctypes.POINTER(Data)]
        self.shell.Shell_NotifyIconW.restype=w.BOOL
        self.data=Data();self.data.size=ctypes.sizeof(Data);self.data.hwnd=self.hwnd
        self.data.id=1;self.data.flags=1|2|4;self.data.message=self.MESSAGE
        self.data.icon=self.user.LoadIconW(None,ctypes.c_void_p(32512));self.data.tip='PCAR · 工作台运行中'
        self.add()
        self.poll_id=self.window.after(100,self.poll)

    def add(self):
        self.active=bool(self.shell.Shell_NotifyIconW(0,ctypes.byref(self.data)))
        if self.active:
            self.data.version=4;self.shell.Shell_NotifyIconW(4,ctypes.byref(self.data))
        return self.active

    def set_tip(self,text):
        self.data.tip=text[:127]
        if self.active:self.shell.Shell_NotifyIconW(1,ctypes.byref(self.data))

    def dispatch(self,hwnd,message,wp,lp):
        try:
            if message==getattr(self,'recreate',-1):
                self.events.append('recreate')
            elif message==self.MESSAGE:
                event=lp & 0xffff
                if event in (0x202,0x203,0x400,0x401):self.events.append('show')
                elif event in (0x205,0x7b):self.events.append('menu')
        except Exception:
            self.events.append('show')
        return self.user.DefWindowProcW(hwnd,message,wp,lp)

    def poll(self):
        self.poll_id=None
        if self.closed:return
        while self.events:
            event=self.events.popleft()
            if event=='recreate':
                if not self.add():self.window.show_from_tray(activate=False)
            elif event=='show':self.window.show_from_tray()
            elif event=='menu':self.menu()
            if self.closed:return
        self.poll_id=self.window.after(100,self.poll)

    def menu(self):
        import tkinter as tk
        if self.menu_widget is None:
            self.menu_widget=tk.Menu(self.window,tearoff=False)
            self.menu_widget.add_command(label='打开工作台',command=lambda:self.defer_menu_action('open'))
            self.menu_widget.add_command(label='运行状态',command=lambda:self.defer_menu_action('status'))
            self.menu_widget.add_separator();self.menu_widget.add_command(label='退出',command=lambda:self.defer_menu_action('exit'))
        menu=self.menu_widget
        try:menu.tk_popup(self.window.winfo_pointerx(),self.window.winfo_pointery())
        finally:
            if not self.closed:
                try:menu.grab_release()
                except tk.TclError:pass

    def defer_menu_action(self,action):
        # Finish the native popup's modal loop before restoring/activating Tk.
        # This is an explicit user action; routine background work never calls it.
        def perform():
            if self.closed:return
            if action=='exit':self.window.close_window();return
            self.window.show_from_tray()
            if action=='status':self.window.show_page('home');self.window.poll(force=True)
        self.window.after_idle(perform)

    def close(self):
        if self.closed:return
        self.closed=True
        if self.menu_widget is not None:
            try:self.menu_widget.destroy()
            except Exception:pass
            self.menu_widget=None
        if self.poll_id:
            try:self.window.after_cancel(self.poll_id)
            except Exception:pass
            self.poll_id=None
        if self.active:self.shell.Shell_NotifyIconW(2,ctypes.byref(self.data));self.active=False
        if self.hwnd:self.user.DestroyWindow(self.hwnd);self.hwnd=None
        self.user.UnregisterClassW(self.class_name,self.instance)
        # ctypes callbacks hold bound-method cycles. Once unregistered, release
        # every reference to Tk on its owning thread rather than letting a
        # worker's later cyclic GC finalize the Tcl interpreter.
        self.events.clear();self.window_class=None;self.callback=None;self.window=None
