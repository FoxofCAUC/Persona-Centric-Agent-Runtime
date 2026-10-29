"""Recoverable bridge heartbeat. No messages, credentials, or model actions."""
import asyncio
import os
import time

from .audit import record
from .config import atomic_json


async def heartbeat_loop(paths, config, platforms, *, publish=atomic_json, sleep=asyncio.sleep,
                         diagnose=record):
    def failure(stage, error):
        # Third-party adapters may remove Python root log handlers. This small
        # metadata-only operation log remains independent of those handlers.
        try:
            diagnose(paths, 'bridge_health_' + stage, error=type(error).__name__)
        except Exception:
            # Disk/log failures must not terminate the only heartbeat task.
            pass

    while True:
        cfg = None
        verified = online = False
        stage = 'config'
        error_type = error_stage = None
        try:
            cfg = config.load()
            stage = 'platform'
            for platform in platforms():
                meta = platform.meta()
                if meta.name != 'aiocqhttp' or meta.id != 'persona-onebot':
                    continue
                stage = 'probe'
                client = platform.get_client()
                login = await asyncio.wait_for(client.get_login_info(), 3)
                verified = str(login.get('user_id', '')) == cfg['bot_qq']
                status = await asyncio.wait_for(client.get_status(), 3)
                online = verified and bool(status.get('online'))
                break
        except Exception as error:
            verified = online = False
            error_type, error_stage = type(error).__name__, stage
            failure(stage, error)
        if cfg is not None:
            try:
                publish(paths.home / 'runtime/astrbot-health.json', {
                    'at':time.time(), 'pid':os.getpid(), 'instance_id':cfg['instance_id'],
                    'verified':verified, 'online':online, 'plugin_version':'0.7.0',
                    'error_type':error_type, 'error_stage':error_stage})
            except Exception as error:
                failure('publish', error)
        # CancelledError intentionally propagates through every await. A real
        # platform outage remains visible; this loop never reconnects or sends.
        await sleep(5)
