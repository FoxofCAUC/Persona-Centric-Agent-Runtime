"""Detached service host, independent of the lifetime of the desktop window."""
import argparse
import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .config import ConfigStore, atomic_json
from .deployment import NO_WINDOW, child_environment, write_component_config
from .instance import InstanceLock
from .lifecycle import listener, process_info, same_process
from .logging_safe import setup, secure_astrbot_logging
from .models import RuntimeErrorBase
from .paths import RuntimePaths


async def run_astrbot(paths, stop):
    bot = paths.home / "components/astrbot/bot"
    os.chdir(bot)
    sys.path.insert(0, str(bot))
    os.environ.update(ASTRBOT_ROOT=str(bot), ASTRBOT_CLI="1")
    from astrbot.cli.commands.cmd_run import run_astrbot as run
    secure_astrbot_logging(paths)
    task = asyncio.create_task(run(bot))
    try:
        while not task.done() and not stop.exists():
            await asyncio.sleep(.25)
        if stop.exists():
            task.cancel()
        await asyncio.gather(task, return_exceptions=False)
    except asyncio.CancelledError:
        pass
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def installed_qq():
    import winreg
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ")
    try:
        executable = Path(winreg.QueryValueEx(key, "UninstallString")[0].strip('"')).parent / "QQ.exe"
    finally:
        winreg.CloseKey(key)
    if not executable.exists():
        raise RuntimeErrorBase("Installed QQ executable not found")
    return executable


def normal_close(identity):
    if not same_process(identity):
        raise RuntimeErrorBase("QQ process identity changed; normal close refused")
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    def callback(window, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value == identity["ProcessId"]:
            user32.PostMessageW(window, 0x0010, 0, 0)
        return True
    user32.EnumWindows(callback_type(callback), 0)


def run_napcat(paths, cfg, record, stop):
    root = paths.home / "components/napcat"
    qq = installed_qq()
    before = listener(cfg["napcat_port"])
    if before:
        raise RuntimeErrorBase("NapCat port already occupied")
    env = child_environment(paths)
    for key, name in (("NAPCAT_PATCH_PACKAGE", "qqnt.json"), ("NAPCAT_LOAD_PATH", "loadNapCat.js"),
                      ("NAPCAT_INJECT_PATH", "NapCatWinBootHook.dll"), ("NAPCAT_LAUNCHER_PATH", "NapCatWinBootMain.exe"),
                      ("NAPCAT_MAIN_PATH", "napcat.mjs")):
        env[key] = str(root / name)
    launched = time.time()
    loader = subprocess.Popen([str(root / "NapCatWinBootMain.exe"), str(qq), str(root / "NapCatWinBootHook.dll"), cfg["bot_qq"]],
                              cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    record["loader"] = process_info(loader.pid)
    record_file = paths.home / "runtime/napcat.json"
    atomic_json(record_file, record)
    while not stop.exists():
        proof = listener(cfg["napcat_port"])
        if proof and Path(proof.get("ExecutablePath") or "").resolve() == qq.resolve():
            from datetime import datetime
            created = datetime.fromisoformat(proof["Created"].replace("Z", "+00:00")).timestamp()
            if created >= launched-1:
                record["qq_listener"] = proof
                parent = proof
                for _ in range(6):
                    candidate = process_info(parent.get("ParentProcessId"))
                    if not candidate or Path(candidate.get("ExecutablePath") or "").resolve() != qq.resolve():
                        break
                    parent_created = datetime.fromisoformat(candidate["Created"].replace("Z", "+00:00")).timestamp()
                    if parent_created < launched-1:
                        break
                    parent = candidate
                record["qq_parent"] = parent
                atomic_json(record_file, record)
                break
        if loader.poll() not in (None, 0):
            raise RuntimeErrorBase("NapCat loader failed")
        time.sleep(1)
    while not stop.exists():
        if record.get("qq_listener") and not same_process(record["qq_listener"]):
            return
        time.sleep(1)
    if record.get("qq_parent"):
        normal_close(record["qq_parent"])
        deadline = time.monotonic()+25
        while time.monotonic() < deadline and same_process(record["qq_listener"]):
            time.sleep(.5)
        if same_process(record["qq_listener"]):
            raise RuntimeErrorBase("QQ did not finish normal shutdown")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("service", "astrbot", "napcat", "configure"), required=True)
    parser.add_argument("--instance")
    args = parser.parse_args()
    paths = RuntimePaths.from_environment()
    cfg = ConfigStore(paths).initialize()
    if args.role == "configure":
        write_component_config(paths)
        return
    if args.instance != cfg["instance_id"]:
        raise RuntimeErrorBase("Instance identity mismatch")
    paths.initialize()
    lock = InstanceLock(paths.home / "runtime" / (args.role + ".host.lock"))
    stop = paths.home / "runtime" / (args.role + ".stop")
    stop.unlink(missing_ok=True)
    record = {"instance_id": cfg["instance_id"], "role": args.role, "host": process_info(os.getpid())}
    atomic_json(paths.home / "runtime" / (args.role + ".json"), record)
    try:
        setup(paths, args.role)
        if args.role == "service":
            from .server import serve
            asyncio.run(serve(paths))
        elif args.role == "astrbot":
            asyncio.run(run_astrbot(paths, stop))
        else:
            run_napcat(paths, cfg, record, stop)
    except Exception:
        import logging
        logging.getLogger("host").exception("host_failure")
        raise SystemExit(1) from None
    finally:
        lock.close()


if __name__ == "__main__":
    main()
