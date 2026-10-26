"""Minimal owner-only adapter; all model calls belong to Persona Runtime."""
import asyncio
from contextlib import suppress
import os
import time

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Star, register

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.logging_safe import setup
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_bridge import QQBridge


@register("astrbot_plugin_persona_runtime", "Persona Runtime", "本人好友私聊人格桥接", "0.2.0")
class PersonaRuntimePlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        self.paths = RuntimePaths.from_environment()
        self.config = ConfigStore(self.paths)
        self.bridge = QQBridge(self.paths)
        self.health_task = None
        self.delivery_task = None

    async def initialize(self):
        setup(self.paths, "astrbot")
        # AstrBot's default log broker must not persist raw chat or third-party errors.
        self.health_task = asyncio.create_task(self.health())
        self.delivery_task = asyncio.create_task(self.deliver_pending())

    async def deliver_pending(self):
        while True:
            try:
                cfg = self.config.load()
                if cfg["owner_qq"] and cfg["bot_qq"]:
                    pending = await self.bridge.request("GET", "/v1/qq-pending")
                    for turn in pending:
                        for platform in self.context.platform_manager.platform_insts:
                            if platform.meta().name != "aiocqhttp" or platform.meta().id != "persona-onebot":
                                continue
                            client = platform.get_client()
                            login = await asyncio.wait_for(client.get_login_info(), 3)
                            if str(login.get("user_id", "")) != cfg["bot_qq"]:
                                continue
                            async def send(text):
                                return await client.send_private_msg(user_id=int(cfg["owner_qq"]),
                                    message=[{"type": "text", "data": {"text": text}}])
                            await self.bridge.deliver(turn, send)
                            break
            except Exception:
                pass
            await asyncio.sleep(1)

    async def health(self):
        while True:
            verified = online = False
            cfg = self.config.load()
            for platform in self.context.platform_manager.platform_insts:
                if platform.meta().name != "aiocqhttp" or platform.meta().id != "persona-onebot":
                    continue
                try:
                    client = platform.get_client()
                    login = await asyncio.wait_for(client.get_login_info(), 3)
                    verified = str(login.get("user_id", "")) == cfg["bot_qq"]
                    status = await asyncio.wait_for(client.get_status(), 3)
                    online = verified and bool(status.get("online"))
                except Exception:
                    pass
            atomic_json(self.paths.home / "runtime/astrbot-health.json", {
                "at": time.time(), "pid": os.getpid(), "instance_id": cfg["instance_id"],
                "verified": verified, "online": online, "plugin_version": "0.2.0"})
            await asyncio.sleep(5)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def receive(self, event):
        if event.platform_meta.name != "aiocqhttp":
            return
        event.stop_event()
        try:
            async def send(text):
                # Await actual platform completion; no generator-yield success assumptions.
                return await event.send(event.plain_result(text))
            await self.bridge.handle(event, send)
        except Exception:
            logger.error("persona_bridge_failure")

    async def terminate(self):
        if self.delivery_task:
            self.delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.delivery_task
        if self.health_task:
            self.health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.health_task
