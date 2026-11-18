"""Process-local dispatch binding for isolated D3 continuous replay instances."""
from contextvars import ContextVar
import json

from .models import RuntimeErrorBase
from .private_personas import provider_identity, safe, sha

current = ContextVar('en5_d3_dispatch', default=None)


def check(paths, policy, kind, model):
    try:
        ctx = current.get()
        marker = safe(paths.home/'runtime/d3-binding.json', paths.home)
        raw = marker.read_bytes()
        binding = json.loads(raw)
        use = safe(paths.home.parent/'author-use.private.json', paths.home.parent)
        parent = safe(paths.home.parent.parent/'author-use.private.json', paths.home.parent.parent)
        if (policy is None or not ctx or policy.get('d3_binding_sha256') != sha(raw)
                or binding['home'] != str(paths.home.resolve())
                or binding['persona_id'] != policy['persona_id'] or ctx['home'] != binding['home']
                or ctx['binding'] != sha(raw) or ctx['kind'] != kind
                or kind not in ('main', 'dialogue_decision')
                or ctx['session'] not in binding['sessions']
                or (kind == 'dialogue_decision' and binding['sessions'][ctx['session']]['branch'] != 'candidate')
                or ctx['provider'] != provider_identity(model) or sha(use.read_bytes()) != binding['author_use_sha256']
                or sha(parent.read_bytes()) != binding['parent_use_sha256']):
            raise ValueError
        return ctx
    except (KeyError,ValueError,TypeError,OSError) as error:
        raise RuntimeErrorBase('d3_use_scope_changed') from error
