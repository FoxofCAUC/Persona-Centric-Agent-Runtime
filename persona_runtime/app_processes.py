"""Application-owned process jobs. Children cannot execute before job assignment."""
import ctypes
from ctypes import wintypes
import subprocess
import _winapi

from .models import RuntimeErrorBase


class BasicLimits(ctypes.Structure):
    _fields_ = [('user',ctypes.c_longlong),('job_user',ctypes.c_longlong),('flags',wintypes.DWORD),
        ('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',wintypes.DWORD),
        ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic',BasicLimits),('io',ctypes.c_ulonglong*6),
        ('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
        ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]


class Job:
    def __init__(self,priority=None):
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        k=self.kernel
        k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];k.CreateJobObjectW.restype=wintypes.HANDLE
        k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        k.CloseHandle.argtypes=[wintypes.HANDLE]
        k.ResumeThread.argtypes=[wintypes.HANDLE];k.ResumeThread.restype=wintypes.DWORD
        self.handle=k.CreateJobObjectW(None,None)
        limits=ExtendedLimits();limits.basic.flags=0x2000
        if priority is not None:
            # A child (including Chromium/QQ GPU workers) must not undo the
            # background class inherited at launch. The nested job's effective
            # priority remains the lowest class in its job chain.
            limits.basic.flags|=0x20;limits.basic.priority=priority
        if not self.handle or not k.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            self.close();raise RuntimeErrorBase('无法建立所属进程组；没有启动组件。')

    def assign(self, process):
        if not self.handle or not self.kernel.AssignProcessToJobObject(self.handle,process):
            raise RuntimeErrorBase('无法绑定所属进程组；组件不会开始执行。')

    def close(self):
        if getattr(self,'handle',None):
            self.kernel.CloseHandle(self.handle);self.handle=None


class Child:
    def __init__(self, handle, pid):self.handle,self.pid=handle,pid
    def poll(self):
        return None if _winapi.WaitForSingleObject(self.handle,0)==258 else _winapi.GetExitCodeProcess(self.handle)
    def wait(self,timeout=None):
        if _winapi.WaitForSingleObject(self.handle,0xffffffff if timeout is None else int(timeout*1000))==258:
            raise subprocess.TimeoutExpired(str(self.pid),timeout)
        return _winapi.GetExitCodeProcess(self.handle)
    def close(self):
        if self.handle:_winapi.CloseHandle(self.handle);self.handle=None


class AppProcesses:
    def __init__(self):self.job,self.roles=Job(),{}

    def start(self,role,args,*,cwd,env):
        if role in self.roles: self.stop(role)
        job=Job(priority=0x4000 if role in ('astrbot','napcat') else None);ph=th=None
        try:
            ph,th,pid,_=_winapi.CreateProcess(None,subprocess.list2cmdline(args),None,None,False,
                subprocess.CREATE_NO_WINDOW | 4 | (0x4000 if role in ('astrbot','napcat') else 0),env,str(cwd),subprocess.STARTUPINFO())
            self.job.assign(ph);job.assign(ph)
            if job.kernel.ResumeThread(th)==0xffffffff:raise OSError('ResumeThread failed')
            child=Child(ph,pid);self.roles[role]=(job,child)
            return child
        except BaseException:
            if ph:
                _winapi.TerminateProcess(ph,1);_winapi.CloseHandle(ph)
            job.close();raise
        finally:
            if th:_winapi.CloseHandle(th)

    def stop(self,role):
        owned=self.roles.pop(role,None)
        if owned:
            job,child=owned;job.close()
            try:child.wait(timeout=3)
            finally:child.close()

    def close(self):
        self.job.close()
        errors=[]
        for role in list(self.roles):
            try:self.stop(role)
            except Exception as error:errors.append(error)
        if errors:raise errors[0]
