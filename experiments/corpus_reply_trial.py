"""Approved fixed-context reply study; immutable requests and one physical attempt.

Language comes from the reviewed private files, frozen real inputs, original v8
assembly or actual model receipts. This module authors no persona or prompt text.
"""
import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import encoded, fingerprint, outside_repository
from persona_runtime.corpus_learning_transport import CorpusLearningHTTPClient
from persona_runtime.provider_compat import build_request, usage_fields
from .corpus_reply_study import read_private, save_once


VERSION = 'corpus-reply-four-condition-v1'
KINDS = ('A', 'B', 'U', 'C', 'D')
SCHEMA = '''
CREATE TABLE trial_meta(id INTEGER PRIMARY KEY CHECK(id=1),plan_hash TEXT NOT NULL,schema_hash TEXT NOT NULL,cancelled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE trial_calls(
 slot TEXT PRIMARY KEY,position INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('A','B','U','C','D')),
 state TEXT NOT NULL CHECK(state IN ('waiting','prepared','dispatched','completed','failed','unknown','cancelled','blocked')),
 model_json TEXT NOT NULL,request_json TEXT,request_hash TEXT,dependency_hash TEXT,
 attempt_id TEXT UNIQUE,started_epoch REAL,finished_epoch REAL,
 raw_response BLOB,raw_hash TEXT,http_status INTEGER,diagnostics_json TEXT,
 result_json TEXT,error_code TEXT,UNIQUE(position,kind));
CREATE TRIGGER trial_contract_immutable BEFORE UPDATE ON trial_calls
 WHEN NEW.slot IS NOT OLD.slot OR NEW.position IS NOT OLD.position OR NEW.kind IS NOT OLD.kind OR NEW.model_json IS NOT OLD.model_json
 OR (OLD.request_json IS NOT NULL AND (NEW.request_json IS NOT OLD.request_json OR NEW.request_hash IS NOT OLD.request_hash OR NEW.dependency_hash IS NOT OLD.dependency_hash))
 OR (OLD.attempt_id IS NOT NULL AND (NEW.attempt_id IS NOT OLD.attempt_id OR NEW.started_epoch IS NOT OLD.started_epoch))
 OR (OLD.raw_response IS NOT NULL AND (NEW.raw_response IS NOT OLD.raw_response OR NEW.raw_hash IS NOT OLD.raw_hash OR NEW.http_status IS NOT OLD.http_status))
 OR (OLD.result_json IS NOT NULL AND NEW.result_json IS NOT OLD.result_json)
 OR (OLD.state IN ('completed','failed','unknown','cancelled','blocked') AND NEW.state IS NOT OLD.state)
 BEGIN SELECT RAISE(ABORT,'trial first contract and receipt are immutable'); END;
CREATE TRIGGER trial_no_delete BEFORE DELETE ON trial_calls BEGIN SELECT RAISE(ABORT,'trial history is append only'); END;
CREATE TRIGGER trial_meta_contract BEFORE UPDATE ON trial_meta
 WHEN NEW.plan_hash IS NOT OLD.plan_hash OR NEW.schema_hash IS NOT OLD.schema_hash OR NEW.id IS NOT OLD.id OR NEW.cancelled<OLD.cancelled
 BEGIN SELECT RAISE(ABORT,'trial scope is immutable'); END;
'''


def substitute(template, values):
    keys = set(re.findall(r'\{([a-z_]+)\}', template))
    if keys != set(values):
        raise CorpusError('reply_trial_template_binding_invalid')
    return re.sub(r'\{([a-z_]+)\}', lambda m: values[m[1]], template)


