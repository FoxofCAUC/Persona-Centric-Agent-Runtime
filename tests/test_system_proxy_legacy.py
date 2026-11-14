"""Local-only checks for mandatory proxy transport and redirect handling."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
import urllib.request

from persona_runtime.chat import ModelError
from persona_runtime.network_proxy import _SystemProxyHandler, system_urlopen


class SynchronousProxyTests(unittest.TestCase):
    @contextmanager
    def proxy(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "http://first.invalid/start":
                    self.send_response(302)
                    self.send_header("Location", "http://second.invalid/final")
                    self.end_headers()
                elif self.path == "http://first.invalid/non-web":
                    self.send_response(302)
                    self.send_header("Location", "ftp://second.invalid/file")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"local proxy fixture")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:" + str(server.server_port), requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_redirects_stay_on_system_proxy_despite_environment_bypass(self):
        with self.proxy() as (proxy, requests), \
                patch("persona_runtime.network_proxy.configured_proxy", return_value=proxy), \
                patch.dict("os.environ", {"NO_PROXY": "*", "HTTP_PROXY": "http://wrong.invalid:9"}), \
                patch("urllib.request.proxy_bypass", side_effect=AssertionError("must not bypass")):
            with system_urlopen("http://first.invalid/start", timeout=5) as response:
                self.assertEqual(response.read(), b"local proxy fixture")
        self.assertEqual(requests, ["http://first.invalid/start", "http://second.invalid/final"])

    def test_https_target_uses_connect_to_the_required_proxy(self):
        request = urllib.request.Request("https://first.invalid/document")
        with patch("persona_runtime.network_proxy.configured_proxy", return_value="http://127.0.0.1:7890"), \
                patch("urllib.request.proxy_bypass", side_effect=AssertionError("must not bypass")):
            _SystemProxyHandler().proxy_open(request, "system", "https")
        self.assertEqual(request.host, "127.0.0.1:7890")
        self.assertEqual(request._tunnel_host, "first.invalid")
        self.assertEqual(request.type, "https")

    def test_missing_or_unsupported_proxy_stops_before_connection(self):
        for proxy, code in ((None, "system_proxy_required"),
                ("https://127.0.0.1:7890", "system_proxy_protocol_unsupported")):
            with self.subTest(proxy=proxy), \
                    patch("persona_runtime.network_proxy.configured_proxy", return_value=proxy), \
                    patch("urllib.request.HTTPHandler.http_open", side_effect=AssertionError("must not connect")):
                with self.assertRaisesRegex(ModelError, "^" + code + "$"):
                    system_urlopen("http://first.invalid/document", timeout=5)

    def test_redirect_to_non_web_protocol_is_rejected(self):
        with self.proxy() as (proxy, requests), \
                patch("persona_runtime.network_proxy.configured_proxy", return_value=proxy):
            with self.assertRaisesRegex(ModelError, "^network_url_scheme_invalid$"):
                system_urlopen("http://first.invalid/non-web", timeout=5)
        self.assertEqual(requests, ["http://first.invalid/non-web"])

    def test_non_web_initial_url_is_rejected(self):
        with self.assertRaisesRegex(ModelError, "^network_url_scheme_invalid$"):
            system_urlopen("file:///not-a-network-request")
