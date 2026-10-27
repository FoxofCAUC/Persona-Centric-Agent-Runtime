"""Offline SDK integration probe. Run only under the dedicated AstrBot interpreter."""
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sys
import subprocess

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


async def main(home):
    from persona_runtime.paths import RuntimePaths
    from persona_runtime.config import ConfigStore
    from persona_runtime.deployment import child_environment
    paths = RuntimePaths(home)
    cfg = ConfigStore(paths)
    cfg.initialize()
    # Never contend with the user's live dashboard during an offline SDK probe.
    while True:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            dashboard_port = reservation.getsockname()[1]
        if dashboard_port not in (8765, 6399, 6299):
            break
    cfg.update({"astrbot_port": dashboard_port})
    bot = home / "components/astrbot/bot"
    bot.mkdir(parents=True, exist_ok=True)
    os.chdir(bot)
    os.environ.update(ASTRBOT_ROOT=str(bot), ASTRBOT_CLI="1", PERSONA_RUNTIME_HOME=str(home),
                      TEMP=str(home / "tmp"), TMP=str(home / "tmp"))
    env = child_environment(paths)
    env.update(ASTRBOT_ROOT=str(bot), ASTRBOT_CLI="1")
    subprocess.run([sys.executable, "-B", "-m", "persona_runtime.host", "--role", "configure"],
                   env=env, cwd=bot, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Real SDK config rendering must also produce a consumer-compatible OneBot timeout.
    from persona_runtime.deployment import onebot_config, validate_onebot_config
    validate_onebot_config(onebot_config(cfg.load(), "offline-probe"))
    plugin_dir = bot / "data/plugins/astrbot_plugin_persona_runtime"
    shutil.copytree(REPO / "integrations/astrbot/astrbot_plugin_persona_runtime", plugin_dir, dirs_exist_ok=True)

    # Block outbound DNS/connections. The probe may bind only the local dashboard.
    original_getaddrinfo = socket.getaddrinfo
    def local_dns(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1", "0.0.0.0", b"127.0.0.1"):
            raise OSError("External networking disabled in SDK probe")
        return original_getaddrinfo(host, *args, **kwargs)
    socket.getaddrinfo = local_dns
    from persona_runtime.host import run_astrbot
    stop = home / "runtime/astrbot.stop"
    task = asyncio.create_task(run_astrbot(paths, stop))
    health = home / "runtime/astrbot-health.json"
    diagnostics = []
    class ProbeHandler(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            if record.levelno >= logging.ERROR or "plugin" in text.lower() or "插件" in text:
                diagnostics.append(text[:1000])
    try:
        async with asyncio.timeout(40):
            while not health.exists():
                if not any(isinstance(h, ProbeHandler) for h in logging.getLogger().handlers):
                    logging.getLogger().addHandler(ProbeHandler())
                if task.done():
                    await task
                    raise AssertionError("SDK exited before plugin initialization")
                await asyncio.sleep(.25)
            proof = json.loads(health.read_text(encoding="utf-8"))
            assert proof["plugin_version"] == "0.4.0"
            assert not proof["online"] and not proof["verified"]
            from astrbot.api import logger
            logger.warning("SDK probe sensitive body must not be persisted")
    except TimeoutError:
        print(json.dumps({"probe_diagnostics": diagnostics[-12:]}, ensure_ascii=False), file=sys.stderr)
        raise
    finally:
        stop.touch()
        try:
            async with asyncio.timeout(10):
                await task
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    for path in home.rglob("*.log"):
        assert "sensitive body" not in path.read_text(encoding="utf-8", errors="replace")
    for path in (home / "logs").glob("*.jsonl"):
        assert "sensitive body" not in path.read_text(encoding="utf-8", errors="replace")
    print(json.dumps({"sdk_plugin_loaded": True, "qq_login": False, "model_calls": 0}))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
