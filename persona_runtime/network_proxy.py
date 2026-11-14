"""Use the current Windows system proxy for every public outbound request."""
import ipaddress
import urllib.request
from urllib.parse import urlsplit


def configured_proxy(scheme="https"):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return None
            raw = winreg.QueryValueEx(key, "ProxyServer")[0]
        if not isinstance(raw, str):
            return None
        if "=" in raw:
            values = dict(part.strip().split("=", 1) for part in raw.split(";") if "=" in part)
            raw = values.get(scheme, "")
        raw = raw.strip()
        if not raw or any(ord(char) < 33 for char in raw):
            return None
        value = raw if "://" in raw else "http://" + raw
        parsed = urlsplit(value)
        if (parsed.scheme in ("http", "https") and parsed.hostname and parsed.port
                and not parsed.username and not parsed.password
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            return value
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return None


def outbound_proxy(url):
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    # Owned local service traffic stays on the local machine.
    if hostname == "localhost":
        return None
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return None
    except ValueError:
        pass
    value = configured_proxy(parsed.scheme)
    if value is None:
        from .chat import ModelError
        raise ModelError("system_proxy_required", diagnostics={
            "request_headers_sent": False, "network_route": "system_proxy_unavailable"})
    return value


class _SystemProxyHandler(urllib.request.ProxyHandler):
    # urllib's default ProxyHandler honors proxy_bypass(), including NO_PROXY.
    # Apply the Windows proxy anew to every request, including redirects.
    def __init__(self):
        super().__init__({"http": "system", "https": "system"})

    def proxy_open(self, request, _proxy, _scheme):
        proxy = outbound_proxy(request.full_url)
        if proxy is None:
            return None
        parsed = urlsplit(proxy)
        if parsed.scheme != "http":
            # urllib cannot provide HTTPS-over-HTTPS proxy transport here.
            # Fail explicitly instead of downgrading or connecting directly.
            from .chat import ModelError
            raise ModelError("system_proxy_protocol_unsupported", diagnostics={
                "request_headers_sent": False, "network_route": "system_proxy_unavailable"})
        request.set_proxy(parsed.netloc, "http")
        return None


class _WebRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        _web_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def _web_url(url):
    if urlsplit(url).scheme not in ("http", "https"):
        from .chat import ModelError
        raise ModelError("network_url_scheme_invalid", diagnostics={"request_headers_sent": False})


def system_urlopen(request, *, context=None, timeout=60):
    'Legacy synchronous downloads use the same required system proxy policy.'
    _web_url(request.full_url if isinstance(request, urllib.request.Request) else request)
    opener = urllib.request.build_opener(_SystemProxyHandler(), _WebRedirectHandler(),
                                        urllib.request.HTTPSHandler(context=context))
    return opener.open(request, timeout=timeout)
