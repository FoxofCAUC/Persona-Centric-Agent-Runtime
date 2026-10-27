"""SDK-independent strict QQ ingress gate and durable delivery client."""
import asyncio
import hashlib
from urllib.parse import quote

import aiohttp

from .config import ConfigStore
from .submissions import SubmissionJournal


class BridgeAPIError(RuntimeError):
    def __init__(self, code, status):
        super().__init__(code)
        self.status = status


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
            "text": text, "platform_message_id":message_id, "platform_time":str(raw.get("time")) if raw.get("time") is not None else None, "request_id": "qq-" + hashlib.sha256(key.encode()).hexdigest()}


class QQBridge:
    def __init__(self, paths):
        self.config = ConfigStore(paths)
        self.journal = SubmissionJournal(paths, "qq")

    async def request(self, method, path, value=None):
        cfg = self.config.load()
        headers = {"Authorization": "Bearer " + self.config.token("bridge")}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False) as client:
            async with client.request(method, f"http://127.0.0.1:{cfg['service_port']}" + path,
                                      json=value, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    try:
                        code = (await response.json()).get("error", "bridge_api_error")
                    except (ValueError, aiohttp.ClientError):
                        code = "bridge_api_error"
                    raise BridgeAPIError(code, response.status)
                return await response.json()

    async def lookup(self,payload):
        for prefix in ('/v2/submissions/qq/','/v1/submissions/qq/'):
            try:return await self.request('GET',prefix+quote(payload['request_id'],safe=''))
            except BridgeAPIError as error:
                if error.status!=404:raise
        return None

    async def reconcile_inbox(self):
        """Only query uncertain receipts. Never replay unaccepted input automatically."""
        cfg = self.config.load()
        for row in self.journal.pending():
            payload = row["payload"]
            if payload.get("sender") != cfg["owner_qq"] or payload.get("bot") != cfg["bot_qq"]:
                continue
            turn = await self.lookup(payload)
            if turn:
                self.journal.forget(payload["request_id"])
            else:
                self.journal.state(payload["request_id"], "not_accepted")

    async def _notice(self, turn, kind, send):
        claim = await self.request("POST", f"/v1/requests/{turn['id']}/notice", {"kind": kind})
        if claim["claimed"]:
            text = ("【服务提示】消息已受理，仍在排队或生成，请稍候。" if kind == "waiting" else
                    "【服务提示】本条生成未完成；消息已保留，请在工作台查看原因并按需手动重试。")
            await send(text)

    async def handle(self, event, send):
        payload = allowed_message(event, self.config.load())
        if payload is None:
            return False
        # The SDK handler stops the normal LLM path before this first await.
        payload = self.journal.remember(payload)
        try:
            health=await self.request('GET','/v1/health')
            if 'durable_ingress_v2' not in health.get('capabilities',[]):
                self.journal.state(payload['request_id'],'not_accepted')
                await send('【服务提示】人格服务版本不兼容，请升级服务；本次消息未自动降级提交。')
                return False
            turn = await self.request("POST", "/v2/ingress", payload)
        except BridgeAPIError as error:
            self.journal.state(payload["request_id"], "not_accepted")
            if str(error) in ("queue_full", "buffer_full", "service_draining"):
                await send("【服务提示】消息未受理：当前队列已满或服务正在停止，请稍后再试。")
            return False
        except (aiohttp.ClientError, OSError, TimeoutError):
            self.journal.state(payload["request_id"], "unconfirmed")
            try:
                turn = await self.lookup(payload)
            except (BridgeAPIError, aiohttp.ClientError, OSError, TimeoutError):
                turn = None
            if not turn:
                await send("【服务提示】暂时无法确认是否受理；已保留本机接收记录，请先在工作台找回，避免重复发送。")
                return False
        self.journal.forget(payload["request_id"])
        if 'receipt_id' in turn:
            while not turn['turn_id']:
                batch=await self.request('GET','/v2/batches/'+turn['batch_id'])
                if batch['state'] in ('interrupted_buffer','cancelled'):return False
                turn['turn_id']=batch['turn_id']
                if not turn['turn_id']:await asyncio.sleep(.5)
            turn=await self.request('GET','/v1/requests/'+turn['turn_id'])
        turn_id = turn["id"]
        loop = asyncio.get_running_loop()
        started = loop.time()
        notice_checked = False
        while turn["status"] in ("queued", "running"):
            await asyncio.sleep(.5)
            try:
                turn = await self.request("GET", "/v1/requests/" + turn_id)
            except (BridgeAPIError, aiohttp.ClientError, OSError, TimeoutError):
                return False  # Already accepted; the background sender handles a completed reply.
            settings = self.config.load()["interaction"]
            if not notice_checked and settings["qq_wait_notice"] and loop.time()-started >= settings["qq_wait_seconds"]:
                notice_checked = True
                await self._notice(turn, "waiting", send)
        if turn["status"] != "completed":
            await self._notice(turn, "failure", send)
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
