"""Open local management pages without leaking project paths to other apps."""
import ctypes
from ctypes import wintypes as w
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from .models import RuntimeErrorBase


def normal_user_environment():
    """Read the user's Windows environment, without inheriting this process."""
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    advapi=ctypes.WinDLL('advapi32',use_last_error=True)
    userenv=ctypes.WinDLL('userenv',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE]
    advapi.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)];advapi.OpenProcessToken.restype=w.BOOL
    userenv.CreateEnvironmentBlock.argtypes=[ctypes.POINTER(w.LPVOID),w.HANDLE,w.BOOL];userenv.CreateEnvironmentBlock.restype=w.BOOL
    userenv.DestroyEnvironmentBlock.argtypes=[w.LPVOID];userenv.DestroyEnvironmentBlock.restype=w.BOOL
    token=w.HANDLE();block=w.LPVOID()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),0x0008|0x0002,ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not userenv.CreateEnvironmentBlock(ctypes.byref(block),token,False):
            raise ctypes.WinError(ctypes.get_last_error())
        result={};position=block.value
        while True:
            entry=ctypes.wstring_at(position)
            if not entry:break
            position+=len(entry.encode('utf-16-le',errors='surrogatepass'))+ctypes.sizeof(ctypes.c_wchar)
            if entry.startswith('='):continue
            key,value=entry.split('=',1)
            if key.upper().startswith(('PCAR_','PERSONA_RUNTIME_','ASTRBOT_','NAPCAT_')):continue
            result[key]=value
        if not result.get('USERPROFILE') or not result.get('TEMP'):
            raise RuntimeErrorBase('无法读取外部浏览器的正常用户环境。')
        return result
    finally:
        if block:userenv.DestroyEnvironmentBlock(block)
        if token:kernel.CloseHandle(token)


def open_browser(url):
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise RuntimeErrorBase('仅允许打开已验证的本机管理页。')
    env=normal_user_environment();cwd=Path(env['USERPROFILE'])
    if not cwd.is_dir():raise RuntimeErrorBase('正常用户目录不可用，未启动浏览器。')
    # The short-lived helper uses Windows' default browser association. Its
    # environment comes from the user token, not the Workbench TEMP/TMP or QQ
    # data overrides. No process-global variables or browser settings change.
    result=subprocess.run([sys.executable,'-B','-c',
        'import sys,webbrowser;raise SystemExit(0 if webbrowser.open(sys.argv[1]) else 1)',url],
        cwd=cwd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    return result.returncode==0
