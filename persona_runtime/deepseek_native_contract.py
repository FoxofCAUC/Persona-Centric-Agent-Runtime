"""M9R.0 opt-in wire contract. No dispatch, configuration or storage writes.

This contract is deliberately separate from ordinary chat and structured jobs.
The production native transport and its durable recovery belong to M9R.1.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
from urllib.parse import urlsplit

VERSION = 'deepseek-native-web-v1'
MODEL = 'deepseek-flash'
PRICE_VERSION = 'deepseek-cny-2026-11-15'
OUTPUT_TOKENS = {'ordinary': 2048, 'deep': 8192}
TOOLS = {
    'search': ('deepseek_web_search', 'deepseek_read_url'),
    'read_link': ('deepseek_read_url',),
    'none': (),
}
CONFLICTS = frozenset(('_json_output', '_disposition_compile', '_expression_snapshot',
    '_character_selection', '_dialogue_profile', '_companion_profile',
    '_native_text_profile', '_generation_profile', '_reasoning_effort'))
TOOL_COUNTS = ('tool_search', 'tool_fetch', 'tool_image_search')
DISPLAY_COUNTS = ('web_search_requests', 'web_fetch_requests')


def official_endpoint(model):
    """A familiar model name must never authorize sending data to a gateway."""
    try:
        if not isinstance(model['base_url'], str) or any(char.isspace() for char in model['base_url']):
            raise ValueError()
        url = urlsplit(model['base_url'])
        if (url.scheme != 'https' or url.hostname != 'api.deepseek.com'
                or url.port not in (None, 443) or url.username or url.password
                or url.query or url.fragment or url.path.rstrip('/') not in
                ('', '/v1', '/chat/completions', '/v1/chat/completions')
                or model['name'] != MODEL):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('native_web_official_flash_required') from None
    base = model['base_url'].rstrip('/')
    return base if base.endswith('/chat/completions') else base + '/chat/completions'


def private_research_model(config):
    cfg = config.load()
    account = cfg.get('private_research_account')
    if account == 'native_web':
        return cfg['native_web_model']
    settings = cfg['organization']['model'] if account == 'organization' else cfg['model']
    return settings | {'name': MODEL}


def describe(model):
    official_endpoint(model)
    profile = model.get('_native_web_profile')
    if (type(profile) is not dict or set(profile) != {'version', 'mode', 'tools', 'stream'}
            or profile['version'] != VERSION or not isinstance(profile['mode'], str)
            or profile['mode'] not in OUTPUT_TOKENS or not isinstance(profile['tools'], str)
            or profile['tools'] not in TOOLS or type(profile['stream']) is not bool):
        raise ValueError('native_web_profile_invalid')
    if (any(key in model for key in CONFLICTS)
            or model.get('_reply_mode') not in (None, 'legacy_full')):
        raise ValueError('native_web_profile_conflict')
    if type(model.get('max_tokens')) is not int or not 1 <= model['max_tokens'] <= 393216:
        raise ValueError('native_web_output_limit_invalid')
    return {'version': VERSION, 'profile': VERSION, 'response_format': 'text',
        'thinking': 'enabled' if profile['mode'] == 'deep' else 'disabled',
        'reasoning_effort': 'high' if profile['mode'] == 'deep' else 'none',
        'stream': profile['stream'], 'tools': list(TOOLS[profile['tools']]),
        'format_hash': None, 'application_validation_required': False,
        'output_limit_scope': 'per_step_not_request_cost',
        'tool_execution': 'observed_not_guaranteed'}


def build_request(model, messages):
    contract = describe(model)
    # Inputs must already be an approved public task projection. No persona,
    # memory, config or arbitrary context object is serialized by this builder.
    if type(messages) is not list or not messages:
        raise ValueError('native_web_messages_invalid')
    for message in messages:
        if (type(message) is not dict or message.get('role') not in ('system', 'user', 'assistant')
                or not isinstance(message.get('content'), str)):
            raise ValueError('native_web_messages_invalid')
        allowed = {'role', 'content'} if message['role'] != 'assistant' else {
            'role', 'content', 'reasoning_content', 'server_tool_calls', 'annotations', 'refusal', 'title'}
        if set(message) - allowed or message.get('prefix'):
            raise ValueError('native_web_messages_invalid')
    if messages[-1]['role'] != 'user':
        raise ValueError('native_web_user_turn_required')
    payload = {'model': MODEL, 'messages': deepcopy(messages),
        'thinking': {'type': contract['thinking']}, 'max_tokens': model['max_tokens'],
        'stream': contract['stream']}
    if contract['thinking'] == 'enabled':
        payload['reasoning_effort'] = 'high'
    if contract['tools']:
        payload['tools'] = [{'type': name} for name in contract['tools']]
    if contract['stream']:
        payload['stream_options'] = {'include_usage': True}
    return payload, contract


@dataclass(frozen=True)
class ReplayScope:
    persona_id: str
    channel: str
    session_id: str
    account_binding: str
    model: str = MODEL

    def checked(self):
        if any(not isinstance(value, str) or not value for value in asdict(self).values()):
            raise ValueError('native_web_scope_invalid')
        if self.model != MODEL:
            raise ValueError('native_web_scope_invalid')
        return asdict(self)


def protocol_packet(message, scope):
    """Preserve opaque signatures/step fields; callers store this away from text."""
    if type(message) is not dict or message.get('role') != 'assistant':
        raise ValueError('native_web_packet_invalid')
    return {'version': VERSION, 'scope': scope.checked(), 'assistant': deepcopy(message)}


def replay(packet, scope):
    if type(packet) is not dict or packet.get('version') != VERSION or packet.get('scope') != scope.checked():
        raise ValueError('native_web_packet_scope_mismatch')
    return deepcopy(packet['assistant'])


def branch(*, enabled, decision):
    """Truth table for the future executor, not a second decision model."""
    if type(enabled) is not bool or decision not in ('none', 'web', 'unavailable'):
        raise ValueError('native_web_decision_invalid')
    needed = enabled and decision != 'none'
    return {'call_jev': enabled, 'need_web': needed,
        'decision_source': 'disabled' if not enabled else
            'policy_fallback' if decision == 'unavailable' else 'jev',
        'allow_native_tools': needed, 'allow_independent_tools': needed}


def supplement(*, enabled, decision, outcome, tool_calls, used, intent,
               public_query=None, url=None):
    """At most one independent action after a completed, tool-free request.

