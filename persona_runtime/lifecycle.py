"""Project-owned services, process identity and safe Windows lifecycle."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from typing import Protocol

from .config import ConfigStore, atomic_json
from .deployment import NO_WINDOW, REPO, child_environment, render_components, component_python
from .desktop_client import LocalClient
from .models import RuntimeErrorBase, identifier, timestamp
from .instance import InstanceLock, current_process_identity, process_identity_alive


def read_console(paths):
    try:
        value = json.loads((paths.home / "runtime/console.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def require_console(paths, token=None):
    """A stale file or a recycled PID cannot authorize background services."""
    lease = read_console(paths)
    if (not lease or lease.get("state") != "active" or not lease.get("token")
            or token is not None and lease["token"] != token
            or lease.get("instance_id") != ConfigStore(paths).load()["instance_id"]
            or not process_identity_alive(lease.get("process"))):
        raise RuntimeErrorBase("控制窗口未运行或已关闭，请先打开 PCAR 工作台。")
    return lease


def ps_json(script):
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=8)
    return json.loads(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def process_info(pid):
    if type(pid) is not int or pid <= 0:
        return None
    result=ps_json("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                   f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' | "
                   "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine,"
                   "@{n='Created';e={$_.CreationDate.ToUniversalTime().ToString('o')}} | ConvertTo-Json -Compress")
    if result:
        from .instance import ProcessIdentityHandle
        try:
            handle=ProcessIdentityHandle(pid)
            try:result['native']=handle.identity
            finally:handle.close()
        except RuntimeErrorBase:return None
    return result


def same_process(saved):
    if not saved:
        return False
    from .instance import native_identity_matches
    if saved.get('native'):return native_identity_matches(saved)
    from .instance import ProcessIdentityHandle
    try:
        handle=ProcessIdentityHandle(saved.get('ProcessId'))
        try:
            if not handle.alive():return False
        finally:handle.close()
    except (OSError,ValueError,TypeError,RuntimeErrorBase):return False
    # Old records use CIM's microsecond precision, not native 100ns FILETIME.
    actual=process_info(saved.get('ProcessId'))
    return bool(actual and all(actual.get(k)==saved.get(k) for k in ('ProcessId','Created','ExecutablePath')))


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
        self.console_lease = None
        self.console_lock = None
        self.app_processes = None
        self.closing = threading.Event()
        self.lifecycle_lock = threading.RLock()
        self.login_health = None
        self.adapters = {role: ManagedService(self, role) for role in ("service", "astrbot", "napcat")}

    def start_console(self):
        if self.console_lease is not None:
            return require_console(self.paths, self.console_lease["token"])
        lock = InstanceLock(self.paths.home / "runtime/console.lock")
        try:
            previous = read_console(self.paths)
            if previous and previous.get("state") == "active" and process_identity_alive(previous.get("process")):
                raise RuntimeErrorBase("已有 PCAR 控制窗口运行，请返回现有窗口。")
            from .app_processes import AppProcesses
            self.app_processes = AppProcesses()
            persona_id = None
            database = self.paths.home / 'data/database/runtime.sqlite3'
            if database.exists():
                import sqlite3
                from contextlib import closing
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
                    row = db.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()
                    persona_id = row[0] if row else None
            cfg=self.config.load()
            if cfg['runtime']['qq_persona_id']:persona_id=cfg['runtime']['qq_persona_id']
            elif persona_id:
                cfg['runtime']['qq_persona_id']=persona_id
                cfg['runtime']['revision']+=1
                atomic_json(self.config.file,cfg)
            try:last_exit=json.loads((self.paths.home/'runtime/last-exit.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):last_exit=None
            exit_matches=bool(previous and last_exit and last_exit.get('console_token')==previous.get('token'))
            previous_exit_unconfirmed=bool(previous and (not exit_matches or last_exit.get('qq_was_running') and not last_exit.get('logout_confirmed')))
            lease = {"token": identifier(), "instance_id": self.config.load()["instance_id"],
                     "process": current_process_identity(), "state": "active", "at": timestamp(),
                     "qq_persona_id": persona_id,
                     'previous_exit_unconfirmed':previous_exit_unconfirmed,
                     'login_confirmation_required':bool(cfg['bot_qq'] and (
                         previous and previous.get('state')!='closed' or
                         previous_exit_unconfirmed)),
                     "previous_stale": bool(previous and previous.get("state") != "closed")}
            atomic_json(self.paths.home / "runtime/console.json", lease)
            self.console_lease, self.console_lock = lease, lock
            return lease
        except BaseException:
            if self.app_processes is not None:
                self.app_processes.close();self.app_processes=None
            lock.close()
            raise

    def end_console(self):
        lease = self.console_lease
        try:
            current = read_console(self.paths)
            if lease and current and current.get("token") == lease["token"]:
                atomic_json(self.paths.home / "runtime/console.json", current | {"state": "closed", "closed_at": timestamp()})
        finally:
            if self.app_processes is not None:
                self.app_processes.close();self.app_processes=None
            if self.console_lock is not None:
                self.console_lock.close()
            self.console_lock = self.console_lease = None

    def has_resident_processes(self):
        instance_id = self.config.load()["instance_id"]
        for role in self.adapters:
            saved = self.record(role)
            if saved and saved.get("instance_id") == instance_id:
                for key in ("host", "loader", "qq_listener", "qq_parent"):
                    if same_process(saved.get(key)):
                        return True
        return False

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
        if not identity or not same_process(identity):
            return False
        command = identity.get("CommandLine") or ""
        return "persona_runtime.host" in command and f"--role {role}" in command and saved["instance_id"] in command

    def health(self, role):
        cfg = self.config.load()
        if role == 'napcat' and cfg['qq_backend'] == 'snowluma':
            bridge = self.health('astrbot')
            enabled = cfg['runtime']['components']['napcat']
            verified = bridge.get('login')
            reason = ('backend_mismatch' if bridge.get('backend_matches') is False else
                      'account_mismatch' if bridge.get('account_matches') is False else 'login_required')
            return {'role': role, 'backend': 'snowluma', 'external': True, 'owned': False,
                    'port': cfg['onebot_port'], 'state': '已停止' if not enabled else
                    '运行' if verified is True and bridge.get('account_online') is True else '等待外部连接',
                    'account_logged_in': verified, 'login_required': verified is not True,
                    'login_reason': None if verified is True else reason,
                    'backend_matches': bridge.get('backend_matches')}
        port = cfg[{"service": "service_port", "astrbot": "astrbot_port", "napcat": "napcat_port"}[role]]
        owned = self.owned(role)
        listening = port_open(port)
        result = {"role": role, "owned": owned, "port": port, "state": "启动中" if owned else "已停止"}
        if role=='napcat' and not owned and self.console_lease and self.console_lease.get('login_confirmation_required'):
            return result|{'state':'等待登录确认','reason':'上次注销未确认；请明确点击启动服务后重新登录。'}
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
                if proof["instance_id"] != cfg["instance_id"] or proof["pid"] != saved["host"]["ProcessId"]:
                    return result | {"state":"等待验证", "login":None, "onebot":None,
                                     "reason":"桥接心跳与当前消息服务不匹配，QQ 登录状态未知。"}
                age = time.time()-proof["at"]
                if age >= 15 or age < -5:
                    return result | {"state":"等待验证", "login":None, "onebot":None,
                                     "heartbeat_stale":True, "heartbeat_age_seconds":max(0,round(age)),
                                     "reason":"桥接心跳已过期，QQ 登录状态未知；请检查消息服务。"}
                if cfg['qq_backend'] == 'snowluma' and proof.get('backend') != 'snowluma':
                    return result | {'state': '等待验证', 'login': None, 'onebot': None,
                                     'reason': '等待当前SnowLuma连接的桥接心跳。'}
                if proof.get('error_type'):
                    return result | {"state":"等待验证", "login":None, "onebot":None,
                                     "reason":"桥接状态检查失败，QQ 登录状态未知；请查看元数据诊断日志。"}
                return result | {"state": "运行", "login": proof["verified"], "onebot": proof["online"],
                    "onebot_connected":proof.get('connected'),"account_online":proof['online'],
                    "backend_matches": proof.get('backend_matches'), "account_matches": proof.get('account_matches')}
            except (OSError, ValueError, KeyError, TypeError):
                return result | {"state":"等待验证", "login":None, "onebot":None,
                                 "reason":"桥接心跳暂不可读取，QQ 登录状态未知；请检查消息服务。"}
        else:
            saved = self.record(role)
            if saved and same_process(saved.get("qq_listener")):
                detail={'account_logged_in':None,'login_required':False,'login_reason':'status_unavailable'}
                cache_key=cfg['instance_id'],cfg['bot_qq'],saved.get('session_token'),saved.get('qq_listener',{}).get('Created')
                now=time.monotonic()
                if self.login_health and self.login_health[0]==cache_key and now-self.login_health[1]<5:
                    detail=self.login_health[2]
                else:
                    try:
                        from .qq_session import login_status_verified
                        detail=login_status_verified(self.paths,saved)
                    except Exception:pass
                    self.login_health=cache_key,now,detail
                return result | {"state": "运行", "web_ready": True} | detail
        return result | {"state": "等待验证"}

    def management_url(self, role):
        """Dashboard access does not depend on QQ/plugin heartbeat health."""
        if role not in ("astrbot", "napcat"):
            raise ValueError("此服务没有管理页面。")
        cfg = self.config.load()
        if role == 'napcat' and cfg['qq_backend'] == 'snowluma':
            return f"http://127.0.0.1:{cfg['snowluma_port']}"
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

    def _start_ready(self, role, state):
        """Startup dependency readiness is not proof of a QQ login.

        AstrBot must be ready before NapCat starts. An unavailable OneBot API
        at that point is expected, but only a live, current host with both of
        its listeners and a fresh bridge heartbeat may unblock the sequence.
        Leave health()'s unknown/offline login and connection values intact.
        """
        if role != 'astrbot':
            return state['state'] == '运行'
        if not state.get('owned') or not self.owned(role):
            return False
        try:
            cfg = self.config.load()
            saved = self.record(role)
            if not isinstance(saved, dict) or saved.get('instance_id') != cfg['instance_id']:
                return False
            expected = saved.get('host')
            if (not isinstance(expected, dict) or type(expected.get('ProcessId')) is not int
                    or not expected.get('Created') or not expected.get('ExecutablePath')):
                return False
            proof = json.loads((self.paths.home / 'runtime/astrbot-health.json').read_text(encoding='utf-8'))
            if (not isinstance(proof, dict) or proof.get('instance_id') != cfg['instance_id']
                    or type(proof.get('pid')) is not int or proof['pid'] != expected['ProcessId']
                    or type(proof.get('at')) not in (int, float) or not math.isfinite(proof['at'])
                    or not -5 <= time.time() - proof['at'] < 15
                    or type(proof.get('verified')) is not bool or type(proof.get('online')) is not bool):
                return False
            error = proof.get('error_type')
            expected_external_probe = cfg['qq_backend'] == 'snowluma' and proof.get('error_stage') == 'probe'
            if error is not None and not expected_external_probe and (error != 'ApiNotAvailable' or proof.get('error_stage') != 'probe'):
                return False
            for port in (cfg['astrbot_port'], cfg['onebot_port']):
                actual = listener(port)
                if not isinstance(actual, dict) or any(actual.get(key) != expected[key]
                                                      for key in ('ProcessId', 'Created', 'ExecutablePath')):
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def start(self, role, *, automatic=False):
        with self.lifecycle_lock:
            if role=='napcat' and self.console_lease and self.console_lease.get('login_confirmation_required'):
                if automatic:return self.health(role)
                self.console_lease['login_confirmation_required']=False
                atomic_json(self.paths.home/'runtime/console.json',self.console_lease)
            if automatic and not self.config.load()['runtime']['components'].get(role):return
            if not automatic:self.set_desired(role,True)
            return self._start(role)

    def set_desired(self,role,enabled):
        if role not in self.adapters:raise RuntimeErrorBase('Unknown service role')
        lock=InstanceLock(self.paths.home/'runtime/config-write.lock')
        try:
            cfg=self.config.load()
            if cfg['runtime']['components'][role]!=enabled:
                cfg['runtime']['components'][role]=enabled;cfg['runtime']['revision']+=1
                atomic_json(self.config.file,cfg)
        finally:lock.close()

    def ensure_binding(self):
        cfg=self.config.load()
        if cfg['runtime']['qq_persona_id'] or not self.console_lease:return
        database=self.paths.home/'data/database/runtime.sqlite3'
        if not database.exists():return
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()
        if not row:return
        lock=InstanceLock(self.paths.home/'runtime/config-write.lock')
        try:
            cfg=self.config.load()
            if not cfg['runtime']['qq_persona_id']:
                cfg['runtime']['qq_persona_id']=row[0];cfg['runtime']['revision']+=1
                atomic_json(self.config.file,cfg)
                self.console_lease['qq_persona_id']=row[0]
                atomic_json(self.paths.home/'runtime/console.json',self.console_lease)
        finally:lock.close()

    def _start(self, role):
        if self.force_stopping.is_set() or self.closing.is_set():
            raise RuntimeErrorBase("强制停止正在执行，不能同时启动服务。")
        generation = self.stop_generation
        if role not in self.adapters:
            raise RuntimeErrorBase("Unknown service role")
        console = require_console(self.paths)
        cfg = self.config.load()
        if role == 'napcat' and cfg['qq_backend'] == 'snowluma':
            if not (cfg['owner_qq'] and cfg['bot_qq']):
                raise RuntimeErrorBase('请先填写本人 QQ 与 Bot QQ。')
            self.start('astrbot')
            return self.health(role)
        health = self.health(role)
        if self._start_ready(role, health):
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
        python = component_python(self.paths, role)
        self.notify("正在启动：" + role)
        if generation != self.stop_generation or self.force_stopping.is_set():
            raise RuntimeErrorBase("启动已被强制停止操作取消。")
        require_console(self.paths, console["token"])
        if self.app_processes is None or not self.console_lease or self.console_lease['token'] != console['token']:
            raise RuntimeErrorBase('请从拥有当前组件的工作台执行启动。')
        env=child_environment(self.paths)
        if console.get('qq_persona_id'):env['PCAR_QQ_PERSONA_ID']=console['qq_persona_id']
        self.processes[role] = self.app_processes.start(role,[str(python), "-B", "-m", "persona_runtime.host", "--role", role,
                          "--instance", cfg["instance_id"], "--console-token", console["token"]], cwd=REPO, env=env)
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            if generation != self.stop_generation:
                raise RuntimeErrorBase("启动已被强制停止操作取消。")
            time.sleep(.5)
            state = self.health(role)
            if self._start_ready(role, state):
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
        with self.lifecycle_lock:return self._stop(role)

    def _stop(self, role):
        self.set_desired(role,False)
        if role == 'napcat' and self.config.load()['qq_backend'] == 'snowluma':
            self.set_desired('astrbot', False)
            if self.owned('astrbot'):
                self._stop('astrbot')
            return
        if not self.owned(role):
            if self.health(role)["state"] == "已停止":
                return
            raise RuntimeErrorBase("进程归属未验证，不执行停止。")
        self.notify("正在停止：" + role)
        if role == "service":
            self.client.call("POST", "/v1/control/drain", {})
            self.client.call("POST", "/v1/control/stop", {}, timeout=370)
        else:
            if role=='napcat':
                result={'logout_confirmed':False,'logout_reason':'remote_logout_unconfirmed'}
                try:
                    from .qq_session import logout_verified
                    result.update(logout_verified(self.paths,self.record(role),timeout=10))
                except Exception as error:result['logout_reason']=type(error).__name__
                atomic_json(self.paths.home/'runtime/last-qq-logout.json',result|{
                    'console_token':(self.console_lease or {}).get('token'),'qq_was_running':True,'bot':self.config.load()['bot_qq']})
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
            if self.owned(role) or role == 'napcat' and self.config.load()['qq_backend'] == 'snowluma':
                self.stop(role)

    def snapshot(self):
        return {role: self.health(role) for role in self.adapters}

    def release_role(self,role):
        """Only jobs held by this Workbench may be force-released."""
        if self.app_processes is not None:self.app_processes.stop(role)
        self.processes.pop(role,None)

    def shutdown(self):
        """Bounded close: 30s drain, 10s logout, 5s release; unknown stays unknown."""
        if self.console_lease is None:raise RuntimeErrorBase('只有当前工作台可以结束整个应用。')
        self.closing.set();self.stop_generation+=1
        report={'at':timestamp(),'instance_id':self.config.load()['instance_id'],
                'console_token':self.console_lease['token'],
                'logout_confirmed':False,'logout_reason':'not_logged_in_or_unavailable','errors':[]}
        def progress(phase):
            self.shutdown_progress={'token':report['console_token'],'at':report['at'],'phase':phase,'updated_at':timestamp(),'maximum_seconds':45}
            try:atomic_json(self.paths.home/'runtime/application-closing.json',self.shutdown_progress)
            except OSError:
                if 'closing_marker_write_failed' not in report['errors']:report['errors'].append('closing_marker_write_failed')
        progress('draining')
        report['qq_backend'] = self.config.load()['qq_backend']
        external = report['qq_backend'] == 'snowluma'
        report['external_qq_session'] = external
        report['qq_was_running']=not external and self.owned('napcat')
        if external:
            report['logout_reason'] = 'external_session_connection_only'
        if not report['qq_was_running'] and not external:
            try:
                previous=json.loads((self.paths.home/'runtime/last-qq-logout.json').read_text())
                if previous.get('console_token')==(self.console_lease or {}).get('token') and previous.get('bot')==self.config.load()['bot_qq']:
                    report.update({k:v for k,v in previous.items() if k!='console_token'})
            except (OSError,ValueError):pass
        try:
            deadline=time.monotonic()+30
            if self.owned('service'):
                try:self.client.call('POST','/v1/control/drain',{'closing':True,'instance_id':report['instance_id']},timeout=2)
                except RuntimeErrorBase:report['errors'].append('drain_unconfirmed')
                while time.monotonic()<deadline:
                    try:
                        status=self.client.call('GET','/v1/health',timeout=min(2,max(.1,deadline-time.monotonic())))
                        if not status['pending'] and not status.get('background_running') and not status.get('sending_parts'):break
                    except RuntimeErrorBase:break
                    time.sleep(.2)
            # Fence the sender before logout and never launch a replacement while closing.
            self.release_role('astrbot')
            progress('logout')
            if not external and self.owned('napcat'):
                try:
                    from .qq_session import logout_verified
                    report.update(logout_verified(self.paths,self.record('napcat'),timeout=10))
                except Exception as exc:report['logout_reason']=type(exc).__name__
        finally:
            progress('releasing')
            try:self.end_console()
            except Exception as error:report['errors'].append('console_cleanup_'+type(error).__name__)
            deadline=time.monotonic()+2
            while self.has_resident_processes() and time.monotonic()<deadline:time.sleep(.1)
            report['processes_released']=not self.has_resident_processes()
            report['phase']='components_released' if report['processes_released'] else 'release_unconfirmed'
            try:atomic_json(self.paths.home/'runtime/last-exit.json',report)
            except OSError:report['errors'].append('exit_report_write_failed')
        return report

    def force_stop_all(self):
        from .emergency import force_stop_all_elevated
        self.force_stopping.set()
        self.stop_generation += 1
        try:
            return force_stop_all_elevated(self.paths, self.notify)
        finally:
            self.force_stopping.clear()

    def force_stop_qq(self):
        from .emergency import force_stop_qq_elevated
        self.force_stopping.set()
        self.stop_generation += 1
        try:
            return force_stop_qq_elevated(self.paths, self.notify)
        finally:
            self.force_stopping.clear()
