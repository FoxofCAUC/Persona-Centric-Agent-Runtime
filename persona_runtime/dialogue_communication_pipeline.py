"""Isolated communication consumption. No production/default policy is changed."""
import asyncio
import hashlib
import json
from pathlib import Path

from .archive import encoded, fingerprint
from .dialogue_context import require, route
from .dialogue_pipeline import DialoguePipeline, collect, current
from .character_selector_protocol import apply_selection
from .character_context import render_parts
from .context_m4 import build_context
from .generation_retry import audited_complete
from .prompt_assembly import PromptBlock
from .communication_consumption import (joint_messages_v2, parse_joint_v2,
    projection_v2, decision_input_v2, UNDERSTANDING_VERSION, RENDERER_VERSION)

VERSION = 'cr3-consumption-v1'


def check_execution(engine, attachment, arm):
    path = engine.store.paths.home / 'execution-contract.json'
    require(hashlib.sha256(path.read_bytes()).hexdigest() == attachment['communication_contract_hash'],
            'communication_execution_changed')
    contract = json.loads(path.read_text('utf-8'))
    require(contract['version'] == VERSION and contract['private'] is False,
            'communication_execution_invalid')
    expected = 'dialogue-understanding-v1' if arm == 'control' else UNDERSTANDING_VERSION
    require(arm in ('control', 'candidate') and contract['conditions'][arm] == expected,
            'communication_execution_version')
    return contract


class CommunicationPipeline(DialoguePipeline):
    def recheck(self, engine, conn, turn, collected):
        check_execution(engine, self.attachment, 'candidate')
        super().recheck(engine, conn, turn, collected)

    async def prepare(self, engine, tid):
        check_execution(engine, self.attachment, 'candidate')
        c = await asyncio.to_thread(collect, engine, tid, self.attachment)
        require(self.expected_guard is None or c['source_guard'] == self.expected_guard,
                'cr2_prepared_sources_changed')
        shared = c['shared']; routing = route(shared); pool = json.loads(self.pool_json)
        require(fingerprint(pool) == self.attachment['pool_hash'], 'cr2_candidate_changed')
        result = None; context = ''; removed = []
        turn = current(engine, tid, self.attachment)
        if routing['path'] == 'joint':
            model = json.loads(turn['selector_model_snapshot']) | {'timeout': 240, 'max_tokens': 2048,
                '_json_output': True, '_disposition_compile': True, '_situation_observation': True}
            payload = joint_messages_v2(shared, pool, 4, model['max_input_chars'])
            def before(state):
                with engine.store.connection() as db:
                    self.recheck(engine, db, current(engine, tid, self.attachment, db), c)
            value = await audited_complete(engine.client, model, payload,
                validate=lambda text: parse_joint_v2(text, shared, pool), save=self.save_audit,
                before_attempt=before)
            result = parse_joint_v2(value[0], shared, pool)
            character = apply_selection(c['character_version'], c['members'], pool, result['selection'], 4)
            character.update(requested_mode='joint', effective_mode='joint', degrade_reason=None)
            context, removed = projection_v2(result, shared)
        else:
            character = c['local'] | {'requested_mode': 'direct_local', 'effective_mode': 'lexical', 'degrade_reason': None}
        with engine.store.connection() as db:
            turn = current(engine, tid, self.attachment, db); self.recheck(engine, db, turn, c)
        character = engine.cognitive.characters.prepare(turn, character)
        blocks = (PromptBlock('communication_spec', context, 'communication_consumption', RENDERER_VERSION),) if context else ()
        with engine.store.connection() as db:
            messages, details = build_context(engine, db, turn, c['archive_evidence'], character,
                shared_materials=c['materials_json'], experimental_blocks=blocks)
            require(details['history_rows'] == json.loads(c['materials_json'])['history_rows'], 'cr2_budget_plan_changed')
            require(len(render_parts(character['parts'])) <= 3000, 'cr2_character_budget')
            manifest = engine.cognitive._manifest(db, turn, messages, details)
        manifest['_shadow_prepared'] = None
        self.record = {'version': VERSION, 'shared': shared.document(), 'evidence_hash': shared.digest,
            'routing': routing, 'understanding': result,
            'understanding_status': 'completed' if result else 'not_executed',
            'communication_spec': result['communication_spec'] if result else None,
            'decision_input': decision_input_v2(result) if result else None,
            'projection': context, 'projection_removed': removed,
            'selection_returned': result['selection'] if result else None,
            'character': details['character'], 'budget': c['budget'], 'truncation': details['removed'],
            'input_hash': fingerprint(messages), 'messages': messages,
            'consumers': {'joint_input': shared.digest if result else None, 'expression_input': shared.digest,
                'communication_input': fingerprint(result['communication_spec']) if result else None}}
        return {'messages': messages, 'details': details, 'manifest': manifest,
            'character_prepared': character, 'guard': c['source_guard'],
            'accepted_policy_hash': fingerprint(turn['context_policy_snapshot']),
            '_dialogue_pipeline': True, '_shared_collected': c}
