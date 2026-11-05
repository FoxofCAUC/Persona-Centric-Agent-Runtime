"""Read-only evidence helpers for the isolated formulaic-response investigation.

No retrieval is rerun here. The caller supplies the complete selection produced
by CharacterRuntime and the finalized request, so a missing observation cannot
be silently replaced by a later query. Body-bearing results belong only inside
the caller's disposable copy home, never in ordinary logs or source control.
"""
from copy import deepcopy
import json

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import digest
from persona_runtime.character_policy import read as read_policy
from persona_runtime.character_runtime import validate_snapshot
from persona_runtime.models import RuntimeErrorBase

VERSION = 'companion-formulaic-audit-v1'


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


def _checked_messages(prepared, messages=None):
    messages = prepared['messages'] if messages is None else messages
    expected = prepared['manifest']['input_snapshot_hash']
    if fingerprint(messages) != expected:
        raise RuntimeErrorBase('formulaic_audit_request_mismatch')
    return messages


def _ids(value, kind):
    return set(value.get('example_ids' if kind == 'example' else 'lore_ids', []))


def _reasons(rows, kind, item):
    return list(dict.fromkeys(row['reason'] for row in rows
        if row.get('kind') == kind and row.get('item_id', row.get('id')) == item
        and row.get('reason')))


def audit_reachability(engine, turn, prepared, *, selection=None):
    """Describe current-run stages; absence of a stage is explicitly unknown.

    ``selection`` optionally retains the pre-final-recheck selection. The final
    selection and actual main-input IDs always come from ``prepared``. This
    function uses no index, embedding, cloud selector or generation client.
    """
    messages = _checked_messages(prepared)
    with engine.store.connection() as conn:
        version = validate_snapshot(conn, turn)
    final_selection = prepared.get('character_prepared')
    selection = selection if selection is not None else final_selection
    if version is None or selection is None:
        return {'version': VERSION, 'scope': 'current_request_only',
            'status': 'not_observed', 'reason': 'character_selection_missing',
            'request_hash': fingerprint(messages), 'materials': [],
            'historical_inference': False}
    if selection['version_id'] != version['id']:
        raise RuntimeErrorBase('formulaic_audit_character_version_mismatch')
    if selection.get('_selection_hash') and selection['_selection_hash'] != digest(
            {k: v for k, v in selection.items() if k != '_selection_hash'}):
        raise RuntimeErrorBase('formulaic_audit_selection_changed')
    policy = read_policy(_object(turn.get('context_policy_snapshot') or {}))
    final = prepared['details'].get('character')
    manifest_retrieval = prepared['manifest'].get('character_retrieval')
    if final is not None and manifest_retrieval is not None:
        for kind in ('example', 'lore'):
            if _ids(final, kind) != set(manifest_retrieval['selected'][kind]):
                raise RuntimeErrorBase('formulaic_audit_final_manifest_mismatch')
    hybrid = selection.get('effective_mode') == 'hybrid'
    raw_known = hybrid and 'raw_candidates' in selection
    rank_known = hybrid and 'candidates' in selection and 'rejected' in selection
    rows = []
    for kind, entries in (('example', version['data']['examples']), ('lore', version['data']['lorebook'])):
        raw = {r['item_id']: r for r in selection.get('raw_candidates', {}).get(kind, [])}
        ranked = {r['item_id']: (index + 1, r) for index, r in
                  enumerate(selection.get('candidates', {}).get(kind, []))}
        threshold = policy.get('config', {}).get('thresholds', {}).get(kind)
        for source in entries:
            item = source['id']
            eligible = source['enabled'] and (kind == 'example' or source['visibility'] == 'conversation')
            rejected = _reasons(selection.get('rejected', []), kind, item)
            trimmed = _reasons(selection.get('removed', []), kind, item)
            final_removed = _reasons((final or {}).get('removed', []), kind, item)
            score = raw.get(item, {}).get('score')
            lexical = item in ranked and 'lexical' in ranked[item][1].get('match_types', [])
            if not eligible:
                decision = 'disabled' if not source['enabled'] else 'hidden'
                if item in raw or item in ranked or item in _ids(selection, kind) or item in _ids(final or {}, kind):
                    raise RuntimeErrorBase('formulaic_audit_ineligible_material_observed')
            elif not rank_known:
                decision = 'not_observed'
            elif 'ambiguous_alias' in rejected:
                decision = 'ambiguous_alias'
            elif 'below_threshold' in rejected:
                decision = 'below_threshold'
            elif item in ranked:
                decision = ('lexical_bypass' if lexical and (score is None or score < threshold)
                            else 'passed')
            elif raw_known and item not in raw:
                decision = 'no_candidate'
            else:
                decision = 'not_ranked_reason_unrecorded'
            rows.append({'kind': kind, 'id': item, 'configured': True,
                'enabled': source['enabled'], 'source_eligible': bool(eligible),
                'source_hash': digest(source), 'body_hash': fingerprint(source['text']),
                'raw_semantic_recalled': item in raw if raw_known else None,
                'semantic_score': score if raw_known else None, 'threshold': threshold,
                'ranked_after_threshold': item in ranked if rank_known else None,
                'rank': ranked[item][0] if item in ranked and rank_known else None,
                'threshold_decision': decision, 'selection_rejection_reasons': rejected,
                'budget_retained': item in _ids(selection, kind),
                'budget_decision': ('retained' if item in _ids(selection, kind) else
                    'removed' if trimmed else 'not_reached' if rank_known and item not in ranked
                    else 'exclusion_reason_not_observed'),
                'budget_removal_reasons': trimmed,
                'final_in_model': item in _ids(final, kind) if final is not None else None,
                'final_removal_reasons': final_removed})
    example_rows = [r for r in rows if r['kind'] == 'example']
    return {'version': VERSION, 'scope': 'current_request_only', 'status': 'observed',
        'historical_inference': False, 'request_hash': fingerprint(messages),
        'character_version': version['id'], 'character_source_hash': digest(version),
        'policy_hash': digest(policy), 'requested_mode': selection.get('requested_mode'),
        'effective_mode': selection.get('effective_mode'), 'fallback_reason': selection.get('degrade_reason'),
        'final_effective_mode': (manifest_retrieval or {}).get('effective_mode'),
        'final_fallback_reason': (manifest_retrieval or {}).get('degrade_reason'),
        'stage_observed': {'raw_semantic': raw_known, 'ranking_and_threshold': rank_known,
                           'selection_budget': True, 'final_main_input': final is not None},
        'counts': {'configured_examples': len(example_rows),
            'enabled_examples': sum(r['source_eligible'] for r in example_rows),
            'raw_semantic_examples': sum(r['raw_semantic_recalled'] is True for r in example_rows) if raw_known else None,
            'threshold_retained_examples': sum(r['ranked_after_threshold'] is True for r in example_rows) if rank_known else None,
            'budget_retained_examples': sum(r['budget_retained'] for r in example_rows),
            'final_included_examples': sum(r['final_in_model'] is True for r in example_rows) if final is not None else None},
        'materials': rows}


