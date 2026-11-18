"""Assemble new private conversations with previously reviewed model inputs."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from persona_runtime.character import CharacterService
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import CorpusError
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from persona_runtime.models import LOCAL_USER_ID, PersonaSpec, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .body_repair_material import repair_messages
from .body_repair_run import read_contract
from .corpus_reply_study import read_private
from .corpus_reply_trial import public_model, reviewed_sender, substitute
from .corpus_semantic_selector import parse_selected_ids
from .semantic_body_comparison import ReviewedSources, file_hash, frozen_json


def require(condition, reason):
    if not condition:
        raise CorpusError('fresh_scene_' + reason)


def timestamp(value):
    moment = datetime.fromisoformat(value)
    require(moment.tzinfo is not None, 'timezone_required')
    return moment.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')


def history_rows(history):
    for entry in history:
        require(len(entry) in (3, 4), 'history_shape')
        user, spoken, accepted_at = entry[:3]
        require(isinstance(user, str) and user.strip() and isinstance(spoken, str) and spoken.strip(),
                'complete_history_required')
        yield user, spoken, accepted_at, entry[3] if len(entry) == 4 else accepted_at


class OfflineChat(ChatEngine):
    def _schedule(self, turn_id):
        # The caller prepares a request locally. No generation task is scheduled.
        pass


class Assembly:
    def __init__(self, state_path, root):
        self.root = outside_repository(root)
        self.root.mkdir(parents=True, exist_ok=True)
        state = read_private(state_path)
        self.sources = ReviewedSources(state['selection_overall_approval'])
        require(self.sources.decision_hash == state['selection_overall_approval_sha256'], 'selection_decision_changed')
        self.approval_hashes = {outside_repository(state[key]): file_hash(state[key]) for key in (
            'semantic_body_use_approval', 'semantic_selector_approval', 'selection_overall_approval',
            'body_repair_manifest', 'body_repair_approval')}
        self.guard()
        self.plan = self.sources.plan
        self.binding = self.plan['binding']
        self.prompts = self.plan['prompts']
        self.assets = {a['id']: a for a in self.plan['assets']['assets']}
        self.snapshot = self.plan['persona_snapshot']
        self.persona = self.snapshot['persona_name']
        self.speaker = reviewed_sender(self.prompts['Pro-user.template.txt'])
        self.variables = self.binding['variables']
        spec = self.sources.packets[1]
        selector = frozen_json(self.sources.selector_root / spec['path'], spec['sha256'])
        self.selector_system = selector['request']['messages'][0]
        self.numbers = {int(i): proposal for i, proposal in selector['candidate_numbers'].items()}
        require(self.selector_system['role'] == 'system' and set(self.numbers.values()) == set(self.assets)
                and selector['candidate_numbers'] == self.sources.selector['candidate_numbers'], 'selector_pool_changed')
        self.selector_model = selector['model']
        self.understanding_model = public_model('deepseek-flash', .3)
        self.speech_model = self.sources.model
        repaired, _ = read_contract(state['body_repair_manifest'], state['body_repair_approval'])
        self.v8_model = repaired['positions'][0]['model']
        require(self.v8_model['_companion_profile']['version'] == 'companion-runtime-v8', 'v8_profile_changed')
        self.cache = {}

    def guard(self):
        require(all(file_hash(path) == expected for path, expected in self.approval_hashes.items()), 'review_changed')
        self.sources.guard({'position': 1})

    def _dialogue(self, current, accepted_at, history):
        line = self.variables['dialogue_prompt']['line_format']
        lines = []
        for user, spoken, user_at, spoken_at in history_rows(history):
            lines.append(line.format(time=timestamp(user_at), speaker=self.speaker, text=user))
            lines.append(line.format(time=timestamp(spoken_at), speaker=self.persona, text=spoken))
        lines.append(line.format(time=timestamp(accepted_at), speaker=self.speaker, text=current))
        return '\n'.join(lines)

    @staticmethod
    def _wire(model, messages):
        require(sum(len(m['content']) for m in messages) <= model['max_input_chars'], 'input_capacity')
        return model, build_request(model, messages)[0]

    def understand(self, current, accepted_at, history):
        messages = [{'role': 'system', 'content': self.prompts['Flash-system.txt']},
                    {'role': 'user', 'content': substitute(self.prompts['Flash-user.template.txt'], {
                        'time_block': self.variables['time_block']['prefix'] + timestamp(accepted_at),
                        'dialogue_prompt': self._dialogue(current, accepted_at, history), 'extra_info_block': ''})}]
        return self._wire(self.understanding_model, messages)

    def select(self, current, accepted_at, history, thought):
        # The four-message selector sees branch history through its own understanding.
        reference = self.variables['planner_reasoning']['format'].format(thought=thought)
        messages = [self.selector_system, {'role': 'user', 'content': self.variables['time_block']['prefix'] + timestamp(accepted_at)},
                    {'role': 'user', 'content': current}, {'role': 'user', 'content': reference}]
        return self._wire(self.selector_model, messages)

    def parse_ids(self, text):
        return [self.numbers[i] for i in parse_selected_ids(text, self.numbers)]

    def _selected(self, ids):
        require(len(ids) <= 5 and len(set(ids)) == len(ids) and all(i in self.assets for i in ids), 'asset_scope')
        return [self.assets[i] for i in ids]

    def speak_s(self, current, accepted_at, history, thought, ids):
        wrap = self.variables['expression_habits_block']
        lines = [wrap['item_format'].format(situation=a['situation'], style=a['style']) for a in self._selected(ids)]
        expression = (wrap['prefix'] + '\n'.join(lines) + wrap['suffix_if_present']) if lines else ''
        require(len(expression) <= 3000, 'expression_capacity')
        values = {'extra_info_block': '', 'expression_habits_block': expression,
                  'time_block': self.variables['time_block']['prefix'] + timestamp(accepted_at),
                  'dialogue_prompt': self._dialogue(current, accepted_at, history),
                  'reply_target_block': self.variables['reply_target_block']['format'].format(current_user_text=current),
                  'planner_reasoning': self.variables['planner_reasoning']['format'].format(thought=thought)}
        messages = [{'role': 'system', 'content': self.prompts['Pro-system.txt']},
                    {'role': 'user', 'content': substitute(self.prompts['Pro-user.template.txt'], values)}]
        require(len(encoded(messages)) <= 60000, 'speech_capacity')
        return self._wire(self.speech_model, messages)

    def speak_r(self, baseline_model, baseline_wire, current, thought, ids):
        require(baseline_model == self.v8_model and build_request(baseline_model, baseline_wire['messages'])[0] == baseline_wire,
                'v8_base_changed')
        messages, _, _ = repair_messages(baseline_wire['messages'], self.binding, thought, self._selected(ids), current)
        return self._wire(baseline_model, messages)

    async def baseline(self, scene_id, turn, current, accepted_at, history, variant):
        rows = list(history_rows(history))
        require(variant in ('B', 'R') and len(rows) + 1 == turn and isinstance(current, str) and current.strip(),
                'baseline_scope')
        timestamp(accepted_at)
        key = fingerprint([scene_id, turn, current, accepted_at, rows])
        if key not in self.cache:
            with TemporaryDirectory(dir=self.root, prefix='v8-') as scratch:
                self.cache[key] = await self._capture(Path(scratch), current, accepted_at, rows)
        return self.cache[key]

    async def _capture(self, directory, current, accepted_at, rows):
        paths = RuntimePaths(directory)
        config = ConfigStore(paths)
        config.initialize()
        config.update({'model': {k: self.v8_model[k] for k in ('base_url', 'name', 'timeout', 'max_tokens', 'max_input_chars')}})
        store = Store(paths)
        store.initialize()
        service = RuntimeService(store)
        pid = service.create_persona(PersonaSpec.from_dict(self.snapshot['persona_spec']))['id']
        service.use_persona(pid)
        source_db = Path(self.plan['source_home']) / 'data/database/runtime.sqlite3'
        with closing(sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT data_json FROM character_versions WHERE id=?', (self.snapshot['character_version'],)).fetchone()
        require(row is not None, 'character_version_missing')
        character = json.loads(row[0])
        character['lorebook'] = [e for e in character['lorebook'] if e['enabled'] and e['visibility'] == 'conversation']
        character['examples'] = []
        characters = CharacterService(store)
        characters.update(pid, character, characters.current(pid)['id'], identifier())
        chat = OfflineChat(store, config, client=object())
        try:
            session = chat.session(fresh=True)['session_id']
            for user, spoken, user_at, spoken_at in rows:
                previous = await chat.submit('desktop', user, identifier(), session_id=session)
                event_id = identifier()
                with store.connection(write=True) as db:
                    db.execute('UPDATE events SET created_at=? WHERE id=?', (user_at, previous['user_event_id']))
                    db.execute('INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,?,?,?)',
                               (event_id, session, pid, LOCAL_USER_ID, 'persona', spoken, spoken_at))
                    db.execute("UPDATE turns SET status='completed',reply_event_id=?,created_at=?,updated_at=? WHERE id=?",
                               (event_id, user_at, spoken_at, previous['id']))
            value = await chat.submit('desktop', current, identifier(), session_id=session)
            with store.connection(write=True) as db:
                db.execute('UPDATE events SET created_at=? WHERE id=?', (accepted_at, value['user_event_id']))
                db.execute('UPDATE turns SET created_at=?,updated_at=? WHERE id=?', (accepted_at, accepted_at, value['id']))
            prepared = await chat._prepare_context(value['id'])
            with store.connection() as db:
                turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (value['id'],)).fetchone())
                require(json.loads(turn['model_snapshot'])['_companion_profile'] == self.v8_model['_companion_profile'], 'v8_profile')
                actual_model = json.loads(turn['model_snapshot']) | {
                    '_reply_mode': turn['reply_mode_snapshot'], '_expression_snapshot': turn['expression_snapshot']}
                messages = chat.cognitive.finalize(db, turn, prepared)['messages']
            require(messages[-1] == {'role': 'user', 'content': current}, 'baseline_target')
            # Preserve v8's own history selection and verify each projected pair comes from this branch.
            expected = [(user, spoken) for user, spoken, _, _ in rows]
            middle = messages[1:-1]
            require(messages[0]['role'] == 'system' and len(middle) % 2 == 0, 'baseline_message_shape')
            for user, spoken in zip(middle[::2], middle[1::2], strict=True):
                pair = (user['content'], spoken['content'])
                require(user['role'] == 'user' and spoken['role'] == 'assistant' and pair in expected, 'foreign_history')
                expected = expected[expected.index(pair) + 1:]
            require(not rows or middle, 'history_missing')
            model, wire = self._wire(self.v8_model, messages)
            require(build_request(actual_model, messages)[0] == wire, 'v8_wire_changed')
            return model, wire
        finally:
            chat.knowledge.close()
            chat.cognitive.characters.close()
