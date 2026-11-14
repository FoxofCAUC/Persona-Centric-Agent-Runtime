"""Offline checks: public requests use the Windows proxy and never fall back."""
import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from persona_runtime.chat import ChatCompletionsClient, ModelError
from persona_runtime.network_proxy import configured_proxy, outbound_proxy


class ProxyPolicyTests(unittest.TestCase):
    def registry(self, raw, enabled=1):
        return SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *args: nullcontext(1),
            QueryValueEx=lambda key, name: (enabled if name == "ProxyEnable" else raw, 1))

    def test_manual_system_proxy_and_protocol_map(self):
        for raw, expected in (("127.0.0.1:7890", "http://127.0.0.1:7890"),
                ("http=127.0.0.1:8000;https=127.0.0.1:9000", "http://127.0.0.1:9000")):
            with self.subTest(raw=raw), patch.dict("sys.modules", {"winreg": self.registry(raw)}):
                self.assertEqual(outbound_proxy("https://example.com/page"), expected)

    def test_missing_disabled_or_invalid_proxy_stops_before_dispatch(self):
        for raw, enabled in (("127.0.0.1:7890", 0), ("", 1), ("socks5://127.0.0.1:1", 1),
                ("http://user:secret@127.0.0.1:1", 1), ("http://localhost:1/path", 1)):
            with self.subTest(raw=raw), patch.dict("sys.modules", {"winreg": self.registry(raw, enabled)}):
                with self.assertRaisesRegex(ModelError, "^system_proxy_required$") as failure:
                    outbound_proxy("https://example.com/page")
                self.assertFalse(failure.exception.diagnostics["request_headers_sent"])

    def test_public_no_proxy_environment_cannot_bypass_system_proxy(self):
        with patch.dict("sys.modules", {"winreg": self.registry("127.0.0.1:7890")}), \
                patch.dict("os.environ", {"NO_PROXY": "*", "HTTPS_PROXY": "http://wrong.invalid:9"}):
            self.assertEqual(outbound_proxy("https://example.com"), "http://127.0.0.1:7890")

    def test_local_service_does_not_leave_machine(self):
        with patch("persona_runtime.network_proxy.configured_proxy", side_effect=AssertionError("local only")):
            for url in ("http://localhost:18765", "http://127.0.0.1:18765", "http://[::1]:18765"):
                self.assertIsNone(outbound_proxy(url))


class ProxyWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_search_fetch_wiki_and_dns_pass_proxy_explicitly(self):
        calls = []
        proxy = "http://127.0.0.1:7890"
        class Content:
            def __init__(self, body): self.body = body
            async def iter_chunked(self, size): yield self.body
        class Response:
            def __init__(self, body):
                self.status = 200
                self.content = Content(json.dumps(body).encode())
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def post(self, url, **kwargs):
                calls.append((url, kwargs))
                if url.endswith("/chat/completions"):
                    return Response({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"total_tokens": 1}})
                return Response({"results": [], "title": "Test", "content": "public"})
            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                if "dns-query" in url:
                    kind = kwargs["params"]["type"]
                    return Response({"Status": 0, "Question": [{"name": "example.com", "type": kind}],
                        "Answer": [{"name": "example.com", "type": kind, "data": "93.184.216.34" if kind == 1 else "2606:4700::1111"}]})
                return Response({"query": {"search": []}})
        model = {"base_url": "https://example.com/v1", "name": "fake", "encrypted_key": "", "max_tokens": 50, "timeout": 10}
        from persona_runtime import knowledge_web_transport as web, knowledge_wikimedia as wiki, knowledge_dns as dns
        with patch("persona_runtime.network_proxy.configured_proxy", return_value=proxy), \
                patch("aiohttp.ClientSession", Session), patch.object(web, "unprotect", return_value="test"), \
                patch.object(wiki, "public_target", new=AsyncMock(return_value={"via_system_proxy": True})), \
                patch.object(dns, "public_target", new=AsyncMock(return_value={"via_system_proxy": True})):
            await ChatCompletionsClient().complete(model, [{"role": "user", "content": "public"}])
            await web.request("search", {"query": "public"}, model)
            await web.request("fetch", {"url": "https://example.com/page"}, model)
            await wiki.execute("wiki_search", {"query": "public"}, 10)
            await dns.https_answers("example.com", 10)
        self.assertEqual(len(calls), 6)
        for url, kwargs in calls:
            self.assertEqual(kwargs["proxy"], proxy, url)
            self.assertFalse(kwargs["allow_redirects"], url)

    async def test_missing_proxy_never_constructs_model_http_session(self):
        model = {"base_url": "https://example.com/v1", "name": "fake", "encrypted_key": "", "max_tokens": 50, "timeout": 10}
        with patch("persona_runtime.network_proxy.configured_proxy", return_value=None), \
                patch("aiohttp.ClientSession", side_effect=AssertionError("must not connect")):
            with self.assertRaisesRegex(ModelError, "system_proxy_required"):
                await ChatCompletionsClient().complete(model, [{"role": "user", "content": "public"}])

    async def test_public_target_never_uses_direct_system_dns(self):
        from persona_runtime.knowledge_dns import public_target
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", side_effect=AssertionError("direct DNS")), \
                patch("persona_runtime.knowledge_dns.https_answers", new=AsyncMock(return_value=(["93.184.216.34"], True))):
            self.assertTrue((await public_target("example.com", 443, 10))["via_system_proxy"])