def parse_receipt(raw, streaming=False):
    """Parse preserved HTTP bytes. Content is retained exactly, including spaces."""
    usage, model, finish, parts, done, tool = None, None, None, [], False, False
    try:
        if streaming:
            for line in raw.decode('utf-8').splitlines():
                if not line.startswith('data:'):
                    continue
                value = line[5:].strip()
                if value == '[DONE]':
                    done = True
                    continue
                item = json.loads(value)
                if item.get('usage') is not None:
                    usage = usage_fields(item['usage'], deepseek=True)
                if item.get('model'):
                    if model and model != item['model']:
                        raise ValueError()
                    model = item['model']
                for choice in item.get('choices', []):
                    delta = choice.get('delta') or {}
                    content = delta.get('content')
                    if content is not None:
                        if not isinstance(content, str):
                            raise ValueError()
                        parts.append(content)
                    tool |= bool(delta.get('tool_calls') or delta.get('function_call'))
                    if choice.get('finish_reason'):
                        finish = choice['finish_reason']
        else:
            item = json.loads(raw)
            usage = usage_fields(item.get('usage'), deepseek=True)
            model = item.get('model')
            choice = item['choices'][0]
            message = choice['message']
            parts = [message['content']]
            finish = choice.get('finish_reason')
            tool = bool(message.get('tool_calls') or message.get('function_call'))
            done = True
        body = ''.join(parts)
        valid = done and finish == 'stop' and not tool and bool(body.strip()) and len(body) <= 60000
        return {'body': body, 'body_hash': digest(body.encode()), 'model': model, 'usage': usage,
                'finish_reason': finish, 'stream_terminal': done, 'valid': valid,
                'incomplete_stream': streaming and not done, 'tool_calls': tool}
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
        return {'body': '', 'body_hash': digest(b''), 'model': model, 'usage': usage,
                'finish_reason': finish, 'stream_terminal': done, 'valid': False,
                'incomplete_stream': streaming and not done, 'tool_calls': tool}


def public_model(name, temperature):
    return {'base_url': 'https://api.deepseek.com', 'name': name, 'timeout': 240,
            'max_tokens': 8192, 'max_input_chars': 60000,
            '_dialogue_profile': {'version': 'natural-dialogue-v8', 'seed': 'EL4-WIRE-ONLY', 'temperature': temperature}}


def load_prompt_approval(path):
    path = outside_repository(path)
    approval = read_private(path)
    if (approval.get('version') != 'el4-ling-four-condition-author-approval-v1'
            or approval.get('positions') != 16 or approval.get('max_dispatches') != 80
            or approval.get('automatic_retries') != 0 or not approval.get('single_persona_research')
            or not approval.get('all_33_accepted_assets_allowed_for_this_role_research')
            or approval.get('new_prompt_prose_changes_authorized') is not False
            or approval.get('formal_deployment_authorized') is not False):
        raise CorpusError('reply_trial_author_scope_invalid')
    root = outside_repository(approval['prompt_root'])
    manifest = read_private(root / 'manifest.private.json')
    if digest((root / 'manifest.private.json').read_bytes()) != approval['prompt_manifest_sha256']:
        raise CorpusError('reply_trial_reviewed_manifest_changed')
    for item in manifest['files'] + manifest['auxiliary_files']:
        file = root / item['path']
        if file.resolve().parent != root or digest(file.read_bytes()) != item['sha256']:
            raise CorpusError('reply_trial_reviewed_material_changed')
    for key in ('positions', 'assets'):
        p = outside_repository(approval['prepared_' + key + '_path'])
        if digest(p.read_bytes()) != approval['prepared_' + key + '_sha256']:
            raise CorpusError('reply_trial_selected_material_changed')
    positions = read_private(approval['prepared_positions_path'])['positions']
    if (len(positions) != 16 or {p['position'] for p in positions} != set(range(1, 17))
            or len({p['group_id'] for p in positions}) != 16):
        raise CorpusError('reply_trial_positions_invalid')
    prompts = {i['path']: (root / i['path']).read_text(encoding='utf-8') for i in manifest['files']}
    return approval, prompts, read_private(root / 'bindings.private.json'), positions, read_private(root / 'persona-source.snapshot.private.json')


def reviewed_sender(template):
    sender = re.search(r'你正在和(.+?)聊天', template)
    if sender is None:
        raise CorpusError('reply_trial_reviewed_sender_unavailable')
    return sender[1]