The query is an explicitly approved public projection, never raw chat history.
Neither failed nor unknown requests are reissued through a fallback chain.
"""
    if (type(tool_calls) is not int or type(used) is not int
            or not branch(enabled=enabled, decision=decision)['need_web']
            or outcome != 'completed' or tool_calls != 0 or used != 0):
        return None
    if intent == 'explicit_search' and isinstance(public_query, str) and public_query.strip():
        return {'tool': 'search', 'input': {'query': public_query.strip()[:128]},
            'new_action': True, 'replay': False}
    if intent == 'explicit_link' and isinstance(url, str):
        from .knowledge_general import public_url
        return {'tool': 'fetch', 'input': {'url': public_url(url)},
            'new_action': True, 'replay': False}
    return None


def tool_accounting(usage, *, terminal):
    """Result-block counts are not billed calls. Missing receipts stay unknown."""
    counts = {name: None for name in TOOL_COUNTS}
    display = {name: None for name in DISPLAY_COUNTS}
    basis = 'unknown'
    if terminal and isinstance(usage, dict) and usage:
        supplied = usage.get('server_tool_use')
        if 'server_tool_use' not in usage:
            # Official v1 omits this field when all three billed counts are zero.
            counts = dict.fromkeys(TOOL_COUNTS, 0)
            basis = 'documented_terminal_omission'
        elif isinstance(supplied, dict):
            basis = 'provider_receipt'
            for target in (counts, display):
                for name in target:
                    number = supplied.get(name)
                    if type(number) is int and number >= 0:
                        target[name] = number
    known = all(number is not None for number in counts.values())
    return {'billable': counts, 'display': display, 'basis': basis,
        'estimated_tool_amount': str(Decimal(sum(counts.values())) * Decimal('0.005')) if known else None,
        'currency': 'CNY', 'price_version': PRICE_VERSION, 'estimate': True,
        'actual_amount': None}