SOURCE_CLASSES = {
    'persona_expression': 'runtime_instruction_and_author_profile',
    'relationship': 'author_relationship', 'temporary_state': 'manual_state',
    'behavior': 'runtime_guidance', 'presentation': 'runtime_guidance',
    'character': 'selected_author_material', 'memory_summary': 'memory_and_summary',
    'explicit_references': 'accepted_reference', 'reply_protocol': 'runtime_output_contract',
    'capsule': 'derived_runtime_guidance', 'history': 'conversation_record',
    'current_input': 'current_user_input',
}


def private_prompt_sources(turn, prepared, *, messages=None):
    """Exact body-bearing blocks and typed provenance, for manual reading only.

    This inventories provenance rather than detecting obligations by keywords.
    It assigns no naturalness score and makes no judgment about politeness.
    The caller must store the result only in the disposable copy home.
    """
    messages = _checked_messages(prepared, messages)
    assembly = prepared['details'].get('prompt_assembly') or {}
    if assembly.get('input_snapshot_hash') != fingerprint(messages) or 'blocks' not in assembly:
        raise RuntimeErrorBase('formulaic_audit_blocks_not_observed')
    blocks, coverage = [], [[] for _ in messages]
    for row in assembly['blocks']:
        index, start, size = row['message_index'], row['start_character'], row['characters']
        if (type(index) is not int or not 0 <= index < len(messages)
                or type(start) is not int or type(size) is not int or start < 0 or size < 0):
            raise RuntimeErrorBase('formulaic_audit_block_range_invalid')
        message = messages[index]
        body = message['content'][start:start + size]
        if (len(body) != size or message['role'] != row['role']
                or fingerprint(body) != row['content_hash']):
            raise RuntimeErrorBase('formulaic_audit_block_binding_mismatch')
        coverage[index].append((start, start + size))
        blocks.append({**deepcopy(row), 'body': body,
            'source_class': SOURCE_CLASSES.get(row['name'], 'unclassified_actual_block'),
            'obligation_review': {'status': 'not_reviewed', 'findings': []}})
    for message, ranges in zip(messages, coverage):
        offset = 0
        for start, end in ranges:
            if start != offset:
                raise RuntimeErrorBase('formulaic_audit_block_coverage_mismatch')
            offset = end
        if offset != len(message['content']):
            raise RuntimeErrorBase('formulaic_audit_block_coverage_mismatch')
    profile = _object(turn.get('profile_snapshot') or {})
    fields = []
    for kind in ('core', 'expression', 'relationship'):
        source = profile.get(kind)
        if not source:
            continue
        for name, value in source['data'].items():
            parent = 'persona_expression' if kind in ('core', 'expression') else 'relationship'
            included = any(b['name'] == parent and b['present'] for b in blocks)
            fields.append({'path': kind + '.' + name,
                'field_type': ('author_expression_guidance' if kind == 'expression' else
                               'author_relationship' if kind == 'relationship' else 'author_' + name),
                'value_type': type(value).__name__, 'source_version': source.get('id'),
                'source_revision': source.get('revision'), 'source_ids': source.get('source_ids', []),
                'source_hash': fingerprint(source), 'value_hash': fingerprint(value),
                'parent_block': parent, 'parent_block_present': included,
                'value': deepcopy(value), 'obligation_review': {'status': 'not_reviewed', 'findings': []}})
    return {'version': VERSION, 'private_only': True, 'scope': 'current_request_only',
        'historical_inference': False, 'request_hash': fingerprint(messages),
        'turn_id': turn['id'], 'execution_revision': turn.get('execution_revision'),
        'blocks': blocks, 'author_fields': fields,
        'source_refs': deepcopy({key: prepared['manifest'].get(key) for key in
            ('included_state_refs', 'evidence_refs', 'excluded_state_refs', 'truncation_info')}),
        'review_policy': 'manual_semantic_reading; no_keyword_naturalness_or_politeness_judgment'}
