"""Cloud generation is official DeepSeek; Jev has its independent transport."""
from urllib.parse import urlsplit
from .provider_compat import CURRENT_DEEPSEEK


PRIVATE_COMPATIBLE_WRITER = ('https://e-flowcode.cc/v1', 'gemini-4-hydrogen')
DEEPSEEK_TEXT_RELAY = 'https://e-flowcode.cc/v1'


def is_deepseek_text_relay(model):
    return (model.get('base_url') == DEEPSEEK_TEXT_RELAY
            and model.get('name') in ('deepseek-v4-pro', 'deepseek-flash'))


def is_private_compatible_writer(model):
    return (model.get('base_url'), model.get('name')) == PRIVATE_COMPATIBLE_WRITER


def require_generation_provider(model, *, allow_loopback_fixture=False, allow_reply_decision_tool=False,
                                allow_private_compatible_writer=False):
    try:
        if '_reply_decision_tool' in model:
            if not allow_reply_decision_tool:raise ValueError()
            from .deepseek_reply_decision import model_profile
            model_profile(model)
            return
        if allow_private_compatible_writer and is_private_compatible_writer(model):
            return
        if is_deepseek_text_relay(model) and '_native_web_profile' not in model:
            return
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
