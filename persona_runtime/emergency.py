"""Explicit emergency stop: select related programs without ownership records."""
import json
import os
from pathlib import Path
import subprocess
import time

from .config import atomic_json
from .deployment import NO_WINDOW, REPO
from .models import identifier, timestamp

PYTHON_NAMES = {"python.exe", "pythonw.exe", "astrbot.exe"}
ROOT_NAMES = {"qq.exe", "napcat.exe", "napcatshell.exe", "napcatwinbootmain.exe"}
PROTECTED_NAMES = {"explorer.exe", "codex.exe", "chatgpt.exe", "msedge.exe", "chrome.exe", "firefox.exe",
                   "svchost.exe", "lsass.exe", "csrss.exe", "services.exe", "wininit.exe", "dwm.exe", "system"}


def normalized(value):
    return str(value or "").replace("/", "\\").casefold()


def select_targets(rows, home, current_pid=None, protected_pids=()):
    """Names and program purpose identify scope; no host/PID provenance is required."""
    current_pid = os.getpid() if current_pid is None else current_pid
    by_id = {int(row["ProcessId"]): row for row in rows}
    protected = {current_pid, *protected_pids}
    # UAC may parent the elevated helper under a broker, outside the console's
    # process tree. Protect the initiating console's ancestors too: on Windows
    # the venv pythonw redirector owns the base interpreter in a kill-on-close
    # job, so killing that parent also kills the otherwise protected window.
    for root_pid in tuple(protected):
        visited = set()
        ancestor = by_id.get(root_pid)
        while ancestor:
            pid = int(ancestor["ProcessId"])
            if pid in visited:
                break
            visited.add(pid)
            parent = int(ancestor.get("ParentProcessId") or 0)
            if not parent:
                break
            protected.add(parent)
            ancestor = by_id.get(parent)
    roots = tuple(normalized(root) for root in (
        home, REPO, "D:/QQ_Bot_2", "D:/AIPoweredTRPGinQQ/AIPoweredTRPG"))
    selected = {}
    for pid, row in by_id.items():
        name = str(row.get("Name") or "").casefold()
        if pid in protected or name in PROTECTED_NAMES:
            continue
        command = normalized(row.get("CommandLine"))
        executable = normalized(row.get("ExecutablePath"))
        if "\\openai\\codex\\runtimes\\" in executable or "\\.cache\\codex-runtimes\\" in executable:
            protected.add(pid)
            continue
        reason = None
        if name in ROOT_NAMES or name.startswith("napcatwinboot"):
            reason = "QQ / NapCat"
        elif name in PYTHON_NAMES:
            if "-m persona_runtime" in command or "-m astrbot" in command or name == "astrbot.exe":
                reason = "persona / AstrBot runtime"
            elif any(root in command for root in roots) and any(script in command for script in (
                    "botmanager.py", "service_host.py", "startrun.py", "main.py")):
                reason = "related manager / service host"
            elif "\\uv\\tools\\astrbot\\scripts\\" in executable or any(
                    root + "\\components\\astrbot\\.venv\\scripts\\" in executable for root in roots):
                reason = "dedicated AstrBot environment"
        elif name in {"node.exe", "pnpm.exe", "npm.exe", "bun.exe"}:
            if any(root in command for root in roots) and any(marker in command for marker in (
                    "apps\\server", "apps\\web", "dist\\index.js", "vite", "pnpm")):
                reason = "related engine / frontend runtime"
        elif name in {"cmd.exe", "powershell.exe", "pwsh.exe"}:
            if any(root in command for root in roots) and any(marker in command for marker in (
                    "launcher", "start-bot", "start-manager", "service_host", "persona_runtime.host")):
                reason = "related launcher"
        if reason:
            selected[pid] = {"pid": pid, "name": row.get("Name", ""), "reason": reason}
    # Include related workers and detached child programs, without closing browsers/system shells.
    changed = True
    while changed:
        changed = False
        for pid, row in by_id.items():
            name = str(row.get("Name") or "").casefold()
            if pid not in selected and pid not in protected and name not in PROTECTED_NAMES and int(row.get("ParentProcessId") or 0) in selected:
                selected[pid] = {"pid": pid, "name": row.get("Name", ""), "reason": "related child process"}
                changed = True
    # Stop supervisors before the clients they can restart.
    return sorted(selected.values(), key=lambda row: (row["name"].casefold() in ROOT_NAMES,
                                                     row["reason"] == "related child process", row["pid"]))


