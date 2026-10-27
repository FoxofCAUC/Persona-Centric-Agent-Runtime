"""Independent component provisioning. Never edits the source bot installation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .config import ConfigStore, atomic_json
from .models import RuntimeErrorBase
from .security import protect

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REPO = Path(__file__).resolve().parents[1]

# Consumer contract from the bundled NapCat TypeBox schema (rae / iae).
NAPCAT_TIMEOUT_DEFAULTS = {"baseTimeout": 10000, "uploadSpeedKBps": 256,
                           "downloadSpeedKBps": 256, "maxTimeout": 1800000}


def validate_onebot_config(value):
    timeout = value.get("timeout")
    if not isinstance(timeout, dict):
        raise RuntimeErrorBase("NapCat timeout 必须是参数对象，不能是数字；请重新生成消息连接配置。")
    for key in NAPCAT_TIMEOUT_DEFAULTS:
        if type(timeout.get(key)) not in (int, float) or timeout[key] <= 0:
            raise RuntimeErrorBase("NapCat timeout 参数无效：" + key)
    network = value.get("network")
    if not isinstance(network, dict) or any(not isinstance(network.get(key), list) for key in (
            "httpServers", "httpSseServers", "httpClients", "websocketServers", "websocketClients", "plugins")):
        raise RuntimeErrorBase("NapCat network 配置结构无效。")


def onebot_config(cfg, transport):
    value = {
        "network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketServers": [],
                    "websocketClients": [{"name": "persona", "enable": True,
                        "url": f"ws://127.0.0.1:{cfg['onebot_port']}/ws", "token": transport,
                        "reportSelfMessage": False, "messagePostFormat": "array", "debug": False,
                        "heartInterval": 30000, "reconnectInterval": 3000}], "plugins": []},
        "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False,
        "imageDownloadProxy": "", "timeout": dict(NAPCAT_TIMEOUT_DEFAULTS),
    }
    validate_onebot_config(value)
    return value


def repair_napcat_timeout(paths):
    """Repair only the known scalar-timeout bug; preserve account, token and routing."""
    from .models import identifier
    cfg = ConfigStore(paths).load()
    path = paths.home / "components/napcat/config" / f"onebot11_{cfg['bot_qq']}.json"
    old = path.read_text(encoding="utf-8-sig")
    value = json.loads(old)
    if isinstance(value.get("timeout"), dict):
        validate_onebot_config(value)
        return {"changed": False}
    if type(value.get("timeout")) not in (int, float):
        raise RuntimeErrorBase("未知 NapCat 配置格式，未自动修改。")
    backup = paths.home / "backups" / ("napcat-timeout-before-" + identifier() + ".dpapi.json")
    value["timeout"] = dict(NAPCAT_TIMEOUT_DEFAULTS)
    validate_onebot_config(value)
    atomic_json(backup, {"format": "windows-dpapi", "original_config": protect(old)})
    atomic_json(path, value)
    return {"changed": True, "encrypted_backup": str(backup)}


def child_environment(paths):
    env = os.environ.copy()
    env.update(PERSONA_RUNTIME_HOME=str(paths.home), PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1", UV_CACHE_DIR=str(paths.home / "cache/uv"),
               PIP_CACHE_DIR=str(paths.home / "cache/pip"), TEMP=str(paths.home / "tmp"), TMP=str(paths.home / "tmp"),
               HF_HOME=str(paths.home / "cache/huggingface"), XDG_CACHE_HOME=str(paths.home / "cache/xdg"))
    return env


def prepare(paths, *, install=True, source=Path("D:/QQ_Bot_2/napcat-shell")):
    config = ConfigStore(paths)
    config.initialize()
    destination = paths.home / "components/napcat"
    if destination.resolve() == source.resolve():
        raise RuntimeErrorBase("Independent NapCat directory required")
    destination.mkdir(parents=True, exist_ok=True)
    for directory in ("native", "node_modules", "static", "worker"):
        if not (source / directory).is_dir():
            raise RuntimeErrorBase("NapCat source files are incomplete")
        shutil.copytree(source / directory, destination / directory, dirs_exist_ok=True)
    for item in source.iterdir():
        if item.is_file() and (item.suffix in (".js", ".mjs", ".dll", ".exe") or item.name in ("package.json", "qqnt.json")):
            shutil.copy2(item, destination / item.name)
    interpreter = paths.home / "components/astrbot/.venv/Scripts/python.exe"
    env = child_environment(paths)
    if install:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeErrorBase("uv is required to prepare the independent environment")
        commands = []
        if not interpreter.exists():
            commands.append([uv, "venv", "--no-managed-python", "--python", sys.executable, str(interpreter.parents[1])])
        lockfile = REPO / "requirements-astrbot.lock"
        if lockfile.exists():
            commands.append([uv, "pip", "sync", "--python", str(interpreter), str(lockfile)])
        else:
            commands.append([uv, "pip", "install", "--python", str(interpreter), "astrbot==4.29.1"])
        for command in commands:
            result = subprocess.run(command, env=env, cwd=paths.home, creationflags=NO_WINDOW,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeErrorBase("Independent AstrBot installation failed; check network and retry prepare")
    if not interpreter.exists():
        raise RuntimeErrorBase("Independent AstrBot environment is not installed")
    bot = paths.home / "components/astrbot/bot"
    plugin = bot / "data/plugins/astrbot_plugin_persona_runtime"
    plugin.mkdir(parents=True, exist_ok=True)
    for item in (REPO / "integrations/astrbot/astrbot_plugin_persona_runtime").iterdir():
        if item.is_file():
            shutil.copy2(item, plugin / item.name)
    atomic_json(paths.home / "runtime/deployment.json", {"version": 1, "astrbot": "4.29.1", "source": str(source),
                "plugin": "0.4.0", "independent": True})
    return {"prepared": True, "component_root": str(paths.home / "components")}


def write_component_config(paths):
    """Called under the dedicated AstrBot interpreter with its root set BEFORE imports."""
    import copy
    import secrets
    from astrbot.core.config.default import DEFAULT_CONFIG
    cfg = ConfigStore(paths).load()
    bot = paths.home / "components/astrbot/bot"
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["provider_settings"]["enable"] = False
    data["disable_metrics"] = True
    data["provider"], data["provider_sources"] = [], []
    data["platform_settings"].update(enable_id_white_list=True, id_whitelist=["persona-onebot:FriendMessage:" + cfg["owner_qq"]] if cfg["owner_qq"] else [],
                                     id_whitelist_log=False)
    # Transport secrets are fresh per deployment of stopped services, never copied from another bot.
    transport = secrets.token_urlsafe(32)
    existing_transport = paths.home / "components/napcat/config" / f"onebot11_{cfg['bot_qq']}.json"
    if existing_transport.exists():
        existing = json.loads(existing_transport.read_text(encoding="utf-8-sig"))
        for client in existing.get("network", {}).get("websocketClients", []):
            if client.get("url") == f"ws://127.0.0.1:{cfg['onebot_port']}/ws" and client.get("token"):
                transport = client["token"]
    data["platform"] = [{"id": "persona-onebot", "type": "aiocqhttp", "enable": bool(cfg["owner_qq"] and cfg["bot_qq"]),
                          "ws_reverse_host": "127.0.0.1", "ws_reverse_port": cfg["onebot_port"], "ws_reverse_token": transport}]
    data["dashboard"].update(host="127.0.0.1", port=cfg["astrbot_port"], disable_access_log=True)
    old_file = bot / "data/cmd_config.json"
    if old_file.exists():
        old = json.loads(old_file.read_text(encoding="utf-8-sig"))
        for key in ("username", "password", "pbkdf2_password", "password_storage_upgraded", "password_change_required", "jwt_secret"):
            if key in old.get("dashboard", {}):
                data["dashboard"][key] = old["dashboard"][key]
    atomic_json(old_file, data)
    (bot / ".astrbot").touch(exist_ok=True)
    shell = paths.home / "components/napcat"
    atomic_json(shell / "config/napcat.json", {"fileLog": False, "consoleLog": False,
                "fileLogLevel": "error", "consoleLogLevel": "error", "packetBackend": "auto"})
    web_file = shell / "config/webui.json"
    web_data = {"host": "127.0.0.1", "port": cfg["napcat_port"], "token": secrets.token_urlsafe(32),
                "loginRate": 5, "autoLoginAccount": "", "disableWebUI": False}
    if web_file.exists():
        # A running, project-owned QQ login may be retained when only application services restart.
        old = json.loads(web_file.read_text(encoding="utf-8-sig"))
        if old.get("host") == "127.0.0.1" and old.get("port") == cfg["napcat_port"]:
            web_data["token"] = old.get("token") or web_data["token"]
    atomic_json(web_file, web_data)
    if cfg["bot_qq"]:
        atomic_json(shell / f"config/onebot11_{cfg['bot_qq']}.json", onebot_config(cfg, transport))
    # Third-party config readers require transport credentials in their native JSON format.
    # Restrict these runtime files to the current Windows user and SYSTEM.
    for root in (bot / "data", shell / "config"):
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r",
                        f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}:(OI)(CI)F", "SYSTEM:(OI)(CI)F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, check=True)


def render_components(paths):
    python = paths.home / "components/astrbot/.venv/Scripts/python.exe"
    bot = paths.home / "components/astrbot/bot"
    bot.mkdir(parents=True, exist_ok=True)
    env = child_environment(paths)
    env.update(ASTRBOT_ROOT=str(bot), ASTRBOT_CLI="1")
    result = subprocess.run([str(python), "-B", "-m", "persona_runtime.host", "--role", "configure"],
                            cwd=bot, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if result.returncode:
        raise RuntimeErrorBase("Could not generate the isolated component configuration")
