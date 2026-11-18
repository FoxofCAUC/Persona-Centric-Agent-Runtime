"""Verify the expanded E.N3 candidate against rc8 and the first holdout's frozen sources."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from scripts.pcar_dialogue_generation_preflight import (LAST_RESEARCH, MANIFEST as RC8,
                                                        previous_files, read)
from scripts.pcar_expression_preflight import check_core, source_digest


PARENT = 'docs/expression/E.N3-单角色研究工具清单.json'
MANIFEST = 'docs/expression/E.N3-修复扩测工具清单.json'


def child(repo, name, parent, version):
    ledger = json.loads(read(repo, name))
    if ledger['version'] != version or ledger['parent_manifest'] != parent or (
            sha256(read(repo, parent)).hexdigest() != ledger['parent_manifest_sha256']):
        raise ValueError('repair_preflight_manifest_chain_changed')
    return ledger


def extend(files, changes, additions):
    """Apply recorded old -> new digests; existing entries can only change once per version."""
    touched = set()
    for item in changes:
        path = item['path']
        if path in touched or path not in files or files[path] != item['baseline_sha256'] or (
                item['sha256'] == files[path]):
            raise ValueError('repair_preflight_wrong_predecessor:' + path)
        touched.add(path)
        files[path] = item['sha256']
    for item in additions:
        path = item['path']
        if path in files:
            raise ValueError('repair_preflight_existing_path:' + path)
        files[path] = item['sha256']


def previous_candidate(repo):
    baseline, old_core, evidence = previous_files(repo)
    core = {name:entry['sha256'] for name,entry in old_core.items()}
    rc8 = child(repo, RC8, LAST_RESEARCH, 'dialogue-generation-rc8-v1')
    if rc8['baseline_core_files'] != len(core):
        raise ValueError('repair_preflight_rc8_parent_count')
    extend(core, rc8['declared_changes'], rc8['new_core_files'])
    extend(evidence, [], rc8['test_files'])
    holdout = child(repo, PARENT, RC8, 'en3-single-persona-tools-v1')
    if holdout['production_core_files'] != len(core):
        raise ValueError('repair_preflight_holdout_parent_count')
    legacy_evidence_count = len(evidence)
    extend(evidence, [], holdout['new_research_tools'])
    return baseline['commit'], core, evidence, legacy_evidence_count


def inspect(repo):
    repo = repo.resolve()
    commit, core, evidence, legacy_count = previous_candidate(repo)
    old_core_count, old_evidence_count = len(core), len(evidence)
    ledger = child(repo, MANIFEST, PARENT, 'en3-repair-expanded-tools-v1')
    if (ledger['baseline_core_files'], ledger['baseline_evidence_files']) != (old_core_count, old_evidence_count):
        raise ValueError('repair_preflight_baseline_count')
    extend(core, ledger['declared_core_changes'], ledger['new_core_files'])
    extend(evidence, ledger['declared_evidence_changes'], ledger['new_research_tools'])
    if core.keys() & evidence.keys():
        raise ValueError('repair_preflight_double_registration')
    checked = check_core(repo, {'commit':commit, 'files':[{'path':p,'sha256':v} for p,v in core.items()],
                                'allowed_new_core_files_e0':[]})
    errors = [p for p,v in evidence.items() if not (repo/p).is_file() or source_digest(read(repo,p)) != v]
    return {'passed':checked['passed'] and not errors, 'version':ledger['version'], 'schema':33,
            'previous_core_files':old_core_count, 'previous_evidence_files':legacy_count,
            'first_holdout_tools': old_evidence_count - legacy_count,
            'declared_core_changes':len(ledger['declared_core_changes']), 'new_core_files':len(ledger['new_core_files']),
            'declared_evidence_changes':len(ledger['declared_evidence_changes']), 'new_research_tools':len(ledger['new_research_tools']),
            'core':checked, 'evidence_files':len(evidence), 'evidence_errors':errors,
            'model_calls':0, 'instance_modified':False}


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--repo', type=Path, required=True)
    cli.add_argument('--output', type=Path)
    args=cli.parse_args()
    if args.output and args.output.resolve().is_relative_to(args.repo.resolve()):
        cli.error('output_must_be_outside_source_repository')
    try:
        report=inspect(args.repo)
    except (OSError, TypeError, KeyError, ValueError) as error:
        report={'passed':False, 'error':str(error), 'model_calls':0, 'instance_modified':False}
    payload=json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload+'\n', encoding='utf-8')
    print(payload)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
