"""Frozen R1 development calibration. Never edits live policy or default config."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.character import CharacterService, read_fact
from persona_runtime.character_context import select_context
from persona_runtime.character_embedding import CharacterEmbeddingProvider, digest, file_hash
from persona_runtime.character_retrieval import rank_candidates, assemble, load_config
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.models import generic_persona, identifier, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from character_evaluation import ranking_metrics, selection_check, choose_threshold, rejection_margins


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def evaluate(case, version, response, config):
    raw = response['by_kind']
    ranked, rejected = rank_candidates(version, case['members'], raw, config)
    selected = assemble(version, case['members'], ranked, 2)
    lexical_rank, _ = rank_candidates(version, case['members'], {}, config)
    legacy = select_context(version, '\n\n'.join(case['members']), members=case['members'])
    ok = len(selected['text']) <= 3000 and selected['budgets']['dynamic'] <= 1600 and selected['budgets']['examples'] <= 600
    contract = case['contract']
    if contract in ('known_fact', 'old_version', 'unknown_fact'):
        path = 'appearance.blood_type' if contract == 'unknown_fact' else 'appearance.height_cm'
        fact = read_fact(version, path)
        ok &= path in selected['fact_paths'] and (fact['status'] == 'unset' if contract == 'unknown_fact' else fact['value'] == 172)
    ids = selected['lore_ids'] + selected['example_ids']
    check = selection_check(case, ids, contract_ok=ok)
    kind = case['kind']
    metrics = {}
    for label, pool in [('raw', raw), ('filtered', ranked), ('lexical_candidates', lexical_rank)]:
        metrics[label] = ranking_metrics([r['item_id'] for r in pool.get(kind, [])], case['required_ids'])
    return dict(id=case['id'], character=case['character'], group=case['group'], kind=kind,
                selected=ids, lexical_final=legacy['lore_ids'] + legacy['example_ids'],
                rankings=ranked, raw_candidates=raw, rejected=rejected, removed=selected['removed'],
                budgets=selected['budgets'], **metrics, **check)


def run(evidence):
    root = Path(__file__).resolve().parents[1]
    fixture = root / 'tests/fixtures/m754r1_development.json'
    frozen = json.loads((evidence / 'development-freeze.json').read_text())
    assert file_hash(fixture) == frozen['sha256']
    corpus = json.loads(fixture.read_text(encoding='utf-8'))
    config = load_config()
    assert config == json.loads((evidence / 'original-config.json').read_text())
    paths = RuntimePaths(evidence / 'calibration-runtime')
    raw_path = evidence / 'raw-scores.json'
    if paths.home.exists() or raw_path.exists():
        raise RuntimeError('Evidence exists; inspect checkpoint rather than rerun')
    store = Store(paths); store.initialize(); service = RuntimeService(store); chars = CharacterService(store)
    home = RuntimePaths.from_environment()
    provider = CharacterEmbeddingProvider(home, home.home / 'models/character-embedding' / config['model_fingerprint'])
    index = CharacterSemanticIndex(store, provider)
    versions = {}; other = service.create_persona(generic_persona())['id']
    for key, data in corpus['characters'].items():
        pid = service.create_persona(generic_persona())['id']; service.use_persona(pid)
        v = chars.update(pid, data, chars.current(pid)['id'], identifier()); versions[key] = v
        changed = deepcopy(data); changed['appearance']['height_cm'] = 199
        chars.update(pid, changed, v['id'], identifier())
    raw = {}; authority_before = file_hash(store.path)
    with patch('socket.socket.connect', side_effect=AssertionError('offline')), patch('socket.getaddrinfo', side_effect=AssertionError('offline')):
        for v in versions.values(): index.build(v['persona_id'], v['id'])
        for case in corpus['cases']:
            v = versions[case['character']]
            if case['contract'] == 'foreign_version':
                try: index.query(other, v['id'], case['members'], per_kind_limit=10)
                except RuntimeErrorBase as exc: raw[case['id']] = {'error': str(exc)}
                else: raise AssertionError('Cross-persona version accepted')
            else: raw[case['id']] = index.query(v['persona_id'], v['id'], case['members'], per_kind_limit=10)
            save(evidence / 'raw-partial.json', raw)
    assert file_hash(store.path) == authority_before
    save(raw_path, {'dataset_sha256': frozen['sha256'], 'config': config, 'versions': versions, 'results': raw})
    calibrate(evidence, corpus, frozen, config, versions, raw)


def calibrate(evidence, corpus, frozen, config, versions, raw, suffix=""):
    grids = {}; choices = {}
    for kind in ('lore', 'example'):
        trials = []
        for step in range(16, 37):
            threshold = step / 40; trial = deepcopy(config); trial['thresholds'] = {'lore': .9, 'example': .9}; trial['thresholds'][kind] = threshold
            hits = 0; violations = []; margins = []
            for case in corpus['cases']:
                if case['contract'] == 'foreign_version': continue
                v = versions[case['character']]; row = evaluate(case, v, raw[case['id']], trial)
                available = {r['id'] for r in v['data']['lorebook' if kind == 'lore' else 'examples']}
                actual = set(row['selected']) & available
                required = set(case['required_ids']) & available; allowed = set(case['allowed_ids']) & available
                if actual - allowed or (case['contract'] == 'dependency' and kind == 'lore' and not required <= actual): violations.append(case['id'])
                if case['group'] == 'positive' and case['kind'] == kind and required <= actual and not actual - allowed:
                    hits += 1
                    margins += [r['score'] - threshold for r in raw[case['id']]['by_kind'][kind] if r['item_id'] in required]
                ambiguous = {r['item_id'] for r in row['rejected'] if r['reason'] == 'ambiguous_alias'}
                # Budget/dependency removal is not a threshold rejection decision.
                rule_removed = {r['item_id'] for r in row['removed']}
                ambiguous |= rule_removed
                margins += rejection_margins(raw[case['id']]['by_kind'][kind], threshold, allowed, ambiguous)
            trials.append(dict(threshold=threshold, hits=hits, violations=len(violations), violation_cases=violations,
                               margin=min(margins) if margins else 0))
        grids[kind] = trials; choices[kind] = choose_threshold(trials)
    save(evidence / ('grid'+suffix+'.json'), {'grids': grids, 'choices': choices})
    candidate = deepcopy(config)
    rows = []
    if all(choices.values()):
        candidate.update(thresholds={k:r['threshold'] for k,r in choices.items()}, calibration_version='m754-r1-development-v1',
                         development_sha256=frozen['sha256'], status='candidate_not_joint_acceptance')
        for case in corpus['cases']:
            if case['contract'] == 'foreign_version':
                ok = raw[case['id']].get('error') == 'record_not_in_selected_persona'
                rows.append(dict(id=case['id'],character=case['character'],group='contract',kind=None,selected=[],passed=ok,hard_violation=not ok,degraded=False)); continue
            rows.append(evaluate(case, versions[case['character']], raw[case['id']], candidate))
    counts = {kind:sum(r['passed'] for r in rows if r['group']=='positive' and r['kind']==kind) for kind in ('lore','example')}
    passed = (len(rows)==64 and all(n>=14 for n in counts.values())
              and all(r['passed'] for r in rows if r['group']!='positive')
              and not any(r['hard_violation'] for r in rows))
    summary = {'passed':passed,'correct_positive':counts,'negative_passed':sum(r['passed'] for r in rows if r['group']=='negative'),
               'contract_passed':sum(r['passed'] for r in rows if r['group']=='contract'), 'choices':choices}
    save(evidence / ('development-report'+suffix+'.json'), {'summary':summary,'candidate_config':candidate if rows else None,'cases':rows,'paid_calls':0,'formal_changes':False})
    if passed: save(evidence/('candidate-config'+suffix+'.json'),candidate)
    checkpoint=json.loads((evidence/'checkpoint.json').read_text());checkpoint.update(R2='passed' if passed else 'blocked_development_gate', latest_report='development-report'+suffix+'.json')
    save(evidence/'checkpoint.json',checkpoint)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--rescore-margin',action='store_true')
    args=parser.parse_args()
    if args.rescore_margin:
        e=args.evidence
        if (e/'development-report-margin-fix.json').exists():raise RuntimeError('Rescore evidence already exists')
        fixture=Path(__file__).resolve().parents[1]/'tests/fixtures/m754r1_development.json'
        frozen=json.loads((e/'development-freeze.json').read_text())
        raw=json.loads((e/'raw-scores.json').read_text(encoding='utf-8'))
        assert file_hash(fixture)==frozen['sha256']==raw['dataset_sha256']
        assert raw['config']==load_config()
        calibrate(e,json.loads(fixture.read_text(encoding='utf-8')),frozen,raw['config'],raw['versions'],raw['results'],'-margin-fix')
    else:run(args.evidence)
