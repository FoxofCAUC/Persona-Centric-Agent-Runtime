"""CR1 offline preparation. This checkpoint has no real execution action.

Material contracts are synthetic source-bound authoring checks, not admitted
runtime snapshots, recalled pools, selection results or main request evidence.
CR0 sources and evidence remain read-only.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from persona_runtime.archive import encoded, fingerprint
from persona_runtime.communication_spec import CommunicativeSpec
from persona_runtime.config import atomic_json
from persona_runtime.dialogue_frame import DialogueEvidence, DialogueFrame, Source, Statement, require
from persona_runtime.private_personas import safe
from tests.dialogue_diagnosis_fixture import fixture

HOME = Path('D:/Persona-Centric Agent Runtime')
OUT = HOME / 'exports/acceptance/PCAR-dialogue-refactor/CR1-oracle-diagnosis'
VERSION = 'cr1-material-check-v1'
CONDITIONS = ('B0', 'B1', 'B2')
COMMON_FIELDS = ('name', 'identity', 'facts', 'base_prompt', 'relationship')


def material_evidence(case, role_id, role):
    """Stable artificial authoring sources, explicitly not runtime authority."""
    def source(kind, alias, text, speaker='data'):
        return Source(kind, f'{case["id"]}-{role_id}-{alias}', fingerprint(text), text, speaker)

    aliases = {}
    current, history = [], []
    for index, text in enumerate(case['members']):
        src = source('current', f'C{index}', text, 'user')
        current.append(src)
        aliases[f'C{index}'] = src.key
    for index, pair in enumerate(case['history']):
        for letter, speaker in (('U', 'user'), ('A', 'assistant')):
            alias = f'H{index}{letter}'
            src = source('history', alias, pair[speaker], speaker)
            history.append(src)
            aliases[alias] = src.key
    references = []
    if case['reference']:
        require(case['reference'] in aliases, 'cr1_reference_alias_missing')
        original = next(s for s in history if s.key == aliases[case['reference']])
        src = source('reference', 'REF', original.text)
        references.append(src)
        aliases['REF'] = src.key
    projection = f'artificial-disposition:{role_id}:{fingerprint(role["disposition"])}'
    evidence = DialogueEvidence(
        turn_id=f'material-not-admitted:{case["id"]}:{role_id}', execution_revision=1,
        scope_json=encoded({'persona_id': f'artificial-cr1-{role_id}', 'user_id': 'artificial-user',
                            'channel': 'desktop', 'bot': '', 'peer': ''}),
        accepted_json=encoded({'authority': 'artificial-authoring-only-not-admitted',
                               'material_version': fixture()['version'],
                               'projection_refs': [projection],
                               'role_hash': fingerprint(role)}),
        current=tuple(current), history=tuple(history), references=tuple(references))
    return evidence, aliases


def bind_annotations(case, role_id, evidence, aliases):
    def statement(row):
        require(set(row) == {'text', 'sources', 'certainty'}, 'cr1_annotation_schema')
        require(all(alias in aliases for alias in row['sources']), 'cr1_annotation_alias_missing')
        return Statement(row['text'], tuple(aliases[alias] for alias in row['sources']), row['certainty'])

    frame = DialogueFrame(evidence.digest, **{
        field: tuple(statement(row) for row in rows) for field, rows in case['frame'].items()
    }).validate(evidence)
    spec_data = case['spec']
    role = spec_data['roles'][role_id]
    import json
    projection_refs = tuple(json.loads(evidence.accepted_json)['projection_refs'])
    spec = CommunicativeSpec(
        frame_hash=frame.digest, focus=spec_data['focus'], stance=role['stance'],
        obligations=tuple(statement(row) for row in spec_data['obligations']),
        avoid=tuple(statement(row) for row in spec_data['avoid']),
        optional_moves=tuple(role['optional_moves']), disposition_refs=projection_refs,
    ).validate(evidence, frame)
    catalog = {s.key: s for s in evidence.sources}
    audit = []
    for owner, fields in (('frame', {k: asdict(frame)[k] for k in frame.fields()}),
                          ('spec', {'obligations': asdict(spec)['obligations'], 'avoid': asdict(spec)['avoid']})):
        for field, rows in fields.items():
            for index, row in enumerate(rows):
                audit.append({'owner': owner, 'field': field, 'index': index,
                              'text': row['text'], 'certainty': row['certainty'],
                              'evidence': [{'source_id': sid, 'text': catalog[sid].text,
                                            'source_hash': catalog[sid].digest} for sid in row['source_ids']]})
    return frame, spec, audit


def generation_materials(data, case_id, role_id):
    """Allowlist for later isolated preparation; evaluator reasons stay out.

    This is not a provider payload or a frozen candidate pool. Actual admission,
    recall, selection and final runtime checks must still run in CR1.
    """
    case = next(case for case in data['cases'] if case['id'] == case_id)
    return deepcopy({'role': data['roles'][role_id],
                     'scene': {key: case[key] for key in ('id', 'history', 'members', 'reference')},
                     'frame_annotation': case['frame'],
                     'spec_annotation': {key: case['spec'][key] for key in ('focus', 'obligations', 'avoid')} |
                                        {'role': case['spec']['roles'][role_id]}})


def validate_materials(data):
    require(data['version'] == 'cr1-development-material-v1', 'cr1_material_version')
    require(len(data['cases']) == 24, 'cr1_scene_count')
    ids = [case['id'] for case in data['cases']]
    require(ids == [f'D{i:02d}' for i in range(1, 25)], 'cr1_scene_ids')
    require(set(data['evaluation']) == set(ids), 'cr1_evaluation_coverage')
    counts = Counter(case['category'] for case in data['cases'])
    require(counts == Counter({name: 3 for name in data['categories']}), 'cr1_category_coverage')
    require(len(data['categories']) == 8 and len(counts) == 8, 'cr1_category_count')
    subset = data['real_subset']
    require(len(subset) == len(set(subset)) == 12 and set(subset) <= set(ids), 'cr1_real_subset')
    require(set(case['category'] for case in data['cases'] if case['id'] in subset) == set(counts),
            'cr1_real_subset_category_coverage')
    require(tuple(data['roles']) == ('weigh', 'lean'), 'cr1_roles')
    first = data['roles']['weigh']
    for role_id, role in data['roles'].items():
        require(set(role) == set(COMMON_FIELDS) | {'disposition', 'examples'}, 'cr1_role_schema')
        require(set(role['disposition']) == {'attention', 'tradeoff'}, 'cr1_disposition_schema')
        require(all(role[key] == first[key] for key in COMMON_FIELDS), 'cr1_common_role_content')
        examples = role['examples']
        require(len(examples) == 6 and len({e['id'] for e in examples}) == 6, 'cr1_examples')
        for example in examples:
            require(set(example) == {'id', 'scenario', 'keywords', 'text'}, 'cr1_example_schema')
            require(0 < len(example['text']) <= 500, 'cr1_example_length')
    require(first['disposition'] != data['roles']['lean']['disposition'], 'cr1_role_difference')
    require([e['text'] for e in first['examples']] != [e['text'] for e in data['roles']['lean']['examples']],
            'cr1_role_difference')
    rows = []
    for case in data['cases']:
        require(set(case) == {'id', 'category', 'title', 'history', 'members', 'reference', 'frame', 'spec'},
                'cr1_scene_schema')
        require(set(case['spec']) == {'focus', 'obligations', 'avoid', 'roles'}, 'cr1_spec_schema')
        require(all(set(role) == {'stance', 'optional_moves'} for role in case['spec']['roles'].values()),
                'cr1_spec_role_schema')
        require(bool(case['members']) and all(type(m) is str and m for m in case['members']), 'cr1_members')
        require(set(case['frame']) <= set(DialogueFrame.fields()), 'cr1_frame_field')
        require(set(case['spec']['roles']) == set(data['roles']), 'cr1_spec_roles')
        evaluation = data['evaluation'][case['id']]
        require(evaluation['control'] in ('failure_opportunity', 'success_control'), 'cr1_control')
        require(bool(evaluation['must']), 'cr1_required_content')
        for role_id, role in data['roles'].items():
            evidence, aliases = material_evidence(case, role_id, role)
            frame, spec, audit = bind_annotations(case, role_id, evidence, aliases)
            rows.append({'id': f'{case["id"]}-{role_id}', 'scene_id': case['id'], 'role_id': role_id,
                         'material_hash': fingerprint(generation_materials(data, case['id'], role_id)),
                         'evaluation_hash': fingerprint(evaluation),
                         'evidence': evidence.document(), 'evidence_hash': evidence.digest,
                         'frame': frame.document(), 'spec': spec.document(), 'annotation_audit': audit})
    for category in data['categories']:
        controls = {data['evaluation'][c['id']]['control'] for c in data['cases'] if c['category'] == category}
        require(controls == {'failure_opportunity', 'success_control'}, 'cr1_category_controls')
    return rows


def check_materials(data=None):
    data = fixture() if data is None else data
    rows = validate_materials(data)
    return {
        'tool': VERSION, 'status': 'material_contract_checks_passed',
        'scope': 'artificial authoring and source binding; no runtime admission or model dispatch',
        'scene_count': len(data['cases']), 'roles': list(data['roles']),
        'category_counts': dict(Counter(case['category'] for case in data['cases'])),
        'real_subset': list(data['real_subset']), 'annotation_pairs': len(rows),
        'normal_positions_confirmed': {'shared_selection': 24, 'main': 144},
        'numeric_protocol': 'pending_user_choice', 'anonymous_review_count': 'pending_user_choice',
        'semantic_review': 'machine review of authored inputs; not independent human review',
        'runtime_admission': 'not_performed', 'candidate_recall': 'not_performed',
        'selection': 'not_performed', 'main_request_simulation': 'not_performed',
        'true_expression_effect': 'unverified', 'real_calls': 0, 'qq_sends': 0,
        'fixture_hash': fingerprint(data), 'bindings': rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check-materials', 'prepare', 'simulate', 'report', 'verify'])
    parser.add_argument('--root', type=Path, default=OUT / 'offline-v3')
    args = parser.parse_args(argv)
    from scripts.dialogue_refactor import offline
    with offline() as audit:
        if args.action == 'check-materials':
            data = fixture()
            result = check_materials(data)
            folder = safe(OUT / 'material-check-v1', OUT)
            folder.mkdir(parents=True, exist_ok=True)
            atomic_json(folder / 'materials.json', {k: v for k, v in data.items() if k != 'evaluation'})
            atomic_json(folder / 'evaluation-only.json', data['evaluation'])
            atomic_json(folder / 'bindings.json', result.pop('bindings'))
            result['network_audit'] = audit.copy()
            result['files_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(folder.glob('*.json')) if p.name != 'result.json'}
            atomic_json(folder / 'result.json', result)
        else:
            import asyncio
            from scripts import dialogue_diagnosis_runtime as runtime
            root = runtime.confined(args.root)
            created_for_action = not root.exists()
            try:
                if args.action == 'prepare':
                    result = asyncio.run(runtime.prepare(root))
                elif args.action == 'simulate':
                    result = asyncio.run(runtime.simulate(root))
                else:
                    result = runtime.verify_simulation(root)
            except BaseException as error:
                if args.action == 'prepare' and created_for_action and root.exists():
                    runtime.write_once(root / 'prepare-failure.json',
                                       {'status': 'failed', 'error_type': type(error).__name__,
                                        'network_audit': audit.copy(), 'real_calls': 0})
                raise
            result['network_audit'] = audit.copy()
            atomic_json(root / (args.action + '-network-audit.json'), result)
    print(encoded(result))


if __name__ == '__main__':
    main()
