"""只读核对结构33源码、历史研究工具和定向测试。"""
import argparse
import hashlib
import json
from pathlib import Path

from scripts.pcar_corpus_learning_preflight import CHAIN, PARENT
from scripts.pcar_expression_handoff_preflight import accepted_files
from scripts.pcar_expression_preflight import check_core, source_digest


LAST_RESEARCH = 'docs/expression/E.L4-新场景扩测工具清单.json'
MANIFEST = 'docs/expression/E.L5-结构33候选扩展清单.json'


def read(repo, name):
    path = (repo / name).resolve()
    if not path.is_relative_to(repo):
        raise ValueError('preflight_path_outside_repository')
    return path.read_bytes()


def previous_files(repo):
    baseline, core = accepted_files(repo)
    evidence = {}
    parent = PARENT
    for name, version in CHAIN:
        ledger = json.loads(read(repo, name))
        if (ledger['version'] != version or ledger['parent_manifest'] != parent or ledger['declared_changes']
                or ledger['parent_manifest_sha256'] != hashlib.sha256(read(repo, parent)).hexdigest()
                or ledger.get('baseline_core_files', len(core)) != len(core)):
            raise ValueError('dialogue_prior_core_mismatch')
        for entry in ledger['new_core_files']:
            if entry['path'] in core:
                raise ValueError('duplicate_core_file')
            core[entry['path']] = entry.copy()
        evidence.update((entry['path'], entry['sha256']) for entry in ledger['test_files'])
        parent = name

    seen = set()
    name = LAST_RESEARCH
    while name != parent:
        if name in seen or not name.startswith('docs/expression/'):
            raise ValueError('dialogue_prior_research_mismatch')
        seen.add(name)
        ledger = json.loads(read(repo, name))
        for entry in ledger['new_research_tools']:
            evidence.setdefault(entry['path'], entry['sha256'])
        name = ledger['parent_manifest']
        if hashlib.sha256(read(repo, name)).hexdigest() != ledger['parent_manifest_sha256']:
            raise ValueError('dialogue_prior_research_mismatch')
    return baseline, core, evidence


def inspect(repo):
    repo = repo.resolve()
    baseline, core, evidence = previous_files(repo)
    delta = json.loads(read(repo, MANIFEST))
    previous_count = len(core)
    if (delta['version'] != 'dialogue-generation-rc8-v1' or delta['parent_manifest'] != LAST_RESEARCH
            or delta['parent_manifest_sha256'] != hashlib.sha256(read(repo, LAST_RESEARCH)).hexdigest()
            or delta['baseline_core_files'] != previous_count):
        raise ValueError('dialogue_manifest_parent_mismatch')
    for entry in delta['declared_changes']:
        prior = core[entry['path']]
        if prior['sha256'] != entry['baseline_sha256']:
            raise ValueError('dialogue_core_revision_mismatch')
        prior['sha256'] = entry['sha256']
    for entry in delta['new_core_files']:
        if entry['path'] in core:
            raise ValueError('duplicate_core_file')
        core[entry['path']] = entry
    for entry in delta['test_files']:
        if entry['path'] in evidence:
            raise ValueError('duplicate_evidence_file')
        evidence[entry['path']] = entry['sha256']
    checked = check_core(repo, {'commit':baseline['commit'], 'files':list(core.values()),
                                'allowed_new_core_files_e0':[]})
    evidence_errors = [name for name, digest in evidence.items() if source_digest(read(repo, name)) != digest]
    return {'passed':checked['passed'] and not evidence_errors, 'candidate':'0.2.0rc8', 'schema':33,
            'previous_core_files':previous_count, 'core':checked, 'evidence_files':len(evidence),
            'evidence_errors':evidence_errors, 'model_calls':0, 'instance_modified':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to(args.repo.resolve()):
        parser.error('output_must_be_outside_source_repository')
    try:
        result = inspect(args.repo)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = {'passed':False, 'error':str(exc), 'model_calls':0}
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + '\n', encoding='utf-8')
    print(payload)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