def process_snapshot():
    script = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
              "@(Get-CimInstance Win32_Process -ErrorAction Stop | "
              "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine) | ConvertTo-Json -Compress")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=15, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeError("无法读取进程列表，未宣称已停止。")
    rows = json.loads(result.stdout)
    return rows if isinstance(rows, list) else [rows]


def kill_pid(pid):
    # No /IM, no blanket Python/Node kill, no ancestry/instance ownership proof.
    # Children are individually selected, so unrelated browser processes are preserved.
    result = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True,
                            creationflags=NO_WINDOW, timeout=10)
    return result.returncode == 0


def windows_argv(command):
    """Read Windows argv without evaluating a shell command or its contents."""
    if not isinstance(command, str) or not command.strip():
        return []
    if os.name != "nt":
        return []  # QQ emergency process control is Windows-only; fail closed.
    import ctypes
    count = ctypes.c_int()
    split = ctypes.windll.shell32.CommandLineToArgvW
    split.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(ctypes.c_wchar_p)
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    args = split(command.strip(), ctypes.byref(count))
    if not args:
        raise RuntimeError("无法解析进程启动参数，未执行 QQ 强停。")
    try:
        return [args[index] for index in range(count.value)]
    finally:
        free(ctypes.cast(args, ctypes.c_void_p))


def qq_host_argv(argv):
    """Only the real Python module/role options identify a PCAR QQ host."""
    args = list(argv[1:])
    while args:
        arg = args.pop(0)
        if arg == "-m":
            if not args or args.pop(0) != "persona_runtime.host":
                return False
            break
        if arg in {"-W", "-X"} and args:
            args.pop(0)
        elif arg not in {"-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-u", "-v", "-q"}:
            return False  # A script, -c payload, or unknown option is not a host.
    else:
        return False
    role = None
    while args:
        arg = args.pop(0)
        if arg in {"--role", "--instance", "--console-token"} and args:
            value = args.pop(0)
            if arg == "--role":
                role = value
        elif arg.startswith("--role="):
            role = arg.split("=", 1)[1]
        elif not arg.startswith(("--instance=", "--console-token=")):
            return False
    return role == "napcat"


def qq_launcher_argv(name, argv):
    """Match the actual entry script, never log paths or arbitrary arguments."""
    args = list(argv[1:])
    if name in {"node.exe", "bun.exe"}:
        while args:
            arg = args.pop(0)
            if arg in {"-r", "--require", "--import", "--loader"} and args:
                args.pop(0)
            elif arg in {"--no-warnings", "--trace-warnings", "--inspect", "--inspect-brk"} or arg.startswith(("--inspect=", "--inspect-brk=", "--require=", "--import=", "--loader=")):
                continue
            elif name == "bun.exe" and arg == "run":
                continue
            elif arg == "--" and args:
                return normalized(args[0]).rsplit("\\", 1)[-1] in {"napcat.mjs", "napcat.js", "loadnapcat.js"}
            else:
                return normalized(arg).rsplit("\\", 1)[-1] in {"napcat.mjs", "napcat.js", "loadnapcat.js"}
        return False
    if name in {"powershell.exe", "pwsh.exe"}:
        while args:
            arg = args.pop(0)
            option = arg.casefold()
            if option in {"-noprofile", "-nologo", "-noninteractive"}:
                continue
            if option in {"-executionpolicy", "-windowstyle"} and args:
                args.pop(0)
                continue
            if option in {"-file", "-f"} and args:
                arg = args.pop(0)
            # -Command / -EncodedCommand is arbitrary code, not a script path.
            return qq_launcher_file(arg)
        return False
    if name == "cmd.exe":
        while args and args[0].casefold() in {"/d", "/q", "/s"}:
            args.pop(0)
        return bool(args and args.pop(0).casefold() in {"/c", "/k"} and args and qq_launcher_file(args[0]))
    # npm/pnpm script names do not prove what executable they launch.
    return False


