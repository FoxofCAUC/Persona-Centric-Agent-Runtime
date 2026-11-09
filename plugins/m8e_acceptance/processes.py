"""Read only the Workbench and its verified components, not opened browsers."""
import psutil

from persona_runtime.instance import ProcessIdentityHandle
from persona_runtime.lifecycle import ServiceController


def owned_processes(paths,identity):
    handle=ProcessIdentityHandle(identity['pid'],identity)
    handle.close()
    owner=psutil.Process(identity['pid']);controller=ServiceController(paths)
    cfg=controller.config.load();roots=[]
    for role in ('service','astrbot','napcat'):
        try:
            if controller.owned(role):
                record=controller.record(role)
                if not record:continue
                host=psutil.Process(record['host']['ProcessId']);branch=host
                # Include venv launcher ancestors only along the live GUI branch.
                while branch.ppid()!=owner.pid:
                    parent=branch.parent()
                    if not parent or not owner.create_time()<=parent.create_time()<=branch.create_time():break
                    branch=parent
                roots.append(branch if branch.ppid()==owner.pid else host)
        except psutil.NoSuchProcess:continue
    # These short-lived diagnostics are launched directly by the GUI, outside
    # the component jobs. Default browsers are also direct children, but remain
    # independent apps and must never become an exit assertion or kill target.
    for child in owner.children():
        try:
            if child.name().lower()!='powershell.exe':continue
            args=child.cmdline()
            if not all(flag in args for flag in ('-NoProfile','-NonInteractive','-Command')):continue
            script=args[args.index('-Command')+1]
            process_query=script.startswith('[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-CimInstance Win32_Process -Filter ')
            port_query=any(script.startswith('Get-NetTCPConnection -State Listen -LocalPort '+str(cfg[key])+' ')
                for key in ('service_port','astrbot_port','onebot_port','napcat_port'))
            qq_query=script.startswith('Get-CimInstance Win32_Process -Filter "Name=\'QQ.exe\'"')
            if process_query or port_query or qq_query:roots.append(child)
        except (psutil.NoSuchProcess,psutil.AccessDenied,ValueError,IndexError):continue
    result={owner.pid:owner}
    for root in roots:
        try:
            result[root.pid]=root
            result.update((p.pid,p) for p in root.children(recursive=True))
        except psutil.NoSuchProcess:continue
    return list(result.values())
