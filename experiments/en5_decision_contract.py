"""E.N5 decision fields, model-visible projections, and the desktop dispatch deadline."""
import asyncio
import json
import re
import time
from dataclasses import dataclass

from persona_runtime.models import RuntimeErrorBase
from persona_runtime.provider_compat import build_request
from persona_runtime.provider_policy import require_generation_provider


STUDY = 'en5-ling-reply-decision-v1'
ACTIONS = ('参与交流', '回应求助', '承接转题或收尾')
STATES = ('无待办', '进行中', '已收束')
UNDETERMINED = '未定'
DEADLINE_SECONDS = 3.0
KEYS = {'target', 'action', 'task_state'}
SLOT = re.compile(r'\{[A-Za-z_][A-Za-z_0-9]*\}')


def require(ok, reason):
    if not ok:
        raise RuntimeErrorBase('en5_decision_' + reason)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def join_blocks(instruction, data):
    return '\n\n'.join(s for s in (instruction, compact(data)) if s)


def validate_decision(value, targets):
    require(isinstance(value, dict) and set(value) == KEYS, 'decision_fields')
    target, action, state = (value[k] for k in ('target', 'action', 'task_state'))
    if target is None and action == state == UNDETERMINED:
        return dict(value)
    require(type(target) is int and any(t['id'] == target for t in targets), 'target_outside_turn')
    require(action in ACTIONS and state in STATES, 'decision_value')
    return {'target': target, 'action': action, 'task_state': state}


def target_list(persona_id, session_id, entries):
    """Keep each eligible event once, with its real role, order and original Pro address."""
    grouped = {}
    for entry in entries:
        require((entry['persona_id'], entry['session_id']) == (persona_id, session_id), 'source_scope')
        require(entry['role'] in ('user', 'assistant') and isinstance(entry['text'], str)
                and entry['text'] and entry['source'] in ('current', 'recent', 'reference'), 'source_event')
        require(entry['role'] != 'assistant' or entry['displayed'], 'unpresented_reply')
        key = entry['event_id']
        location = entry['pro_target']
        require(isinstance(key, str) and key and type(entry['sequence']) is int
                and type(location['message_index']) is int and location['message_index'] >= 0, 'target_address')
        if key in grouped:
            prior = grouped[key]
            require(all(prior[k] == entry[k] for k in ('text', 'role', 'sequence')), 'source_changed')
            if entry['source'] not in prior['origins']:
                prior['origins'].append(entry['source'])
            # Prefer an address in actual dialogue history to a duplicate in the reference block.
            if entry['source'] != 'reference':
                prior['pro_target'] = location
        else:
            grouped[key] = {k: entry[k] for k in ('event_id', 'sequence', 'role', 'text', 'pro_target')}
            grouped[key]['origins'] = [entry['source']]
    rows = sorted(grouped.values(), key=lambda row: row['sequence'])
    require(rows and any('current' in t['origins'] for t in rows), 'current_input_missing')
    require(len({t['sequence'] for t in rows}) == len(rows), 'event_order')
    return [dict(t, id=index) for index, t in enumerate(rows, 1)]


def persona_projection(profile):
    traits = profile['core']['data']['core_traits']
    style = profile['expression']['data']['style']
    require(isinstance(traits, list) and all(isinstance(t, str) for t in traits)
            and isinstance(style, str), 'persona_fields')
    return {'core_traits': traits, 'expression': {'style': style}}


def flash_base(model, persona, targets):
    # Only the three-field transport contract and the source data are model-visible.
    protocol = {'target': [t['id'] for t in targets] + [None],
                'action': [*ACTIONS, UNDETERMINED], 'task_state': [*STATES, UNDETERMINED]}
    messages = [{'role': 'system', 'content': compact({**persona, 'json_fields': protocol})}]
    for t in targets:
        messages.append({'role': t['role'], 'content': compact({'id': t['id'], 'source': t['origins'], 'text': t['text']})})
    flash = {'base_url': model['base_url'], 'name': 'deepseek-flash', 'max_tokens': 128, '_json_output': True}
    require_generation_provider(flash)
    return build_request(flash, messages)[0]


def flash_wire(base, instruction):
    require(isinstance(instruction, str), 'flash_instruction')
    first, *rest = base['messages']
    return base | {'messages': [first | {'content': '\n\n'.join(s for s in (instruction, first['content']) if s)}, *rest]}


def pro_wire(base, instruction, decision, targets):
    choice = validate_decision(decision, targets)
    if choice['target'] is None:
        return base
    require(isinstance(instruction, str), 'pro_instruction')
    target = next(t for t in targets if t['id'] == choice['target'])
    address = target['pro_target']
    index = address['message_index']
    require(type(index) is int and 0 <= index < len(base['messages'])
            and address.get('role') == target['role'], 'target_address')
    message = base['messages'][index]
    if 'reference_event' in address:
        require(message['role'] == 'system' and address['reference_event'] == target['event_id']
                and target['event_id'] in message['content'], 'reference_address')
    else:
        require(message['role'] == target['role'], 'target_role')
    block = join_blocks(instruction, choice | {'target': address})
    first, *rest = base['messages']
    require(first['role'] == 'system' and rest[-1]['role'] == 'user', 'pro_base')
    return base | {'messages': [first | {'content': first['content'] + '\n\n' + block}, *rest]}


@dataclass(frozen=True)
class Resolution:
    decision: dict | None
    reason: str


class DecisionWindow:
    """One resolution per dispatch. The receipt owner may still journal a late physical response."""
    def __init__(self, scope, targets, *, clock=time.monotonic):
        self.scope = scope
        self.targets = targets
        self.clock = clock
        self.started = None
        self.result = asyncio.get_running_loop().create_future()

    def dispatched(self):
        require(self.started is None and not self.result.done(), 'already_dispatched')
        self.started = self.clock()

    def _finish(self, decision, reason):
        if not self.result.done():
            self.result.set_result(Resolution(decision, reason))

    def cancel(self):
        self._finish(None, 'cancelled')

    def received(self, scope, raw):
        require(self.started is not None, 'dispatch_missing')
        if scope != self.scope or self.result.done():
            return False
        if self.clock() - self.started >= DEADLINE_SECONDS:
            self._finish(None, 'deadline')
            return False
        try:
            def unique(pairs):
                obj = dict(pairs)
                if len(obj) != len(pairs):
                    raise ValueError('duplicate_field')
                return obj
            value = validate_decision(json.loads(raw, object_pairs_hook=unique), self.targets)
        except (ValueError, TypeError, RuntimeErrorBase):
            self._finish(None, 'invalid')
        else:
            if value['target'] is None:
                self._finish(None, 'undetermined')
            else:
                self._finish(value, 'selected')
        return True

    async def resolve(self):
        if not self.result.done():
            require(self.started is not None, 'dispatch_missing')
            remaining = DEADLINE_SECONDS - (self.clock() - self.started)
            if remaining > 0:
                await asyncio.wait({self.result}, timeout=remaining)
            self._finish(None, 'deadline')
        return self.result.result()

    async def pro_request(self, scope, base, instruction, recheck):
        """Resolve once against the original turn, then recheck live source/permission eligibility."""
        require(scope == self.scope, 'wrong_turn')
        result = await self.resolve()
        require(result.reason != 'cancelled', 'turn_cancelled')
        recheck()
        if result.decision is None:
            return base, result.reason
        return pro_wire(base, instruction, result.decision, self.targets), result.reason
