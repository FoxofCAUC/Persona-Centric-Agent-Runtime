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


def select_targets(rows, home, current_pid=None):
    """Names and program purpose identify scope; no host/PID provenance is required."""
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


def force_stop_related(paths, notify=lambda message: None, *, snapshot=None, kill=None, pause=None):
    snapshot = snapshot or process_snapshot
    kill = kill or kill_pid
    pause = pause or time.sleep
    stopped, attempted = {}, {}
    notify("正在强制停止 QQ、NapCat 和相关服务，不等待排空或校验托管来源。")
    for _ in range(5):
        targets = select_targets(snapshot(), paths.home)
        if not targets:
            pause(.5)
            if not select_targets(snapshot(), paths.home):
                break
            continue
        for target in targets:
            attempted[target["pid"]] = target
            if kill(target["pid"]):
                stopped[target["pid"]] = target
        pause(.5)
    remaining = select_targets(snapshot(), paths.home)
    report = {"at": timestamp(), "stopped": list(stopped.values()), "attempted": list(attempted.values()),
              "remaining": remaining, "success": not remaining,
              "scope": "all QQ/NapCat and related persona/TRPG/AstrBot programs; current controller retained"}
    report_file = paths.home / "logs" / ("force-stop-" + identifier() + ".json")
    atomic_json(report_file, report)
    report["report_file"] = str(report_file)
    return report