def assemble_new(prompts, binding, position, accepted_at, *, understanding=None, assets=None):
    raw = position['input_text']
    # These wrappers are data from the approved private binding contract.
    variables = binding['variables']
    moment = datetime.fromisoformat(accepted_at).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    time_block = variables['time_block']['prefix'] + moment
    dialogue = variables['dialogue_prompt']['line_format'].format(time=moment, speaker=reviewed_sender(prompts['Pro-user.template.txt']), text=raw)
    if understanding is None:
        user = substitute(prompts['Flash-user.template.txt'], {'time_block': time_block, 'dialogue_prompt': dialogue, 'extra_info_block': ''})
        return [{'role': 'system', 'content': prompts['Flash-system.txt']}, {'role': 'user', 'content': user}]
    selected = []
    if assets is not None:
        by_id = {item['id']: item for item in assets['assets']}
        for ident in position['selection']['selected_ids']:
            if ident not in by_id:
                raise CorpusError('reply_trial_selected_asset_missing')
            item = by_id[ident]
            selected.append(variables['expression_habits_block']['item_format'].format(situation=item['situation'], style=item['style']))
    expression = ''
    if selected:
        expression = variables['expression_habits_block']['prefix'] + '\n'.join(selected) + variables['expression_habits_block']['suffix_if_present']
    if len(selected) > 5 or len(expression) > 3000:
        raise CorpusError('reply_trial_expression_capacity')
    user = substitute(prompts['Pro-user.template.txt'], {
        'extra_info_block': '', 'expression_habits_block': expression, 'time_block': time_block,
        'dialogue_prompt': dialogue, 'reply_target_block': variables['reply_target_block']['format'].format(current_user_text=raw),
        'planner_reasoning': variables['planner_reasoning']['format'].format(thought=understanding)})
    messages = [{'role': 'system', 'content': prompts['Pro-system.txt']}, {'role': 'user', 'content': user}]
    if len(encoded(messages)) > 60000:
        raise CorpusError('reply_trial_request_capacity')
    return messages


async def capture_baselines(root, positions, snapshot, source_home):
    """Exercise the existing v8 assembler with a client that cannot send HTTP."""
    from persona_runtime.paths import RuntimePaths
    from persona_runtime.config import ConfigStore
    from persona_runtime.storage import Store
    from persona_runtime.service import RuntimeService
    from persona_runtime.character import CharacterService
    from persona_runtime.models import PersonaSpec, identifier
    from persona_runtime.chat import ChatEngine, ModelError
    target = root / 'baseline-captures.private.json'
    if target.exists():
        return read_private(target)
    paths = RuntimePaths(root / 'baseline-instance')
    if paths.home.exists():
        raise CorpusError('reply_trial_partial_capture_requires_local_recovery')
    config = ConfigStore(paths)
    config.initialize()
    config.update({'model': {'base_url': 'https://api.deepseek.com', 'name': 'deepseek-flash', 'api_key': 'LOCAL-CAPTURE-ID-001',
                            'max_tokens': 8192, 'max_input_chars': 60000, 'timeout': 240}})
    store = Store(paths)
    store.initialize()
    service = RuntimeService(store)
    pid = service.create_persona(PersonaSpec.from_dict(snapshot['persona_spec']))['id']
    service.use_persona(pid)
    source_db = source_home / 'data/database/runtime.sqlite3'
    with closing(sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT data_json FROM character_versions WHERE id=?', (snapshot['character_version'],)).fetchone()
        if row is None:
            raise CorpusError('reply_trial_character_version_unavailable')
        character = json.loads(row[0])
    character['lorebook'] = [e for e in character['lorebook'] if e['enabled'] and e['visibility'] == 'conversation']
    character['examples'] = []
    characters = CharacterService(store)
    characters.update(pid, character, characters.current(pid)['id'], identifier())
    class Capture:
        def __init__(self):
            self.active = None
            self.saved = {}
        async def complete(self, model, messages):
            if model.get('_character_selection') or self.active in self.saved:
                raise CorpusError('reply_trial_unplanned_baseline_model')
            self.saved[self.active] = {'model': {k: v for k, v in model.items() if k != 'encrypted_key'}, 'messages': messages}
            raise ModelError('reply_trial_capture_only', diagnostics={'request_headers_sent': False, 'response_headers_received': False})
    capture = Capture()
    chat = ChatEngine(store, config, capture)
    result = {}
    try:
        for position in positions:
            capture.active = position['position']
            session = chat.session(fresh=True)['session_id']
            value = await chat.submit('desktop', position['input_text'], 'EL4-CAPTURE-' + str(capture.active), session_id=session)
            await chat.wait()
            with store.connection() as db:
                turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (value['id'],)).fetchone())
            captured = capture.saved.get(capture.active)
            if (not captured or turn['error_code'] != 'reply_trial_capture_only'
                    or captured['model'].get('_companion_profile', {}).get('version') != 'companion-runtime-v8'
                    or [m for m in captured['messages'] if m['role'] == 'user'][-1]['content'] != position['input_text']
                    or any(m['role'] == 'assistant' for m in captured['messages'])):
                raise CorpusError('reply_trial_original_v8_capture_invalid')
            result[str(capture.active)] = captured | {'accepted_at': turn['created_at'], 'turn_id': turn['id'], 'session_id': session}
    finally:
        chat.knowledge.close()
        chat.cognitive.characters.close()
    save_once(target, result)
    return result


