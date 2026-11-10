"""Minimal owner-only adapter; all model calls belong to Persona Runtime."""
import asyncio
import time
from contextlib import suppress

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Star, register

from persona_runtime.config import ConfigStore
from persona_runtime.logging_safe import setup
from persona_runtime.paths import RuntimePaths
from persona_runtime.qq_bridge import QQBridge, allowed_message
from persona_runtime.qq_client import require_snowluma, send_private_text


@register("astrbot_plugin_persona_runtime", "Persona Runtime", "本人好友私聊人格桥接", "0.7.4")
class PersonaRuntimePlugin(Star):
    def __init__(self, context):
        super().__init__(context)
        self.paths = RuntimePaths.from_environment()
        self.config = ConfigStore(self.paths)
        self.bridge = QQBridge(self.paths)
        self.health_task = None
        self.delivery_task = None
        self.delivery_wakeup = asyncio.Event()
        self.active_until = 0

    async def initialize(self):
        setup(self.paths, "astrbot")
        # AstrBot's default log broker must not persist raw chat or third-party errors.
        self.health_task = asyncio.create_task(self.health())
        self.delivery_task = asyncio.create_task(self.deliver_pending())

    async def deliver_pending(self):
        delay=.5
        while True:
            try:
                cfg = self.config.load()
                enabled = cfg.get('qq_backend', 'napcat') != 'snowluma' or cfg['runtime']['components']['napcat']
                if enabled and cfg["owner_qq"] and cfg["bot_qq"]:
                    await self.bridge.flush_receipts()
                    await self.bridge.reconcile_inbox()
                    health = await self.bridge.request("GET", "/v1/health")
                    m63 = "reply_delivery_parts_v1" in health.get("capabilities", [])
                    pending = (await self.bridge.request("GET", "/v2/delivery/streams") if m63
                               else await self.bridge.request("GET", "/v1/qq-pending"))
                    delay=.5 if pending or time.monotonic()<self.active_until else min(5,delay*1.5)
                    for item in pending:
                        for platform in self.context.platform_manager.platform_insts:
                            if platform.meta().name != "aiocqhttp" or platform.meta().id != "persona-onebot":
                                continue
                            client = platform.get_client()
                            if cfg.get('qq_backend', 'napcat') == 'snowluma':
                                await require_snowluma(client, cfg)
                            else:
                                login = await asyncio.wait_for(client.get_login_info(), 3)
                                if str(login.get("user_id", "")) != cfg["bot_qq"]:
                                    continue
                            async def send(text):
                                return await send_private_text(client, cfg, text)
                            if m63:
                                await self.bridge.deliver_stream(item, send)
                            else:
                                await self.bridge.deliver(item, send)
                            break
            except Exception:
                delay=min(5,delay*1.5)
            try:await asyncio.wait_for(self.delivery_wakeup.wait(),delay)
            except asyncio.TimeoutError:pass
            self.delivery_wakeup.clear()

    async def health(self):
        from persona_runtime.bridge_health import heartbeat_loop
        await heartbeat_loop(self.paths, self.config, lambda:self.context.platform_manager.platform_insts)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def receive(self, event):
        if event.platform_meta.name != "aiocqhttp":
            return
        event.stop_event()
        try:
            cfg = self.config.load()
            if cfg.get('qq_backend', 'napcat') == 'snowluma':
                if not allowed_message(event, cfg):
                    return
                await require_snowluma(event.bot, cfg)
            async def send(text):
                # Await actual platform completion; no generator-yield success assumptions.
                if cfg.get('qq_backend', 'napcat') == 'snowluma':
                    return await send_private_text(event.bot, self.config.load(), text)
                return await event.send(event.plain_result(text))
            await self.bridge.handle(event, send, background=True)
            self.active_until=time.monotonic()+60
            self.delivery_wakeup.set()
        except Exception:
            logger.error("persona_bridge_failure")

    async def terminate(self):
        await self.bridge.close()
        if self.delivery_task:
            self.delivery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.delivery_task
        if self.health_task:
            self.health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.health_task