def qq_launcher_file(value):
    parts = normalized(value).split("\\")
    name = parts[-1]
    return (name in ROOT_NAMES or name in {"napcat.bat", "napcat.cmd", "napcat.ps1"}
            or name in {"launcher.bat", "launcher.cmd", "launcher.ps1"}
            and any(part in {"napcat", "napcat-shell", "napcat.shell"} for part in parts[:-1]))


def select_qq_targets(rows, home, current_pid=None):
    """QQ-only emergency scope, independent of ownership and port records."""
    current_pid = os.getpid() if current_pid is None else current_pid
    by_id = {int(row["ProcessId"]): row for row in rows}
    protected = {current_pid}
    ancestor = by_id.get(current_pid)
    while ancestor:
        parent = int(ancestor.get("ParentProcessId") or 0)
        if not parent or parent in protected:
            break
        protected.add(parent)
        ancestor = by_id.get(parent)
    selected = {}
    for pid, row in by_id.items():
        name = str(row.get("Name") or "").casefold()
        argv = windows_argv(row.get("CommandLine"))
        qq_host = name in PYTHON_NAMES and qq_host_argv(argv)
        executable = normalized(row.get("ExecutablePath"))
        # Preserve all runtime/bridge/controller processes, even if a QQ parent
        # happens to appear in an old or recycled process tree.
        if (name in PROTECTED_NAMES or "\\openai\\codex\\runtimes\\" in executable
                or "\\.cache\\codex-runtimes\\" in executable
                or name in PYTHON_NAMES and not qq_host):
            protected.add(pid)
        if pid in protected:
            continue
        reason = None
        if name in ROOT_NAMES or name.startswith("napcatwinboot"):
            reason = "QQ / NapCat"
        elif qq_host:
            reason = "QQ connection host"
        elif name in {"node.exe", "pnpm.exe", "npm.exe", "bun.exe", "cmd.exe", "powershell.exe", "pwsh.exe"}:
            if qq_launcher_argv(name, argv):
                reason = "QQ / NapCat launcher or worker"
        if reason:
            selected[pid] = {"pid": pid, "name": row.get("Name", ""), "reason": reason}
    changed = True
    while changed:
        changed = False
        for pid, row in by_id.items():
            if (pid not in selected and pid not in protected
                    and int(row.get("ParentProcessId") or 0) in selected):
                selected[pid] = {"pid": pid, "name": row.get("Name", ""), "reason": "QQ / NapCat child process"}
                changed = True
    return sorted(selected.values(), key=lambda row: (row["name"].casefold() in ROOT_NAMES, row["pid"]))


def force_stop_qq_processes(paths, *, snapshot=None, kill=None, pause=None, report_file=None):
    """Run inside the elevated helper; never stop persona/AstrBot services."""
    snapshot, kill, pause = snapshot or process_snapshot, kill or kill_pid, pause or time.sleep
    stopped, attempted = {}, {}
    for _ in range(5):
        targets = select_qq_targets(snapshot(), paths.home)
        if not targets:
            pause(.5)
            if not select_qq_targets(snapshot(), paths.home):
                break
            continue
        for target in targets:
            attempted[target["pid"]] = target
            if kill(target["pid"]):
                stopped[target["pid"]] = target
        pause(.5)
    remaining = select_qq_targets(snapshot(), paths.home)
    report = {"at": timestamp(), "stopped": list(stopped.values()), "attempted": list(attempted.values()),
              "remaining": remaining, "success": not remaining, "scope": "QQ/NapCat only; persona and AstrBot retained"}
    report_file = report_file or paths.home / "logs" / ("force-stop-qq-" + identifier() + ".json")
    atomic_json(report_file, report)
    return report | {"report_file": str(report_file)}