def model_request(kind, capture, messages=None):
    if kind in ('A', 'B'):
        model = dict(capture['model'], name='deepseek-flash' if kind == 'A' else 'deepseek-v4-pro')
        request = build_request(model, capture['messages'])[0]
    else:
        model = public_model('deepseek-flash' if kind == 'U' else 'deepseek-v4-pro', .3 if kind == 'U' else 1.0)
        request = build_request(model, messages)[0] if messages is not None else None
    if request is not None and ('tools' in request or 'response_format' in request):
        raise CorpusError('reply_trial_unexpected_wire_contract')
    return model, request


class TrialLedger:
    def __init__(self, root, client=None, guard=None):
        self.root = outside_repository(root)
        self.path = self.root / 'trial.sqlite3'
        self.client = client or CorpusLearningHTTPClient()
        self.guard = guard or (lambda *_: None)

    def initialize(self, plan):
        slots = plan['calls']
        if (len(slots) != 80 or {s['slot'] for s in slots} != {f'{p:02}.{k}' for p in range(1, 17) for k in KINDS}
                or any(type(s['position']) is not int or s['slot'] != f"{s['position']:02}.{s['kind']}" for s in slots)):
            raise CorpusError('reply_trial_fixed_eighty_slots_required')
        self.root.mkdir(parents=True, exist_ok=True)
        digest_plan = fingerprint(plan)
        if self.path.exists():
            with self.connection() as db:
                if db.execute('SELECT plan_hash FROM trial_meta').fetchone()[0] != digest_plan:
                    raise CorpusError('reply_trial_plan_conflict')
            return
        with self.path.open('xb'):
            pass
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA)
            db.execute('INSERT INTO trial_meta VALUES(1,?,?,0)', (digest_plan, digest(SCHEMA.encode())))
            for slot in plan['calls']:
                request = slot['request']
                db.execute('INSERT INTO trial_calls(slot,position,kind,state,model_json,request_json,request_hash) VALUES(?,?,?,?,?,?,?)',
                           (slot['slot'], slot['position'], slot['kind'], 'prepared' if request else 'waiting', encoded(slot['model']),
                            encoded(request) if request else None, fingerprint(request) if request else None))
            db.execute('PRAGMA user_version=1')
            db.commit()

    @contextmanager
    def connection(self, write=False):
        if not self.path.is_file():
            raise CorpusError('reply_trial_ledger_missing')
        db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            meta = db.execute('SELECT * FROM trial_meta').fetchone()
            if not meta or meta['schema_hash'] != digest(SCHEMA.encode()) or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise CorpusError('reply_trial_schema_invalid')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def row(self, slot):
        with self.connection() as db:
            row = db.execute('SELECT * FROM trial_calls WHERE slot=?', (slot,)).fetchone()
            if row is None:
                raise CorpusError('reply_trial_slot_missing')
            return dict(row)

    def status(self):
        with self.connection() as db:
            rows = [dict(r) for r in db.execute('SELECT slot,position,kind,state,attempt_id,http_status,error_code FROM trial_calls ORDER BY position,kind')]
            return {'schema': 1, 'cancelled': bool(db.execute('SELECT cancelled FROM trial_meta').fetchone()[0]),
                    'attempts': sum(r['attempt_id'] is not None for r in rows), 'calls': rows}

    def bind(self, slot, model, request, dependency_hash):
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM trial_calls WHERE slot=?', (slot,)).fetchone()
            if not row or row['kind'] not in ('C', 'D') or row['model_json'] != encoded(model):
                raise CorpusError('reply_trial_dependency_contract_invalid')
            dependency = db.execute("SELECT * FROM trial_calls WHERE position=? AND kind='U'", (row['position'],)).fetchone()
            if not dependency or dependency['state'] != 'completed' or dependency['raw_hash'] != dependency_hash:
                raise CorpusError('reply_trial_understanding_receipt_unavailable')
            if row['request_json'] is not None:
                if row['request_json'] != encoded(request) or row['dependency_hash'] != dependency_hash:
                    raise CorpusError('reply_trial_dependency_rebind_forbidden')
                return
            if row['state'] != 'waiting' or row['attempt_id'] is not None:
                raise CorpusError('reply_trial_dependency_closed')
            db.execute("UPDATE trial_calls SET request_json=?,request_hash=?,dependency_hash=?,state='prepared' WHERE slot=?",
                       (encoded(request), fingerprint(request), dependency_hash, slot))

    def finalize(self, slot):
        row = self.row(slot)
        if row['result_json'] is not None or row['state'] in ('unknown', 'blocked', 'cancelled'):
            return self.row(slot)
        if row['raw_response'] is None or digest(row['raw_response']) != row['raw_hash']:
            raise CorpusError('reply_trial_raw_receipt_unavailable_or_changed')
        request = json.loads(row['request_json'])
        result = parse_receipt(row['raw_response'], request.get('stream', False))
        valid = row['http_status'] == 200 and result['valid']
        state = 'completed' if valid else 'unknown' if row['http_status'] == 200 and result['incomplete_stream'] else 'failed'
        with self.connection(write=True) as db:
            fresh = db.execute('SELECT * FROM trial_calls WHERE slot=?', (slot,)).fetchone()
            if fresh['state'] == 'dispatched':
                cancelled = db.execute('SELECT cancelled FROM trial_meta').fetchone()[0]
                db.execute('UPDATE trial_calls SET result_json=?,state=?,error_code=?,finished_epoch=? WHERE slot=?',
                           (encoded(result), 'cancelled' if cancelled else state, None if valid else 'reply_trial_provider_result_invalid', time.time(), slot))
        return self.row(slot)

    async def perform(self, slot, credentials):
        row = self.row(slot)
        if row['state'] != 'prepared' or row['attempt_id'] is not None:
            return row
        self.guard(row)
        model, request = json.loads(row['model_json']), json.loads(row['request_json'])
        if fingerprint(request) != row['request_hash'] or build_request(model, request['messages'])[0] != request:
            raise CorpusError('reply_trial_frozen_wire_changed')
        if credentials.get('base_url', '').rstrip('/') != model['base_url']:
            raise CorpusError('reply_trial_official_credential_origin_required')
        live = model | {'encrypted_key': credentials.get('encrypted_key', '')}
        self.client.preflight(live, request)
        with self.connection(write=True) as db:
            fresh = db.execute('SELECT * FROM trial_calls WHERE slot=?', (slot,)).fetchone()
            if fresh['state'] != 'prepared' or fresh['attempt_id'] is not None:
                return dict(fresh)
            if db.execute('SELECT cancelled FROM trial_meta').fetchone()[0]:
                raise CorpusError('reply_trial_cancelled')
            if db.execute('SELECT count(*) FROM trial_calls WHERE attempt_id IS NOT NULL').fetchone()[0] >= 80:
                raise CorpusError('reply_trial_budget_reached')
            attempt = str(uuid4())
            db.execute("UPDATE trial_calls SET state='dispatched',attempt_id=?,started_epoch=? WHERE slot=?", (attempt, time.time(), slot))
        def save_raw(raw, *, http_status, diagnostics):
            with self.connection(write=True) as db:
                fresh = db.execute('SELECT * FROM trial_calls WHERE slot=?', (slot,)).fetchone()
                if fresh['attempt_id'] != attempt:
                    raise CorpusError('reply_trial_receipt_attempt_conflict')
                if fresh['raw_response'] is not None:
                    if fresh['raw_hash'] == digest(raw) and fresh['http_status'] == http_status:
                        return
                    raise CorpusError('reply_trial_first_receipt_conflict')
                db.execute('UPDATE trial_calls SET raw_response=?,raw_hash=?,http_status=?,diagnostics_json=? WHERE slot=?',
                           (raw, digest(raw), http_status, encoded(diagnostics), slot))
        try:
            diagnostics = await self.client.complete(live, request, save_raw=save_raw)
            with self.connection(write=True) as db:
                db.execute('UPDATE trial_calls SET diagnostics_json=? WHERE slot=?', (encoded(diagnostics), slot))
        except BaseException as error:
            current = self.row(slot)
            if current['raw_response'] is None:
                diagnostics = getattr(error, 'diagnostics', {})
                unsent = diagnostics.get('request_headers_sent') is False and diagnostics.get('response_headers_received') is False
                with self.connection(write=True) as db:
                    cancelled = db.execute('SELECT cancelled FROM trial_meta').fetchone()[0]
                    db.execute('UPDATE trial_calls SET state=?,diagnostics_json=?,error_code=?,finished_epoch=? WHERE slot=? AND state=\'dispatched\'',
                               ('cancelled' if cancelled else 'failed' if unsent else 'unknown', encoded(diagnostics), 'reply_trial_transport_interrupted', time.time(), slot))
            if isinstance(error, asyncio.CancelledError) or not isinstance(error, Exception):
                raise
            if current['raw_response'] is None:
                return self.row(slot)
        result = self.finalize(slot)
        # Authority can be withdrawn during HTTP. Preserve the receipt and stop.
        self.guard(result)
        return result

    def recover(self):
        with self.connection() as db:
            pending = [r[0] for r in db.execute("SELECT slot FROM trial_calls WHERE state='dispatched'")]
        for slot in pending:
            row = self.row(slot)
            if row['raw_response'] is not None:
                self.finalize(slot)
            else:
                with self.connection(write=True) as db:
                    db.execute("UPDATE trial_calls SET state='unknown',error_code='reply_trial_interrupted_without_receipt',finished_epoch=? WHERE slot=?", (time.time(), slot))
        return self.status()

    def cancel(self):
        with self.connection(write=True) as db:
            db.execute('UPDATE trial_meta SET cancelled=1')
            db.execute("UPDATE trial_calls SET state='cancelled',error_code='reply_trial_cancelled' WHERE state IN ('waiting','prepared')")
        return self.status()


