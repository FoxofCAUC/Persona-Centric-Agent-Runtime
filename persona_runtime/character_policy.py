"""Character selection policy carried by existing context policy snapshots."""
from copy import deepcopy
from .character_embedding import digest
from .character_retrieval import config_checked, load_config
from .models import RuntimeErrorBase


def checked(value):
    if not isinstance(value, dict) or value.get('mode') not in ('lexical', 'hybrid','hybrid_llm'):
        raise RuntimeErrorBase('invalid_character_policy_snapshot')
    if value['mode'] == 'lexical':
        if set(value) != {'mode'}:raise RuntimeErrorBase('invalid_character_policy_snapshot')
        return dict(value)
    required={'mode','config','config_fingerprint'}
    if value['mode']=='hybrid_llm':required.add('selector_contract')
    if set(value) != required:
        raise RuntimeErrorBase('invalid_character_policy_snapshot')
    if value['mode']=='hybrid_llm':
        from .character_selector_protocol import prompt_for, RECALL_VERSION, OUTPUT_VERSION
        contract=value['selector_contract']
        if (not isinstance(contract,dict) or set(contract)!={'recall','prompt','output'}
                or contract['recall']!=RECALL_VERSION or type(contract['output']) is not int
                or contract['output']!=OUTPUT_VERSION):
            raise RuntimeErrorBase('invalid_character_policy_snapshot')
        prompt_for(contract['prompt'])
    try:
        config = config_checked(value['config'])
        if digest(config) != value['config_fingerprint']:raise ValueError()
        model = config['model_fingerprint']
        if not isinstance(model, str) or len(model) != 64 or any(c not in '0123456789abcdef' for c in model):raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeErrorBase):
        raise RuntimeErrorBase('invalid_character_policy_snapshot') from None
    return deepcopy(value)


def read(context):
    return checked(context['character_retrieval']) if 'character_retrieval' in context else {'mode':'lexical'}


def choose(mode):
    if mode == 'lexical':return {'mode':'lexical'}
    if mode not in ('hybrid','hybrid_llm'):raise RuntimeErrorBase('invalid_character_retrieval_mode')
    config = load_config()
    value={'mode':mode,'config':config,'config_fingerprint':digest(config)}
    if mode=='hybrid_llm':
        from .character_selector_protocol import PROMPT_VERSION, RECALL_VERSION, OUTPUT_VERSION
        value['selector_contract']={'recall':RECALL_VERSION,'prompt':PROMPT_VERSION,'output':OUTPUT_VERSION}
    return checked(value)