def is_administrator():
    import ctypes
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def force_stop_qq_elevated(paths, notify=lambda message: None):
    """Always require administrator rights, with a Windows UAC prompt if needed."""
    import base64
    import sys
    report_file = paths.home / "logs" / ("force-stop-qq-" + identifier() + ".json")
    notify("正在以管理员权限强制停止全部 QQ / NapCat；人格服务与消息服务保持运行。")
    if is_administrator():
        report = force_stop_qq_processes(paths, report_file=report_file)
        report["elevated"] = True
        atomic_json(report_file, report)
        return report
    if os.name != "nt":
        raise RuntimeError("QQ 管理员强停仅支持 Windows。")
    # Use a fixed, independently validated helper. No interpolated shell code
    # from process command lines, settings, or account data.
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    arguments = subprocess.list2cmdline(["-B", str(REPO / "scripts/force_stop_qq.py"),
                                       "--home", str(paths.home), "--report", str(report_file)])
    script = ("$ErrorActionPreference='Stop'; try { $p=Start-Process -FilePath " + quote(sys.executable)
              + " -ArgumentList " + quote(arguments)
              + " -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode } catch { exit 1 }")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, creationflags=NO_WINDOW)
    if not report_file.exists():
        raise RuntimeError("QQ 强停未完成：管理员授权被取消或提升失败；未宣称已停止，其他服务保持原状。")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    if not report.get("elevated") or result.returncode and report.get("success"):
        raise RuntimeError("QQ 强停管理员执行结果异常，请查看报告：" + str(report_file))
    return report | {"report_file": str(report_file)}


def force_stop_related(paths, notify=lambda message: None, *, snapshot=None, kill=None, pause=None, protected_pids=()):
    snapshot = snapshot or process_snapshot
    kill = kill or kill_pid
    pause = pause or time.sleep
    stopped, attempted = {}, {}
    notify("正在强制停止 QQ、NapCat 和相关服务，不等待排空或校验托管来源。")
    for _ in range(5):
        targets = select_targets(snapshot(), paths.home, protected_pids=protected_pids)
        if not targets:
            pause(.5)
            if not select_targets(snapshot(), paths.home, protected_pids=protected_pids):
                break
            continue
        for target in targets:
            attempted[target["pid"]] = target
            if kill(target["pid"]):
                stopped[target["pid"]] = target
        pause(.5)
    remaining = select_targets(snapshot(), paths.home, protected_pids=protected_pids)
    report = {"at": timestamp(), "stopped": list(stopped.values()), "attempted": list(attempted.values()),
              "remaining": remaining, "success": not remaining,
              "scope": "all QQ/NapCat and related persona/TRPG/AstrBot programs; current controller retained"}
    report_file = paths.home / "logs" / ("force-stop-" + identifier() + ".json")
    atomic_json(report_file, report)
    report["report_file"] = str(report_file)
    return report


def force_stop_all_elevated(paths, notify=lambda message: None):
    """Console exit requires an administrator sweep, including orphan services."""
    import base64
    import sys
    if is_administrator():
        report = force_stop_related(paths, notify)
        report['elevated'] = True
        atomic_json(Path(report['report_file']), report)
        return report
    if os.name != 'nt':
        raise RuntimeError('控制台关闭强停仅支持 Windows。')
    report_file = paths.home / 'logs' / ('console-stop-' + identifier() + '.json')
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    args = subprocess.list2cmdline(['-B', str(REPO / 'scripts/force_stop_services.py'),
                                  '--home', str(paths.home), '--report', str(report_file),
                                  '--controller-pid', str(os.getpid())])
    script = ("$ErrorActionPreference='Stop'; try { $p=Start-Process -FilePath " + quote(sys.executable)
              + ' -ArgumentList ' + quote(args)
              + ' -Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode } catch { exit 1 }')
    notify('正在关闭全部服务；如出现 Windows 管理员授权，请确认。')
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                            capture_output=True, creationflags=NO_WINDOW)
    if not report_file.exists():
        raise RuntimeError('管理员强停未完成；控制台保持打开，请重新关闭并完成 Windows 授权。')
    report = json.loads(report_file.read_text(encoding='utf-8'))
    if not report.get('elevated') or result.returncode and report.get('success'):
        raise RuntimeError('强停执行结果异常，请查看报告：' + str(report_file))
    return report | {'report_file':str(report_file)}