async def prepare(root, approval_path, corpus, learning, package_path, asset_acceptance, source_home):
    from .corpus_reply_study import adopted_assets
    root = outside_repository(root)
    plan_path = root / 'plan.private.json'
    if plan_path.exists():
        return read_private(plan_path)
    approval, prompts, binding, positions, snapshot = load_prompt_approval(approval_path)
    assets = adopted_assets(corpus, learning, package_path, asset_acceptance)
    if digest(Path(approval['prepared_assets_path']).read_bytes()) != approval['prepared_assets_sha256'] or read_private(approval['prepared_assets_path']) != assets:
        raise CorpusError('reply_trial_approved_assets_changed')
    root.mkdir(parents=True, exist_ok=True)
    captures = await capture_baselines(root, positions, snapshot, source_home)
    calls = []
    for position in positions:
        capture = captures[str(position['position'])]
        for kind in KINDS:
            messages = assemble_new(prompts, binding, position, capture['accepted_at']) if kind == 'U' else None
            model, request = model_request(kind, capture, messages)
            calls.append({'slot': f"{position['position']:02}.{kind}", 'position': position['position'], 'kind': kind, 'model': model, 'request': request})
    source_files = {str(Path(approval_path).resolve()): digest(Path(approval_path).read_bytes()),
                    str(Path(package_path).resolve()): digest(Path(package_path).read_bytes()),
                    str(Path(asset_acceptance).resolve()): digest(Path(asset_acceptance).read_bytes()),
                    str(root / 'baseline-captures.private.json'): digest((root / 'baseline-captures.private.json').read_bytes())}
    # Bind original learning contracts as well as their immutable SQL receipts.
    with learning.connection() as db:
        approvals = [dict(r) for r in db.execute('SELECT * FROM learning_approvals')]
        learning_rows = [dict(r) for r in db.execute('SELECT id,approval_id,state,request_hash,raw_response_hash FROM learning_jobs')]
    for row in approvals:
        saved = json.loads(row['binding_json'])
        a_path = Path(saved['approval_path'])
        directory = Path(saved['preview_root'])
        for p in [a_path, *directory.glob('*.json')]:
            source_files[str(p)] = digest(p.read_bytes())
    source_files[str(Path(__file__).resolve())] = digest(Path(__file__).read_bytes().replace(b'\r\n', b'\n'))
    plan = {'version': VERSION, 'approval_path': str(Path(approval_path).resolve()), 'approval_sha256': digest(Path(approval_path).read_bytes()),
            'max_dispatches': 80, 'scope': 'single_persona_fixed_context_16_positions', 'positions': positions,
            'prompts': prompts, 'binding': binding, 'persona_snapshot': snapshot, 'assets': assets,
            'source_home': str(source_home), 'view_id': positions[0]['view_id'], 'view_payload_hash': positions[0]['view_payload_hash'],
            'source_files': source_files, 'learning_rows': learning_rows,
            'learning_approval_ids': [a['id'] for a in approvals], 'calls': calls,
            'captures': captures, 'corpus_root': str(corpus.root), 'learning_root': str(learning.root),
            'automatic_retries': 0, 'formal_deployment': False, 'new_persona_prose': False}
    save_once(plan_path, plan)
    return plan


