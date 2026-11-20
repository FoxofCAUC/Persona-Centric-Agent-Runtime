"""Freeze the public retrieval provider separately for new v8 Pro desktop work."""
import json

from .deepseek_native_contract import MODEL as NATIVE_MODEL, official_endpoint
from .models import RuntimeErrorBase


ROUTE = 'v8-pro-public-native-flash-v1'
PERMISSION = 'knowledge_native'
# This is the model identity of the frozen v1 route, independent of later factory defaults.
SPEECH_MODEL = 'deepseek-v4-pro'


def for_new_work(db, turn, stage):
    if stage != 'conversation_native' or turn['channel'] != 'desktop' or turn.get('reply_mode_snapshot') != 'context_full':
        return None
    base = json.loads(turn['model_snapshot'])
    if base.get('name') != SPEECH_MODEL or base.get('_companion_profile', {}).get('version') != 'companion-runtime-v8':
        return None
    from .expression_runtime import enabled as expression_enabled
    from .dialogue_generation import enabled as direct_enabled
    if expression_enabled(db,turn['id']) or direct_enabled(db,turn['id']):
        return None
    return ROUTE


def flash_model(base, config=None):
    if base.get('name') != SPEECH_MODEL:
        raise RuntimeErrorBase('native_pro_speech_source_changed')
    # Native requests take only provider configuration. The speech profile belongs to the Pro turn.
    from .provider_policy import is_deepseek_text_relay
    settings = config.load() if config is not None and is_deepseek_text_relay(base) else {}
    if settings.get('private_research_account') == 'native_web':
        model = settings['native_web_model']
    else:
        model = {k:v for k,v in base.items() if not k.startswith('_')}
        model['name'] = NATIVE_MODEL
    try:
        official_endpoint(model)
    except ValueError as error:
        raise RuntimeErrorBase('native_web_official_flash_required') from error
    return model


def model_for_task(db, persona_id, spec, config=None):
    from .knowledge_native import RETRIEVAL_PROFILE
    root = db.execute('SELECT * FROM turns WHERE id=? AND persona_id=?',
                      (spec['root_turn_id'], persona_id)).fetchone()
    if (spec.get('native_model_route') != ROUTE or spec['input'].get('retrieval_profile') != RETRIEVAL_PROFILE
            or not root or for_new_work(db,dict(root),spec['stage']) != ROUTE):
        raise RuntimeErrorBase('native_model_route_changed')
    return flash_model(json.loads(root['model_snapshot']),config)
