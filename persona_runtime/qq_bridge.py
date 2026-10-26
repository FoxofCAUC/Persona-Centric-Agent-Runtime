"""SDK-independent strict QQ ingress gate and durable delivery client."""
import asyncio
import hashlib

import aiohttp

from .config import ConfigStore


def allowed_message(event, cfg):
    if not cfg["owner_qq"] or not cfg["bot_qq"]:
        return None
    meta = getattr(event, "platform_meta", None)
    if getattr(meta, "name", None) != "aiocqhttp" or getattr(meta, "id", None) != "persona-onebot":
        return None
    if not event.is_private_chat() or str(event.get_self_id()) != cfg["bot_qq"] or str(event.get_sender_id()) != cfg["owner_qq"]:
        return None
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, dict) or raw.get("message_type") != "private" or raw.get("sub_type") != "friend":
        return None
    if str(raw.get("user_id", "")) != cfg["owner_qq"] or str(raw.get("self_id", "")) != cfg["bot_qq"]:
        return None
    message_id = str(getattr(event.message_obj, "message_id", "") or "")
    if not message_id or str(raw.get("message_id", "")) != message_id:
        return None
    parts = raw.get("message")
    if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or p.get("type") != "text" for p in parts):
        return None
    texts = [p.get("data", {}).get("text") for p in parts]
    if any(not isinstance(t, str) for t in texts):
        return None
    text = "".join(texts)
    if not text.strip():
        return None
    key = ":".join((meta.id, cfg["bot_qq"], cfg["owner_qq"], message_id))
    return {"channel": "qq", "bot": cfg["bot_qq"], "sender": cfg["owner_qq"],
            "text": text, "request_id": "qq-" + hashlib.sha256(key.encode()).hexdigest()}


class QQBridge:
    def __init__(self, paths):
        self.config = ConfigStore(paths)

    async def request(self, method, path, value=None):
        cfg = self.config.load()
        headers = {"Authorization": "Bearer " + self.config.token("bridge")}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as client:
            async with client.request(method, f"http://127.0.0.1:{cfg['service_port']}" + path,
                                      json=value, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError("bridge_api_error")
                return await response.json()

    async def handle(self, event, send):
        payload = allowed_message(event, self.config.load())
        if payload is None:
            return False
        # The SDK handler stops the normal LLM path before this first await.
        turn = await self.request("POST", "/v1/chat", payload)
        turn_id = turn["id"]
        while turn["status"] in ("queued", "running"):
            await asyncio.sleep(.5)
            turn = await self.request("GET", "/v1/requests/" + turn_id)
        if turn["status"] != "completed":
            return False
        return await self.deliver(turn, send)

    async def deliver(self, turn, send):
        turn_id = turn["id"]
        claim = await self.request("POST", f"/v1/requests/{turn_id}/delivery", {"action": "claim"})
        if not claim["claimed"]:
            return False
        try:
            for offset in range(0, len(turn["reply"]), 450):
                result = await send(turn["reply"][offset:offset+450])
                if result is False:
                    raise RuntimeError("platform_send_failed")
        except BaseException:
            # Cancellation and partial sending are uncertain, never silently resent.
            try:
                await self.request("POST", f"/v1/requests/{turn_id}/delivery", {"action": "uncertain"})
            except Exception:
                pass
            raise
        await self.request("POST", f"/v1/requests/{turn_id}/delivery", {"action": "sent"})
        return True