def bind_canonical_ledger(root, approval_path, plan):
    root = outside_repository(root)
    target = Path(approval_path).with_suffix('.run-binding.private.json')
    record = {'version': VERSION, 'root': str(root), 'plan_hash': fingerprint(plan), 'ledger': str(root / 'ledger/trial.sqlite3')}
    if target.exists():
        if read_private(target) != record or not (root / 'ledger/trial.sqlite3').is_file():
            raise CorpusError('reply_trial_original_ledger_required')
    else:
        if (root / 'ledger/trial.sqlite3').exists():
            raise CorpusError('reply_trial_unbound_ledger_refused')
        save_once(target, record)
        TrialLedger(root / 'ledger').initialize(plan)


def authority_guard(plan, corpus, learning):
    def guard(row):
        for path, expected in plan['source_files'].items():
            raw = Path(path).read_bytes()
            if Path(path).resolve() == Path(__file__).resolve():
                raw = raw.replace(b'\r\n', b'\n')
            if digest(raw) != expected:
                raise CorpusError('reply_trial_frozen_source_file_changed')
        approval, prompts, binding, positions, snapshot = load_prompt_approval(plan['approval_path'])
        if prompts != plan['prompts'] or binding != plan['binding'] or snapshot != plan['persona_snapshot'] or positions != plan['positions']:
            raise CorpusError('reply_trial_prompt_or_input_source_changed')
        view = corpus.view_status(plan['view_id'])
        if view['payload_hash'] != plan['view_payload_hash']:
            raise CorpusError('reply_trial_corpus_permission_changed')
        position = next(p for p in plan['positions'] if p['position'] == row['position'])
        with corpus.connection() as db:
            source = db.execute('SELECT split,data_json FROM corpus_view_messages WHERE view_id=? AND message_id=?', (plan['view_id'], position['message_id'])).fetchone()
            if not source or source['split'] != 'dev_review':
                raise CorpusError('reply_trial_source_split_changed')
            message = json.loads(source['data_json'])
            if message['text'] != position['input_text'] or digest(message['text'].encode()) != position['input_text_hash']:
                raise CorpusError('reply_trial_source_text_changed')
        with learning.connection() as db:
            for ident in plan['learning_approval_ids']:
                state = db.execute('SELECT state FROM learning_approvals WHERE id=?', (ident,)).fetchone()
                if not state or state[0] != 'active':
                    raise CorpusError('reply_trial_learning_permission_revoked')
            for expected in plan['learning_rows']:
                fresh = db.execute('SELECT id,approval_id,state,request_hash,raw_response_hash FROM learning_jobs WHERE id=?', (expected['id'],)).fetchone()
                if not fresh or dict(fresh) != expected:
                    raise CorpusError('reply_trial_learning_receipt_changed')
        source_db = Path(plan['source_home']) / 'data/database/runtime.sqlite3'
        with closing(sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True)) as db:
            current = db.execute('SELECT spec_json FROM personas WHERE id=?', (snapshot['persona_id'],)).fetchone()
            character = db.execute('SELECT version_id FROM character_heads WHERE persona_id=?', (snapshot['persona_id'],)).fetchone()
            if (not current or digest(current[0].encode()) != snapshot['spec_utf8_sha256']
                    or not character or character[0] != snapshot['character_version']):
                raise CorpusError('reply_trial_persona_authority_changed')
    return guard


