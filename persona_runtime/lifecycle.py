"""Project-owned services, process identity and safe Windows lifecycle."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Protocol

from .config import ConfigStore, atomic_json
from .deployment import NO_WINDOW, REPO, child_environment, render_components
from .desktop_client import LocalClient
from .models import RuntimeErrorBase


def ps_json(script):
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=8)
    return json.loads(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def process_info(pid):
    if type(pid) is not int or pid <= 0:
        return None
    return ps_json("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                   f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
                   "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,"
                   "@{n='Created';e={$_.CreationDate.ToUniversalTime().ToString('o')}} | ConvertTo-Json -Compress")


def same_process(saved):
    if not saved:
        return False
    actual = process_info(saved.get("ProcessId"))
    return bool(actual and all(actual.get(k) == saved.get(k) for k in ("ProcessId", "Created", "ExecutablePath")))


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), .2):
            return True
    except OSError:
        return False


def listener(port):
    value = ps_json(f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction SilentlyContinue | "
                    "Select-Object -First 1 -ExpandProperty OwningProcess | ConvertTo-Json -Compress")
    return process_info(value) if type(value) is int else None


class ServiceAdapter(Protocol):
    def health(self) -> dict: ...
    def start(self): ...
    def drain(self): ...
    def stop(self): ...


@dataclass
class ManagedService:
    controller: "ServiceController"
    role: str

    def health(self):
        return self.controller.health(self.role)

    def start(self):
        return self.controller.start(self.role)

    def drain(self):
        if self.role == "service":
            return self.controller.client.call("POST", "/v1/control/drain", {})

    def stop(self):
        return self.controller.stop(self.role)


class ServiceController:
    def __init__(self, paths, notify=lambda message: None):
        self.paths = paths
        self.config = ConfigStore(paths)
        self.client = LocalClient(self.config)
        self.notify = notify
        self.processes = {}
        self.stop_generation = 0
        self.force_stopping = threading.Event()
        self.adapters = {role: ManagedService(self, role) for role in ("service", "astrbot", "napcat")}

    def record_path(self, role):
        return self.paths.home / "runtime" / (role + ".json")

    def record(self, role):
        try:
            return json.loads(self.record_path(role).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def owned(self, role):
        saved = self.record(role)
        if not saved or saved.get("instance_id") != self.config.load()["instance_id"]:
            return False
        identity = saved.get("host")
        actual = process_info(identity.get("ProcessId")) if identity else None
        if not actual or not same_process(identity):
            return False
        command = actual.get("CommandLine") or ""
        return "persona_runtime.host" in command and f"--role {role}" in command and saved["instance_id"] in command

    def health(self, role):
        cfg = self.config.load()
        port = cfg[{"service": "service_port", "astrbot": "astrbot_port", "napcat": "napcat_port"}[role]]
        owned = self.owned(role)
        listening = port_open(port)
        result = {"role": role, "owned": owned, "port": port, "state": "启动中" if owned else "已停止"}
        if listening and not owned:
            return result | {"state": "外部程序占用", "reason": "此端口不属于本项目，不接管或停止"}
        if not listening:
            return result
        if role == "service":
            try:
                proof = self.client.call("GET", "/v1/health")
                if proof.get("instance_id") == cfg["instance_id"] and proof.get("service") == "persona-runtime":
                    return result | {"state": "排空中" if proof["draining"] else "运行", "pending": proof["pending"]}
            except RuntimeErrorBase:
                pass
        elif role == "astrbot":
            try:
                proof = json.loads((self.paths.home / "runtime/astrbot-health.json").read_text(encoding="utf-8"))
                saved = self.record(role)
                if time.time()-proof["at"] < 15 and proof["instance_id"] == cfg["instance_id"] and proof["pid"] == saved["host"]["ProcessId"]:
                    return result | {"state": "运行", "login": proof["verified"], "onebot": proof["online"]}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        else:
            saved = self.record(role)
            if saved and same_process(saved.get("qq_listener")):
                return result | {"state": "运行", "web_ready": True}
        return result | {"state": "等待验证"}

    def management_url(self, role):
        """Dashboard access does not depend on QQ/plugin heartbeat health."""
        if role not in ("astrbot", "napcat"):
            raise ValueError("此服务没有管理页面。")
        if not self.owned(role):
            raise ValueError("尚未验证此页面属于本项目，请先启动对应服务。")
        cfg = self.config.load()
        saved = self.record(role) or {}
        expected = saved.get("host" if role == "astrbot" else "qq_listener")
        actual = listener(cfg[role + "_port"])
        if not actual:
            raise ValueError("管理页面尚未监听，请等待启动完成后重试。")
        if not expected or any(actual.get(key) != expected.get(key)
                               for key in ("ProcessId", "Created", "ExecutablePath")):
            raise ValueError("管理页面端口由其他进程占用，拒绝打开。")
        url = f"http://127.0.0.1:{cfg[role + '_port']}"
        if role == "napcat":
            from urllib.parse import quote
            try:
                web_config = json.loads((self.paths.home / "components/napcat/config/webui.json").read_text(encoding="utf-8-sig"))
                token = web_config["token"]
                if not isinstance(token, str) or not token:
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError("无法读取本项目 QQ 管理页凭据，请检查连接配置。") from None
            url += "/webui/?token=" + quote(token, safe="")
        return url

    def start(self, role):
        if self.force_stopping.is_set():
            raise RuntimeErrorBase("强制停止正在执行，不能同时启动服务。")
        generation = self.stop_generation
        if role not in self.adapters:
            raise RuntimeErrorBase("Unknown service role")
        health = self.health(role)
        if health["state"] == "运行":
            return health
        if health["owned"] or port_open(health["port"]):
            raise RuntimeErrorBase("服务仍在运行或端口被占用，请先查看诊断。")
        cfg = self.config.load()
        if role == "napcat" and port_open(6099):
            raise RuntimeErrorBase("旧项目 QQ/NapCat 仍在运行，请从原入口正常停止后再启动本项目。")
        if role == "napcat":
            external = ps_json("Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | Select-Object ProcessId | ConvertTo-Json -Compress")
            if external:
                raise RuntimeErrorBase("检测到其他 QQ 实例；为避免共享主进程，请从原入口正常退出后再启动本项目。")
        if role != "service" and not (cfg["owner_qq"] and cfg["bot_qq"]):
            raise RuntimeErrorBase("请先填写本人 QQ 与 Bot QQ。")
        if role == "astrbot":
            if port_open(cfg["onebot_port"]):
                raise RuntimeErrorBase("OneBot 端口被其他程序占用。")
            render_components(self.paths)
        python = (self.paths.home / "components/astrbot/.venv/Scripts/python.exe" if role == "astrbot"
                  else self.paths.home / ".venv/Scripts/python.exe")
        if not python.exists():
            # An explicitly overridden test home still uses the installed project environment.
            python = Path("D:/Persona-Centric Agent Runtime/.venv/Scripts/python.exe") if role == "service" else python
        if not python.exists():
            raise RuntimeErrorBase("独立运行环境未准备，请先准备组件。")
        self.notify("正在启动：" + role)
        if generation != self.stop_generation or self.force_stopping.is_set():
            raise RuntimeErrorBase("启动已被强制停止操作取消。")
        self.processes[role] = subprocess.Popen([str(python), "-B", "-m", "persona_runtime.host", "--role", role,
                          "--instance", cfg["instance_id"]], cwd=REPO, env=child_environment(self.paths),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            if generation != self.stop_generation:
                raise RuntimeErrorBase("启动已被强制停止操作取消。")
            time.sleep(.5)
            state = self.health(role)
            if state["state"] == "运行":
                return state
        raise RuntimeErrorBase("启动未完成；请检查组件、端口或 QQ 登录，已启动的进程保留供诊断。")

    def start_all(self, cancel=None):
        generation = self.stop_generation
        for role in ("service", "astrbot", "napcat"):
            if generation != self.stop_generation:
                raise RuntimeErrorBase("后续启动步骤已被强制停止操作取消。")
            if cancel and cancel.is_set():
                return
            self.start(role)
        # Login is separate from page readiness and the existence of a listener.
        return self.health("astrbot")

    def stop(self, role):
        if not self.owned(role):
            if self.health(role)["state"] == "已停止":
                return
            raise RuntimeErrorBase("进程归属未验证，不执行停止。")
        self.notify("正在停止：" + role)
        if role == "service":
            self.client.call("POST", "/v1/control/drain", {})
            self.client.call("POST", "/v1/control/stop", {}, timeout=370)
        else:
            (self.paths.home / "runtime" / (role + ".stop")).touch()
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if not self.owned(role) and not port_open(self.health(role)["port"]):
                if role in self.processes:
                    self.processes.pop(role).wait(timeout=2)
                return
            time.sleep(.5)
        raise RuntimeErrorBase("正常停止尚未完成；不会强制结束其他程序。")

    def stop_all(self, include_qq=False):
        if self.owned("service"):
            self.client.call("POST", "/v1/control/drain", {})
            deadline = time.monotonic()+370
            while time.monotonic() < deadline:
                status = self.client.call("GET", "/v1/health")
                if not status["pending"]:
                    break
                time.sleep(.5)
            else:
                raise RuntimeErrorBase("请求尚未排空，保留服务，不强制中断。")
            # Let an already-running bridge finish platform delivery before canceling it.
            deadline = time.monotonic()+15
            while self.owned("astrbot") and time.monotonic() < deadline:
                rows = self.client.manage("delivery.pending")
                if not any(r["status"] == "completed" and r["state"] in ("pending", "sending") for r in rows):
                    break
                time.sleep(.5)
        for role in ("astrbot", "service") + (("napcat",) if include_qq else ()):
            if self.owned(role):
                self.stop(role)

    def snapshot(self):
        return {role: self.health(role) for role in self.adapters}

    def force_stop_all(self):
        from .emergency import force_stop_related
        self.force_stopping.set()
        self.stop_generation += 1
        try:
            return force_stop_related(self.paths, self.notify)
        finally:
            self.force_stopping.clear()
