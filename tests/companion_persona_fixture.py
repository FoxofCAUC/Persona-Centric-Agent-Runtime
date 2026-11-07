"""Small artificial inputs for the frozen pre-v4 companion requests."""
from copy import deepcopy
import json
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.provider_compat import build_request
from tests.test_companion_chat_contract import Runtime


async def legacy_requests(modes=('minimal', 'minimal_structural', 'minimal_chat')):
    case = Runtime('test_normal_admission_final_manifest_and_single_generation')
    case.setUp()
    try:
        data = deepcopy(case.data)
        data['identity']['gender'] = '女'
        data['social']['context_rules'] = '交换意见时允许有不同判断。'
        case.save(data)
        results = {}
        for mode in modes:
            case.enable(mode)
            for query in ('今天风很舒服', '你多大了', '你喜欢什么，书店怎么样'):
                turn = await case.turn(query)
                with patch.object(case.rt, '_hybrid', side_effect=case.hybrid):
                    prepared = case.engine.cognitive.prepare(turn['id'])
                model = json.loads(case.raw(turn['id'])['model_snapshot'])
                messages = prepared['messages']
                wire = build_request(model, messages)[0]
                results[mode + ':' + query] = {
                    'messages': messages, 'wire': wire,
                    'message_hash': fingerprint(messages),
                }
        return results
    finally:
        case.doCleanups()
