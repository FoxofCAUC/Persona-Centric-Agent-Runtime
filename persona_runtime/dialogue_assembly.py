"""Assemble the reviewed Flash/selector/v8 Pro contracts from local evidence."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3

from .archive import read_exact, turn_scope
from .corpus_files import CorpusError
from .dialogue_display import project_display
from .dialogue_repair import repair_messages
from .expression_corpus_sources import CANDIDATE, REPLAY, require, resolve_bundle, source_window
from .expression_protocol import bubble_plan, local_baseline_plan
from .expression_sources import protected
from .models import RuntimeErrorBase
from .provider_compat import build_request
from .provider_policy import require_generation_provider
from .turn_input import load_turn_input, render_input


@contextmanager
def checked_authority():
    try:
        yield
    except RuntimeErrorBase:
        raise
    except CorpusError as error:
        code = str(error)
        raise RuntimeErrorBase(code if re.fullmatch(r'(?:dialogue_source|corpus)_[a-z_]+', code)
                               else 'dialogue_source_unavailable') from None
    except (OSError, sqlite3.Error, ValueError, IndexError, KeyError, TypeError):
        raise RuntimeErrorBase('dialogue_source_unavailable') from None


def moment(value):
    instant = datetime.fromisoformat(value)
    require(instant.tzinfo is not None, 'timezone_required')
    return instant.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')


def fill(template, values):
    pattern = r'\{([a-z_]+)\}'
    require(set(re.findall(pattern, template)) == set(values), 'template_binding_invalid')
    return re.sub(pattern, lambda m: values[m[1]], template)


def live_history(db, turn, messages, details):
    expected = list(messages[1:-1])
    if not expected:
        return []
    rows, wire = [], []
    selected = turn_scope(db, turn)
    group_ids = [row['turn_id'] for row in details.get('history_groups', [])]
    if not group_ids:
        for ordinal in details.get('history_rows', []):
            row = db.execute('SELECT id FROM turns WHERE rowid=? AND persona_id=? AND session_id=?',
                             (ordinal, turn['persona_id'], turn['session_id'])).fetchone()
            require(row, 'history_withdrawn')
            group_ids.append(row[0])
    for group_id in group_ids:
        value = read_exact(db, selected, {'kind': 'turn', 'id': group_id})
        require(value.get('usable'), 'history_withdrawn')
        user = render_input(value['members'], bool(value['batch_id']))
        spoken = '\n'.join(item['text'] for item in value['replies'])
        require(spoken and value['members'], 'history_unspoken')
        used = db.execute('SELECT created_at FROM events WHERE id=?', (value['replies'][-1]['id'],)).fetchone()
        require(used, 'history_spoken_time_missing')
        wire.extend([{'role':'user','content':user}, {'role':'assistant','content':spoken}])
        rows.append(('\n'.join(m['text'] for m in value['members']), spoken, value['members'][0]['created_at'], used[0]))
    require(wire == expected, 'history_does_not_match_v8')
    return rows


def wire(model, messages):
    require_generation_provider(model)
    require(sum(len(m['content']) for m in messages) <= min(model['max_input_chars'], 60000), 'input_capacity')
    return model, build_request(model, messages)[0]


class DialogueAssembly:
    def __init__(self, store):
        self.store = store
        self._bundles = {}

    def _resolve(self, db, persona_id, bundle_id, purpose):
        with checked_authority():
            require(not self._bundles or bundle_id in self._bundles, 'assembly_bundle_changed')
            self._bundles[bundle_id] = resolve_bundle(db, self.store, persona_id, bundle_id, purpose)
        self._current_bundle_id = bundle_id
        return self._bundles[bundle_id]

    @contextmanager
    def authority_window(self):
        """Serialize the local dispatch/publication boundary with authoritative withdrawals."""
        manifest, _ = self._bundles[self._current_bundle_id]
        with ExitStack() as stack:
            with checked_authority():
                stack.enter_context(source_window(manifest))
            yield

    def guard(self, db, turn, material):
        with checked_authority():
            require(turn['channel'] == 'desktop' and material['turn_id'] == turn['id']
                    and material['persona_id'] == turn['persona_id'], 'wrong_turn')
            self._resolve(db, turn['persona_id'], material['bundle_id'], CANDIDATE)

    def freeze(self, db, turn, bundle_id, model, messages, *, phase='main', history=None, current=None, details=None):
        self._resolve(db, turn['persona_id'], bundle_id, CANDIDATE)
        with checked_authority():
            require(history is None and current is None and turn['channel'] == 'desktop', 'live_history_requires_authority')
            return self._freeze(db, turn, bundle_id, model, messages, phase, details or {})

    def _freeze(self, db, turn, bundle_id, model, messages, phase, details):
        require(phase in ('main', 'search_notice') and messages and messages[0]['role'] == 'system'
                and messages[-1]['role'] == 'user', 'v8_message_shape')
        members = load_turn_input(db, turn)
        require(members and all(m['role'] == 'user' for m in members), 'current_input_missing')
        target = render_input(members, bool(turn.get('input_batch_id')))
        history = live_history(db, turn, messages, details) if phase == 'main' else []
        result = self.material(turn, bundle_id, model, messages, phase, history, target, turn['created_at'])
        result['current'] = '\n'.join(m['text'] for m in members)
        result['current_lines'] = [[m['text'], m['created_at']] for m in members]
        return result

    @staticmethod
    def material(turn, bundle_id, model, messages, phase, history, current, accepted_at):
        require(phase in ('main', 'search_notice') and bool(current.strip()) and messages and
                all(isinstance(m, dict) and set(m) == {'role', 'content'} and isinstance(m['content'], str)
                    and m['role'] in ('user','assistant','system') for m in messages), 'message_shape')
        clean = {k: model[k] for k in ('name','base_url','max_tokens','max_input_chars','timeout','hourly_limit',
                 '_generation_profile','_reply_mode','_expression_snapshot','_companion_profile','_search_notice_profile')
                 if k in model}
        require_generation_provider(clean)
        require(clean.get('_companion_profile', {}).get('version') == 'companion-runtime-v8', 'v8_required')
        if phase == 'main':
            require(messages[-1] == {'role':'user', 'content':current}, 'v8_target_changed')
        else:
            from .knowledge_search_notice import VERSION
            require(clean.get('_search_notice_profile') == VERSION, 'reviewed_notice_required')
        moment(accepted_at)
        return {'version':'direct-reviewed-assembly-v1', 'bundle_id':bundle_id, 'turn_id':turn['id'],
                'persona_id':turn['persona_id'], 'phase':phase, 'base_model':clean, 'messages':messages,
                'current':current, 'accepted_at':accepted_at, 'history':[list(r) for r in history]}

    def selectable(self, material):
        return bool(self._bundles[material['bundle_id']][0]['candidate_numbers']) and material['phase'] == 'main'

    def parse_selection(self, body, material):
        numbers = self._bundles[material['bundle_id']][0]['candidate_numbers']
        value = json.loads(body)
        require(isinstance(value, dict) and set(value) == {'selected_ids'} and type(value['selected_ids']) is list,
                'selector_schema')
        ids = value['selected_ids']
        require(len(ids) <= 5 and all(type(i) is int and str(i) in numbers for i in ids)
                and len(set(ids)) == len(ids), 'selector_id_scope')
        return [numbers[str(i)] for i in ids]

    def request(self, stage, turn, material, thought, selected_ids):
        manifest, available = self._bundles[material['bundle_id']]
        variables = manifest['variables']
        current = material['current']
        if stage == 'search_notice':
            require(material['phase'] == stage, 'wrong_stage')
            return wire(material['base_model'] | {'name':'deepseek-v4-pro'}, material['messages'])
        require(material['phase'] == 'main', 'wrong_stage')
        stamp = moment(material['accepted_at'])
        time_block = variables['time_block']['prefix'] + stamp
        if stage == 'understanding':
            line = variables['dialogue_prompt']['line_format']
            lines = []
            for row in material['history']:
                require(len(row) in (3,4), 'history_shape')
                user, spoken, accepted_at = row[:3]
                lines.append(line.format(time=moment(accepted_at), speaker=manifest['speaker'], text=user))
                lines.append(line.format(time=moment(row[-1]), speaker=manifest['persona'], text=spoken))
            for content, at in material.get('current_lines', [(current, material['accepted_at'])]):
                lines.append(line.format(time=moment(at), speaker=manifest['speaker'], text=content))
            text = fill(manifest['flash_user'], {'time_block':time_block, 'dialogue_prompt':'\n'.join(lines),
                                                'extra_info_block':''})
            return wire(manifest['flash_model'], [{'role':'system','content':manifest['flash_system']},
                                                   {'role':'user','content':text}])
        if stage == 'selection':
            reference = variables['planner_reasoning']['format'].format(thought=thought)
            return wire(manifest['selector_model'], [manifest['selector_system'], {'role':'user','content':time_block},
                         {'role':'user','content':current}, {'role':'user','content':reference}])
        require(stage == 'reply' and len(selected_ids) <= 5 and len(set(selected_ids)) == len(selected_ids)
                and set(selected_ids) <= set(available), 'selected_scope')
        messages, _, _ = repair_messages(material['messages'], {'variables':variables}, thought,
                                        [available[i] for i in selected_ids], material['messages'][-1]['content'])
        return wire(material['base_model'] | {'name':'deepseek-v4-pro'}, messages)

    @staticmethod
    def projection(body, material):
        return project_display(body, user_text=material['current'])

    def display(self, body, material):
        return self.projection(body, material)['display_body']

    @staticmethod
    def segments(body, material):
        spans = protected(body)
        if material['phase'] == 'search_notice':
            return bubble_plan(body, (), [body])
        return local_baseline_plan({'draft':body, 'protected':spans})


def load_bundle(store, db, persona_id, bundle_id):
    assembly = DialogueAssembly(store)
    assembly._resolve(db, persona_id, bundle_id, CANDIDATE)
    return assembly


def load_offline_bundle(store, db, persona_id, bundle_id):
    assembly = DialogueAssembly(store)
    assembly._resolve(db, persona_id, bundle_id, REPLAY)
    return assembly
