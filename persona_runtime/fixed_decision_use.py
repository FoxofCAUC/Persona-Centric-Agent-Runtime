"""A private desktop research copy can grant individual, sealed Flash decision requests."""
import json
import re

from .config import ConfigStore
from .models import RuntimeErrorBase
from .private_personas import canonical, provider_identity, safe, sha
from .provider_policy import require_generation_provider


def guard(paths, policy, model, request):
    try:
        grant_id, slot, wire = request
        if not isinstance(grant_id, str) or not re.fullmatch('[0-9a-f]{64}', grant_id):
            raise ValueError
        home = paths.home.resolve()
        folder = home/'private/en5/decision-uses'
        raw = safe(folder/(grant_id+'.json'), home).read_bytes()
        data = json.loads(raw)
        require_generation_provider(model, allow_reply_decision_tool=True)
        expected = {'version':'en5-fixed-decision-use-v1','approved':True,'purpose':'dialogue_decision',
                    'home':str(home),'instance_id':ConfigStore(paths).load()['instance_id'],
                    'persona_id':policy['persona_id'],'copy_binding':policy['_copy_binding']}
        external = safe(data['author_use']).read_bytes()
        receipt = json.loads(external)
        if (sha(raw) != grant_id or sha(external) != data['author_use_sha256']
                or any(data.get(k) != v for k,v in expected.items())
                or model['name'] != 'deepseek-flash' or provider_identity(model) != data['provider']
                or receipt.get('purpose') != 'dialogue_decision' or receipt.get('approved') is not True
                or receipt.get('slots') != data['slots'] or receipt.get('provider') != data['provider']
                or receipt.get('persona_id') != data['persona_id']
                or receipt.get('instance_id') != data['instance_id']
                or data['slots'].get(slot) != canonical(wire) or wire['model'] != model['name']):
            raise ValueError
    except (TypeError, ValueError, KeyError, OSError) as error:
        raise RuntimeErrorBase('en5_specific_use_unavailable') from error
