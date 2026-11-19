"""Frozen requests, first physical receipts and cancellation for the direct character reply."""
import asyncio
from contextlib import contextmanager, nullcontext
import json
import re

from .corpus_files import CorpusError, digest
from .corpus_learning_transport import CorpusLearningHTTPClient
from .corpus_store import encoded, fingerprint
from .dialogue_receipt import parse_receipt
from .expression_assets import ExpressionAssets, persona, revision_checked
from .models import RuntimeErrorBase, identifier, timestamp
from .private_personas import guard_cloud_dispatch, PrivatePersonas
from .provider_compat import build_request
from .provider_policy import require_generation_provider


VERSION = 'direct-persona-generation-v1'
DEFAULTS = {'version': VERSION, 'enabled': False, 'bundle_id': None}
STAGE_MODELS = {'understanding': 'deepseek-flash', 'selection': 'deepseek-flash', 'reply': 'deepseek-v4-pro'}
PERMISSIONS = {key: 'dialogue_' + key for key in STAGE_MODELS}


def require(ok, reason):
    if not ok:
        raise RuntimeErrorBase('dialogue_' + reason)


@contextmanager
def source_check():
    try:
        yield
    except CorpusError as error:
        raise RuntimeErrorBase(str(error)) from error
    except OSError as error:
        raise RuntimeErrorBase('dialogue_material_unavailable') from error


