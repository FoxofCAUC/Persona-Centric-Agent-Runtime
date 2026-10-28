"""Single-instance OS file lock, released automatically on process exit."""
import msvcrt
import ctypes
from ctypes import wintypes
import os

from .models import RuntimeErrorBase


class InstanceLock:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = None
        try:
            self.file = path.open("a+b")
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if self.file is not None:
                self.file.close()
            raise RuntimeErrorBase("This project instance is already running") from None

    def close(self):
        if self.file is not None and not self.file.closed:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


class ProcessIdentityHandle:
    """A retained kernel handle identifies a process even after its PID is reused."""
    def __init__(self, pid, expected=None):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, int(pid))
        if not self.handle:
            raise RuntimeErrorBase("控制窗口进程已退出或无法核对。")
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not self.kernel.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited),
                                           ctypes.byref(kernel_time), ctypes.byref(user_time)):
            self.close()
            raise RuntimeErrorBase("无法核对控制窗口进程创建时间。")
        self.identity = {"pid": int(pid), "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
        if expected is not None and self.identity != expected:
            self.close()
            raise RuntimeErrorBase("控制窗口进程标识已变化，拒绝复用旧 PID。")

    def alive(self):
        return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle, 0) == 0x102

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def current_process_identity():
    process = ProcessIdentityHandle(os.getpid())
    try:
        return process.identity
    finally:
        process.close()


def process_identity_alive(identity):
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int or type(identity.get("created")) is not int:
        return False
    try:
        process = ProcessIdentityHandle(identity["pid"], identity)
    except (OSError, ValueError, RuntimeErrorBase):
        return False
    try:
        return process.alive()
    finally:
        process.close()


def bind_host_job():
    """Keep this handle open for the host's lifetime; exit kills all its children."""
    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", Counters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)  # Non-inheritable; only this host keeps it open.
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        if handle:
            kernel.CloseHandle(handle)
        raise RuntimeErrorBase("无法建立窗口绑定的服务进程组，拒绝后台启动。")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise RuntimeErrorBase("无法绑定服务进程组，拒绝后台启动。")
    # Do not close explicitly: this job contains the caller. Windows closes
    # the final handle on every host exit, including a watchdog force exit.
    return handle
