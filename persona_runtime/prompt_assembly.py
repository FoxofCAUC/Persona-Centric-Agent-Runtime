"""Pure concatenation of rendered blocks; observations never select or rewrite text."""
from dataclasses import dataclass
from .archive import encoded, fingerprint

VERSION = 'prompt-assembly-r0-v1'
DIAGNOSTIC_BYTES = 16 * 1024
MANIFEST_BYTES = 128 * 1024
# Inventory only: these generation paths remain outside this assembler.
AUXILIARY_ROLES = (
    {'owner': 'character_selector_protocol', 'versions': ['character-selector-v1','character-selector-v2','character-selector-v3','character-selector-v4'], 'role': 'select_character_material'},
    {'owner': 'situation_observation', 'versions': ['situation-observation-v1','situation-observation-v2','situation-observation-v3'], 'role': 'observe_situation'},
    {'owner': 'organization', 'versions': ['existing-organization-render-r0'], 'role': 'propose_memory_and_summary'},
)


@dataclass(frozen=True)
class PromptBlock:
    name: str
    content: str
    owner: str
    version: str


def assemble(blocks, history, current, *, include_system=True):
    """Accept pre-rendered text in final order, with no IO or input mutation.

    Characters are Python string lengths, never estimates of token usage.
    Empty blocks are retained as absent components in the diagnostic only.
    """
    blocks = tuple(blocks)
    messages = ([{'role': 'system', 'content': ''.join(b.content for b in blocks)}]
                if include_system else [])
    messages += [dict(m) for m in history] + [dict(current)]
    rows = []
    offset = 0
    for block in blocks:
        rows.append({'name': block.name, 'owner': block.owner, 'version': block.version,
                     'message_index': 0, 'role': 'system', 'start_character': offset,
                     'characters': len(block.content), 'present': bool(block.content),
                     'content_hash': fingerprint(block.content)})
        offset += len(block.content)
    for index in range(1 if include_system else 0, len(messages)):
        message = messages[index]
        rows.append({'name': 'current_input' if index == len(messages)-1 else 'history',
                     'owner': 'turn_input' if index == len(messages)-1 else 'context_m4',
                     'version': 'existing-render-v1', 'message_index': index, 'role': message['role'],
                     'start_character': 0, 'characters': len(message['content']), 'present': True,
                     'content_hash': fingerprint(message['content'])})
    return messages, {'assembler_version': VERSION, 'character_unit': 'unicode_characters',
                      'blocks': rows, 'message_count': len(messages),
                      'input_characters': sum(len(m['content']) for m in messages),
                      'input_snapshot_hash': fingerprint(messages)}


def byte_size(value):
    return len(encoded(value).encode('utf-8'))


def bounded(diagnostic):
    if byte_size(diagnostic) <= DIAGNOSTIC_BYTES:
        return diagnostic
    # IDs, source references and per-block detail are the unbounded portions.
    summary = {k: v for k, v in diagnostic.items()
               if k not in ('blocks', 'source_refs', 'excluded_refs', 'truncation_info')}
    summary.update(details_omitted=True, omission_reason='diagnostic_size_limit',
                   block_count=len(diagnostic.get('blocks', [])))
    if 'material_selection' in summary:
        summary['material_selection']={k:v for k,v in summary['material_selection'].items()
                                       if k not in ('returned_example_ids','included_example_ids','fact_paths','lore_ids')}
        summary['material_selection']['ids_omitted']=True
    return summary if byte_size(summary) <= DIAGNOSTIC_BYTES else None


def fit_manifest(manifest):
    """Remove ONLY the optional field if it would break the old manifest limit."""
    if 'prompt_assembly' in manifest and byte_size(manifest) > MANIFEST_BYTES:
        return {k: v for k, v in manifest.items() if k != 'prompt_assembly'}
    return manifest


def material_selection(policy, details, record=None):
    """Only existing source IDs and ledger evidence, never generated explanations."""
    character=details.get('character') or {};retrieval=character.get('retrieval') or {}
    requested=(policy.get('character_retrieval') or {}).get('selector_contract',{}).get('prompt')
    mode=retrieval.get('effective_mode')
    record=record or {}
    returned=record.get('selected')
    returned_ids=returned.get('example_ids') if isinstance(returned,dict) else None
    included=character.get('example_ids',[])
    applied=bool(mode=='hybrid_llm' and record.get('status')=='completed' and record.get('prompt_version')==requested and requested)
    return {'version':'material-selection-r1-v1','requested_prompt':requested,
            'effective_mode':mode,'applied_prompt':requested if applied else None,
            'fallback_reason':retrieval.get('degrade_reason'),
            'selection_attempt_id':retrieval.get('selection_attempt_id'),
            'selector_status':record.get('status'),'selector_prompt':record.get('prompt_version'),
            'returned_example_ids':returned_ids,'returned_example_count':len(returned_ids) if returned_ids is not None else None,
            'included_example_ids':included,'included_example_count':len(included),
            'fact_paths':character.get('fact_paths',[]),'lore_ids':character.get('lore_ids',[]),
            'ids_omitted':False}


def attach(manifest, turn, details, selection_record=None):
    raw = details.get('prompt_assembly')
    if raw is None:
        return manifest
    # Values below are already validated by the existing builder/manifest path.
    import json
    try:
        model = json.loads(turn['model_snapshot'])
        policy = json.loads(turn.get('context_policy_snapshot') or '{}')
    except (ValueError, TypeError):
        return manifest
    if not isinstance(model, dict) or not isinstance(policy, dict):
        return manifest
    voice = model.get('_dialogue_profile')
    mode = policy.get('disposition_mode', 'baseline')
    meta = details.get('decision_capsule') or {}
    applied = bool(meta.get('applied_to_prompt'))
    renderer = meta.get('renderer') if applied else None
    if not applied:
        reason = meta.get('degrade_reason') or ('provider_probe' if turn['channel']=='test' else
                 'shadow_observation_only' if mode=='shadow' else 'baseline_mode' if mode=='baseline' else 'not_applicable')
    else:
        reason = None
    blocks = [dict(row) for row in raw['blocks']]
    for row in blocks:
        if row['name']=='capsule' and renderer: row['version'] = renderer
    diagnostic = {**raw, 'blocks': blocks,
        'requested': {'dialogue_voice': voice['version'] if voice else None,
                      'reply_mode': turn.get('reply_mode_snapshot') or 'legacy_full',
                      'expression_version': manifest['reply_output_contract']['expression_version'],
                      'capsule_mode': mode},
        'effective': {'expression': raw['expression_renderer'], 'format': raw['format_renderer'],
                      'wire': manifest['reply_output_contract'],
                      'capsule': {'requested_mode': mode, 'applied': applied, 'renderer': renderer,
                                  'reason': reason}},
        'source_refs': {'state': manifest['included_state_refs'], 'evidence': manifest['evidence_refs'],
                        'character': {k:v for k,v in (details.get('character') or {}).items()
                                      if k in ('version_id','revision','fact_paths','lore_ids','example_ids')},
                        'references': details.get('references')},
        'excluded_refs': manifest['excluded_state_refs'], 'truncation_info': manifest['truncation_info'],
        'source_checks': {'relationship_requires_review': bool(details.get('relationship_requires_review')),
                          'state_version': details.get('state_version')},
        'auxiliary_registry': AUXILIARY_ROLES,
        'material_selection': material_selection(policy,details,selection_record),
        'details_omitted': False}
    diagnostic = bounded(diagnostic)
    return fit_manifest(manifest | {'prompt_assembly': diagnostic}) if diagnostic else manifest