def available(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='dialogue_generation_turns'").fetchone())


def accepted(db, turn_id):
    row = db.execute('SELECT policy_json FROM dialogue_generation_turns WHERE turn_id=?',
                     (turn_id,)).fetchone() if available(db) else None
    return json.loads(row[0]) if row else {'revision': 0, 'data': dict(DEFAULTS)}


def enabled(db, turn_id):
    return accepted(db, turn_id)['data']['enabled']


class GenerationHTTPClient(CorpusLearningHTTPClient):
    @staticmethod
    def preflight(model, wire, **transport_profile):
        route = CorpusLearningHTTPClient.preflight(model, wire, **transport_profile)
        require(route[3] not in encoded(wire), 'credential_in_request')
        return route


class DialogueGeneration:
    accepted = staticmethod(accepted)
    enabled = staticmethod(enabled)

    def __init__(self, store, client=None, context_guard=None):
        self.store = store
        self.client = client if client is not None else GenerationHTTPClient()
        self.active = set()
        if context_guard is None:
            from .expression_sources import validate
            context_guard = lambda db, turn, context: validate(store, db, turn, context)
        self.context_guard = context_guard

    def policy(self, persona_id):
        with self.store.connection() as db:
            return self._policy(db, persona_id)

    def authorize(self, persona_id, stage, expected_provider, operation_id):
        require(stage in STAGE_MODELS, 'permission_stage')
        return PrivatePersonas(self.store.paths).authorize_model(
            self.store, persona_id, PERMISSIONS[stage], expected_provider, operation_id)

    def load_assembly(self, db, persona_id, bundle_id):
        from .dialogue_assembly import load_bundle
        from .dialogue_portable import adapt_bundle
        with source_check():
            return adapt_bundle(load_bundle(self.store, db, persona_id, bundle_id))

    def configure(self, persona_id, changes, expected_revision, operation_id):
        revision_checked(expected_revision)
        require(isinstance(changes, dict) and set(changes) <= {'enabled', 'bundle_id'}, 'policy_invalid')
        with self.store.connection(write=True) as db:
            def apply():
                old = self._policy(db, persona_id)
                require(old['revision'] == expected_revision, 'policy_conflict')
                new = old['data'] | changes
                require(type(new['enabled']) is bool and (new['bundle_id'] is None or
                        isinstance(new['bundle_id'], str) and re.fullmatch('[0-9a-f]{64}', new['bundle_id'])),
                        'policy_invalid')
                if new['enabled']:
                    require(new['bundle_id'], 'bundle_required')
                    self.load_assembly(db, persona_id, new['bundle_id'])
                db.execute("""INSERT INTO dialogue_generation_policies VALUES(?,?,?) ON CONFLICT(persona_id)
                           DO UPDATE SET revision=excluded.revision,data_json=excluded.data_json""",
                           (persona_id, expected_revision + 1, encoded(new)))
                return {'revision': expected_revision + 1, 'data': new}
            return ExpressionAssets(self.store)._operation(db, persona_id, operation_id, 'dialogue_generation_policy',
                                                          [expected_revision, changes], apply)

    @staticmethod
    def _policy(db, persona_id):
        persona(db, persona_id)
        row = db.execute('SELECT * FROM dialogue_generation_policies WHERE persona_id=?', (persona_id,)).fetchone()
        return {'revision': row['revision'], 'data': json.loads(row['data_json'])} if row else {
            'revision': 0, 'data': dict(DEFAULTS)}

    def freeze(self, db, turn_id, persona_id):
        turn = db.execute('SELECT persona_id,channel FROM turns WHERE id=?', (turn_id,)).fetchone()
        require(turn and tuple(turn) == (persona_id, 'desktop'), 'turn_scope')
        # Only acceptance may freeze an enabled policy, never retrofit an existing turn.
        return accepted(db, turn_id)

    def register(self, db, turn, phase, material, context):
        require(phase in ('main', 'search_notice') and turn['channel'] == 'desktop', 'phase_scope')
        require(self.enabled(db, turn['id']), 'turn_policy_disabled')
        require(material.get('bundle_id') == self.accepted(db, turn['id'])['data']['bundle_id'], 'bundle_scope')
        frozen = (encoded(material), encoded(context), turn['execution_revision'])
        old = db.execute('SELECT * FROM dialogue_generation_units WHERE turn_id=? AND phase=?',
                         (turn['id'], phase)).fetchone()
        if old:
            require(frozen == (old['material_json'], old['context_json'], old['execution_revision']),
                    'unit_material_changed')
            return old['id']
        uid = identifier()
        db.execute('INSERT INTO dialogue_generation_units VALUES(?,?,?,?,?,?,?)',
                   (uid, turn['id'], phase, *frozen, timestamp()))
        return uid

    def _scope(self, db, unit_id):
        row = db.execute('SELECT * FROM dialogue_generation_units WHERE id=?', (unit_id,)).fetchone()
        require(row, 'unit_missing')
        unit = dict(row)
        turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (unit['turn_id'],)).fetchone())
        root = db.execute('SELECT * FROM dialogue_generation_turns WHERE turn_id=?', (turn['id'],)).fetchone()
        require(root['closed_at'] is None and not turn.get('cancelled_at') and
                turn.get('error_code') != 'migration_requires_review', 'turn_closed')
        require(turn['channel'] == 'desktop' and turn['execution_revision'] == unit['execution_revision'] and
                self.store.current(db, required=False) == turn['persona_id'], 'turn_scope_changed')
        require(turn['status'] in ('queued', 'running', 'interrupted', 'failed') and not turn['reply_event_id'],
                'turn_inactive')
        policy = json.loads(root['policy_json'])
        require(policy['data']['enabled'] and self._policy(db, turn['persona_id']) == policy, 'policy_changed')
        return turn, unit, policy['data']

    def validate(self, db, unit_id, assembly):
        turn, unit, policy = self._scope(db, unit_id)
        material = json.loads(unit['material_json'])
        self.context_guard(db, turn, json.loads(unit['context_json']))
        with source_check():
            assembly.guard(db, turn, material)
        for row in db.execute('SELECT stage,model_json FROM dialogue_generation_stages WHERE unit_id=?', (unit_id,)):
            guard_cloud_dispatch(self.store, turn['persona_id'], PERMISSIONS[row['stage']], json.loads(row['model_json']))
        return turn, unit, material

    @staticmethod
    def _wire(stage, public, wire):
        require_generation_provider(public)
        require(not public.get('encrypted_key') and public['name'] == STAGE_MODELS[stage] and
                wire.get('model') == public['name'] and build_request(public, wire['messages'])[0] == wire and
                wire.get('thinking') == {'type': 'disabled'} and not wire.get('tools') and
                wire.get('response_format') == ({'type': 'json_object'} if stage == 'selection' else None),
                'stage_model_contract')
        require(sum(len(m['content']) for m in wire['messages']) <= min(public['max_input_chars'], 60000),
                'request_capacity')

    @staticmethod
    def _finish(db, stage_id):
        row = db.execute('SELECT * FROM dialogue_generation_stages WHERE id=?', (stage_id,)).fetchone()
        raw = row['raw_response']
        if raw is None or row['result_json'] is not None:
            return
        require(digest(raw) == row['raw_hash'], 'receipt_changed')
        wire = json.loads(row['request_json'])
        parsed = parse_receipt(raw, wire['stream'])
        valid = row['http_status'] == 200 and parsed['valid'] and parsed['model'] == wire['model']
        unknown = row['http_status'] == 200 and parsed['incomplete_stream']
        state = 'completed' if valid else 'unknown' if unknown else 'failed'
        # A response received after an unknown outcome remains evidence only.
        if row['state'] == 'sending':
            db.execute('UPDATE dialogue_generation_stages SET state=?,error_code=?,result_json=? WHERE id=?',
                       (state, None if valid else 'dialogue_response_invalid', encoded(parsed), stage_id))
        else:
            db.execute('UPDATE dialogue_generation_stages SET result_json=? WHERE id=?', (encoded(parsed), stage_id))

    @staticmethod
    def _result(db, stage_id):
        row = db.execute('SELECT * FROM dialogue_generation_stages WHERE id=?', (stage_id,)).fetchone()
        result = json.loads(row['result_json'] or '{}')
        return {'state': row['state'], 'stage_id': row['id'], 'stage': row['stage'], 'raw_hash': row['raw_hash'],
                'unit_id': row['unit_id'], 'body': result.get('body') if row['state'] == 'completed' else None,
                'error_code': row['error_code']}

    async def _stage(self, unit_id, stage, credential_model, assembly, thought, ids, dependencies):
        window = getattr(assembly, 'authority_window', nullcontext)
        with window(), self.store.connection(write=True) as db:
            turn, unit, material = self.validate(db, unit_id, assembly)
            old = db.execute('SELECT * FROM dialogue_generation_stages WHERE unit_id=? AND stage=?',
                             (unit_id, stage)).fetchone()
            if old:
                require(old['dependencies_json'] == encoded(dependencies), 'stage_dependency_changed')
                self._finish(db, old['id'])
                return self._result(db, old['id'])
            request_kind = 'search_notice' if unit['phase'] == 'search_notice' else stage
            public, wire = assembly.request(request_kind, turn, material, thought, ids)
            self._wire(stage, public, wire)
            # Both routes must be official. The physical request and its private
            # permission retain the exact endpoint of the frozen stage.
            require_generation_provider({'base_url':credential_model.get('base_url'), 'name':public['name']})
            live = dict(public, encrypted_key=credential_model['encrypted_key'])
            from .history_context import validate as validate_history
            proof = json.loads(unit["context_json"]).get("guard", {}).get("import_history_sources")
            validate_history(db,turn,proof,recipient=live)
            guard_cloud_dispatch(self.store, turn['persona_id'], PERMISSIONS[stage], live)
            preflight = getattr(self.client, 'preflight', None)
            if preflight:
                try:
                    preflight(live, wire)
                except (ValueError, OSError, KeyError) as error:
                    raise RuntimeErrorBase('dialogue_transport_preflight_blocked') from error
            sid, attempt = identifier(), identifier()
            db.execute("""INSERT INTO dialogue_generation_stages
              (id,unit_id,stage,state,model_json,request_json,request_hash,dependencies_json,attempt_id,started_at)
              VALUES(?,?,?,'sending',?,?,?,?,?,?)""",
                       (sid, unit_id, stage, encoded(public), encoded(wire), fingerprint(wire),
                        encoded(dependencies), attempt, timestamp()))

        def save_raw(raw, *, http_status, diagnostics):
            raw = bytes(raw)
            raw_hash = digest(raw)
            with self.store.connection(write=True) as db:
                prior = db.execute('SELECT raw_hash,http_status FROM dialogue_generation_stages WHERE id=? AND attempt_id=?',
                                   (sid, attempt)).fetchone()
                require(prior is not None, 'attempt_lost')
                if prior['raw_hash'] is not None:
                    require(tuple(prior) == (raw_hash, http_status), 'receipt_conflict')
                    return
                db.execute("""UPDATE dialogue_generation_stages SET raw_response=?,raw_hash=?,http_status=?,transport_json=?
                             WHERE id=?""", (raw, raw_hash, http_status, encoded(diagnostics), sid))

        self.active.add(sid)
        diagnostics = {}
        try:
            diagnostics = await self.client.complete(live, wire, save_raw=save_raw) or {}
        except (Exception, asyncio.CancelledError) as error:
            diagnostics = getattr(error, 'diagnostics', {}) or {}
            with self.store.connection(write=True) as db:
                no_send = (diagnostics.get('request_headers_sent') is False and
                           diagnostics.get('response_headers_received') is False)
                http_failure = (diagnostics.get('response_headers_received') is True and
                                type(diagnostics.get('http_status')) is int and diagnostics['http_status'] != 200)
                state = 'failed' if no_send or http_failure else 'unknown'
                code = 'dialogue_not_dispatched' if no_send else 'dialogue_transport_' + state
                db.execute("""UPDATE dialogue_generation_stages SET state=?,error_code=?,transport_json=?
                              WHERE id=? AND state='sending' AND raw_response IS NULL""",
                           (state, code, encoded(diagnostics), sid))
            if isinstance(error, asyncio.CancelledError):
                raise
        finally:
            self.active.discard(sid)
        with self.store.connection(write=True) as db:
            self._finish(db, sid)
            db.execute("""UPDATE dialogue_generation_stages SET state='unknown',error_code='dialogue_receipt_missing',
                          transport_json=? WHERE id=? AND state='sending' AND raw_response IS NULL""",
                       (encoded(diagnostics), sid))
            return self._result(db, sid)

    def _artifact(self, result, assembly):
        with self.store.connection(write=True) as db:
            _, _, material = self.validate(db, result['unit_id'], assembly)
            row = db.execute('SELECT artifact_json,validation_error FROM dialogue_generation_stages WHERE id=?',
                             (result['stage_id'],)).fetchone()
            if row['validation_error']:
                return None
            if row['artifact_json'] is not None:
                return json.loads(row['artifact_json'])
            try:
                if result['stage'] == 'selection':
                    artifact = assembly.parse_selection(result['body'], material)
                    require(isinstance(artifact,list) and len(artifact) <= 5 and
                            all(isinstance(k,str) and k for k in artifact) and len(set(artifact)) == len(artifact),
                            'selected_ids_invalid')
                else:
                    artifact = assembly.projection(result['body'], material)
                    require(isinstance(artifact['display_body'],str) and artifact['display_body'].strip(),
                            'display_body_invalid')
            except (ValueError, KeyError, TypeError):
                db.execute("UPDATE dialogue_generation_stages SET validation_error=? WHERE id=?",
                           ('dialogue_' + result['stage'] + '_artifact_invalid', result['stage_id']))
                return None
            db.execute("UPDATE dialogue_generation_stages SET artifact_json=? WHERE id=?",
                       (encoded(artifact), result['stage_id']))
            return artifact

    async def execute(self, unit_id, credential_model, assembly):
        with self.store.connection() as db:
            _, unit, material = self.validate(db, unit_id, assembly)
        thought, ids, deps = None, [], []
        if unit['phase'] == 'main':
            understood = await self._stage(unit_id, 'understanding', credential_model, assembly, thought, ids, deps)
            if understood['state'] != 'completed':
                return understood
            thought = understood['body']
            deps.append(understood['raw_hash'])
            if assembly.selectable(material):
                chosen = await self._stage(unit_id, 'selection', credential_model, assembly, thought, ids, deps)
                if chosen['state'] != 'completed':
                    return chosen
                ids = self._artifact(chosen, assembly)
                if ids is None:
                    return dict(chosen,state='failed',body=None,error_code='dialogue_selection_artifact_invalid')
                deps.append(chosen['raw_hash'])
        result = await self._stage(unit_id, 'reply', credential_model, assembly, thought, ids, deps)
        if result['state'] == 'completed':
            view = self._artifact(result, assembly)
            if view is None:
                return dict(result,state='failed',body=None,error_code='dialogue_reply_artifact_invalid')
            result['body'], result['projection'] = view['display_body'], view
        return result

    def cancel_root(self, turn_id, reason='cancelled_by_user'):
        with self.store.connection(write=True) as db:
            if available(db):
                db.execute("""UPDATE dialogue_generation_turns SET closed_at=?,reason=?
                              WHERE turn_id=? AND closed_at IS NULL""", (timestamp(), reason, turn_id))

    def recover(self):
        with self.store.connection(write=True) as db:
            if available(db):
                rows = db.execute("""SELECT id FROM dialogue_generation_stages
                                     WHERE raw_response IS NOT NULL AND result_json IS NULL""").fetchall()
                for row in rows:
                    self._finish(db, row['id'])
                for row in db.execute("""SELECT id FROM dialogue_generation_stages
                                     WHERE state='sending' AND raw_response IS NULL""").fetchall():
                    if row['id'] not in self.active:
                        db.execute("""UPDATE dialogue_generation_stages SET state='unknown',
                                      error_code='service_restarted' WHERE id=?""", (row['id'],))
        return {'new_model_calls': 0}

    def status(self, turn_id):
        with self.store.connection() as db:
            stages = []
            for row in db.execute("""SELECT u.id AS unit_id,u.phase,s.id AS stage_id,
                s.stage,CASE WHEN s.validation_error IS NULL THEN s.state ELSE 'failed' END AS state,
                COALESCE(s.validation_error,s.error_code) AS error_code, s.attempt_id, s.raw_hash, s.http_status,
                json_extract(s.model_json,'$.name') AS model, json_extract(s.result_json,'$.usage') AS usage,
                json_extract(s.transport_json,'$.elapsed_ms') AS elapsed_ms
                FROM dialogue_generation_units u LEFT JOIN dialogue_generation_stages s
                ON s.unit_id=u.id WHERE u.turn_id=? ORDER BY u.rowid,s.rowid""", (turn_id,)):
                item = dict(row)
                item['usage'] = json.loads(item['usage']) if item['usage'] is not None else None
                item['actual_charge'] = None
                stages.append(item)
            root = db.execute('SELECT closed_at,reason FROM dialogue_generation_turns WHERE turn_id=?', (turn_id,)).fetchone()
            return {'turn_id': turn_id, 'enabled': enabled(db, turn_id), 'stages': stages,
                    'closed': bool(root and root['closed_at']), 'reason': root['reason'] if root else None}