async def execute(plan, ledger, credentials):
    prior = ledger.recover()
    if prior['cancelled'] or any(c['state'] == 'unknown' for c in prior['calls']):
        return prior
    failures = 0
    for position in plan['positions']:
        ordinal = position['position']
        first = ('A', 'B', 'U') if ordinal % 2 else ('B', 'A', 'U')
        slots = [f'{ordinal:02}.{kind}' for kind in first]
        previous = {s: ledger.row(s)['attempt_id'] for s in slots}
        results = await asyncio.gather(*(ledger.perform(s, credentials) for s in slots), return_exceptions=True)
        if any(isinstance(r, BaseException) for r in results):
            raise CorpusError('reply_trial_source_or_preflight_failed')
        for row in results:
            if previous[row['slot']] is None and row['attempt_id']:
                failures = failures + 1 if row['state'] == 'failed' else 0
            print(encoded({'position': ordinal, 'condition': row['kind'], 'state': row['state'], 'http_status': row['http_status']}), flush=True)
        if any(r['state'] in ('unknown', 'cancelled') or r['http_status'] in (401, 403, 429) for r in results) or failures >= 3:
            break
        understanding = ledger.row(f'{ordinal:02}.U')
        if understanding['state'] != 'completed':
            with ledger.connection(write=True) as db:
                db.execute("UPDATE trial_calls SET state='blocked',error_code='reply_trial_understanding_failed' WHERE position=? AND state='waiting'", (ordinal,))
            continue
        thought = json.loads(understanding['result_json'])['body']
        for kind in ('C', 'D'):
            model = json.loads(ledger.row(f'{ordinal:02}.{kind}')['model_json'])
            messages = assemble_new(plan['prompts'], plan['binding'], position, plan['captures'][str(ordinal)]['accepted_at'],
                                    understanding=thought, assets=plan['assets'] if kind == 'D' else None)
            ledger.bind(f'{ordinal:02}.{kind}', model, build_request(model, messages)[0], understanding['raw_hash'])
        second = ('C', 'D') if ordinal % 2 else ('D', 'C')
        slots = [f'{ordinal:02}.{k}' for k in second]
        previous = {s: ledger.row(s)['attempt_id'] for s in slots}
        results = await asyncio.gather(*(ledger.perform(s, credentials) for s in slots), return_exceptions=True)
        if any(isinstance(r, BaseException) for r in results):
            raise CorpusError('reply_trial_source_or_preflight_failed')
        for row in results:
            if previous[row['slot']] is None and row['attempt_id']:
                failures = failures + 1 if row['state'] == 'failed' else 0
            print(encoded({'position': ordinal, 'condition': row['kind'], 'state': row['state'], 'http_status': row['http_status']}), flush=True)
        if any(r['state'] in ('unknown', 'cancelled') or r['http_status'] in (401, 403, 429) for r in results) or failures >= 3:
            break
    return ledger.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'inspect', 'recover', 'cancel'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--corpus', type=Path)
    parser.add_argument('--learning', type=Path)
    parser.add_argument('--asset-package', type=Path)
    parser.add_argument('--asset-acceptance', type=Path)
    parser.add_argument('--source-home', type=Path)
    args = parser.parse_args(argv)
    from persona_runtime.corpus_dataset import DatasetCorpusStore
    from persona_runtime.corpus_learning import CorpusLearning
    if args.action == 'prepare':
        if not all((args.approval, args.corpus, args.learning, args.asset_package, args.asset_acceptance, args.source_home)):
            parser.error('all_explicit_source_paths_required')
        corpus = DatasetCorpusStore(args.corpus)
        learning = CorpusLearning(args.learning, corpus)
        plan = asyncio.run(prepare(args.root, args.approval, corpus, learning, args.asset_package, args.asset_acceptance, args.source_home))
        bind_canonical_ledger(args.root, args.approval, plan)
    else:
        plan = read_private(args.root / 'plan.private.json')
        corpus = DatasetCorpusStore(Path(plan['corpus_root']))
        learning = CorpusLearning(Path(plan['learning_root']), corpus)
        bind_canonical_ledger(args.root, Path(plan['approval_path']), plan)
    ledger = TrialLedger(args.root / 'ledger', guard=authority_guard(plan, corpus, learning))
    if args.action == 'run':
        from persona_runtime.config import ConfigStore
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.instance import InstanceLock
        from scripts.pcar_corpus_learning_preflight import inspect
        if not inspect(Path(__file__).resolve().parents[1])['passed']:
            raise CorpusError('reply_trial_core_preflight_failed')
        lock = InstanceLock(args.root / 'runner.lock')
        try:
            credentials = ConfigStore(RuntimePaths(Path(plan['source_home']))).load()['model']
            result = asyncio.run(execute(plan, ledger, credentials))
        finally:
            lock.close()
    elif args.action == 'recover':
        result = ledger.recover()
    elif args.action == 'cancel':
        result = ledger.cancel()
    else:
        result = ledger.status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
