"""Project approved Flash materials into the reviewed, function-only transport."""
import json

from persona_runtime.deepseek_reply_decision import BASE, MODEL, PROFILE, VERSION, build_request, parse_receipt
from persona_runtime.dialogue_generation import GenerationHTTPClient
from persona_runtime.provider_policy import require_generation_provider
from .en3_holdout_trial import FirstReceipts
from .en5_decision_contract import ACTIONS, STATES, UNDETERMINED, join_blocks, require

MODE = 'strict-tool-v1'


def decision_pair(base, instruction, targets, timeout):
    ids = [t['id'] for t in targets]
    require(ids == list(range(1, len(targets)+1)) and bool(ids), 'decision_target_order')
    system, *dialogue = base['messages']
    source = json.loads(system['content'])
    require(system['role'] == 'system' and set(source) == {'core_traits', 'expression', 'json_fields'}
            and source['json_fields'] == {'target': [*ids, None], 'action': [*ACTIONS, UNDETERMINED],
                                          'task_state': [*STATES, UNDETERMINED]}, 'decision_frozen_material')
    data = {'core_traits': source['core_traits'], 'expression': source['expression'],
            'undetermined': {'target': 0, 'action': UNDETERMINED, 'task_state': UNDETERMINED}}
    messages = [{'role': 'system', 'content': join_blocks(instruction, data)}, *dialogue]
    model = {'base_url': BASE, 'name': MODEL, 'max_tokens': 128, 'timeout': timeout,
             PROFILE: {'version': VERSION, 'target_count': len(ids)}}
    require_generation_provider(model, allow_reply_decision_tool=True)
    return model, build_request(model, messages)[0]


class ToolFirstReceipts(FirstReceipts):
    @staticmethod
    def receipt(raw, wire):
        if wire.get('tools'):
            return parse_receipt(raw, wire)
        return FirstReceipts.receipt(raw, wire)

    def check_wire(self, stage, model, wire):
        if stage != 'decision':
            return super().check_wire(stage, model, wire)
        require_generation_provider(model, allow_reply_decision_tool=True)
        require(PROFILE in model and not model.get('encrypted_key') and wire == build_request(model, wire['messages'])[0],
                'decision_tool_wire_changed')


class DecisionToolHTTPClient(GenerationHTTPClient):
    @staticmethod
    def preflight(model, wire):
        if PROFILE in model:
            return GenerationHTTPClient.preflight(model, wire, wire_builder=build_request, allow_reply_decision_tool=True)
        return GenerationHTTPClient.preflight(model, wire)
