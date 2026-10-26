"""Synchronous local API client for background desktop tasks."""
import json
import urllib.error
import urllib.request

from .models import RuntimeErrorBase


class LocalClient:
    def __init__(self, config):
        self.config = config

    def call(self, method, path, value=None, timeout=8):
        cfg = self.config.load()
        request = urllib.request.Request(f"http://127.0.0.1:{cfg['service_port']}" + path,
            data=json.dumps(value).encode("utf-8") if value is not None else None, method=method,
            headers={"Authorization": "Bearer " + self.config.token(), "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(4096)).get("error", "local_api_error")
            except (ValueError, OSError):
                detail = "local_api_error"
            raise RuntimeErrorBase(str(detail)) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeErrorBase("人格服务尚未连接，请先在首页启动。") from None

    def manage(self, action, **args):
        return self.call("POST", "/v1/manage", {"action": action, "args": args})
