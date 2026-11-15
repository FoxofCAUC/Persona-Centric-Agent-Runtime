"""Cloud generation is official DeepSeek; Jev has its independent transport."""
from urllib.parse import urlsplit
from .provider_compat import CURRENT_DEEPSEEK


def require_generation_provider(model, *, allow_loopback_fixture=False):
    try:
        raw=model['base_url'];url=urlsplit(raw)
        if (allow_loopback_fixture and url.hostname in ('127.0.0.1','localhost','::1')
                and url.scheme in ('http','https') and not url.username and not url.password):
            return
        if (not isinstance(raw,str) or any(character.isspace() for character in raw)
                or url.scheme!='https' or url.hostname!='api.deepseek.com'
                or url.port not in (None,443) or url.username or url.password or url.query or url.fragment
                or url.path.rstrip('/') not in ('','/v1','/chat/completions','/v1/chat/completions')
                or model['name'] not in CURRENT_DEEPSEEK):
            raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError):
        raise ValueError('provider_official_deepseek_required') from None
