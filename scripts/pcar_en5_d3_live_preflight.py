"""Check the continuous desktop study against the previously frozen strict D2 candidate."""
import argparse
import json
from pathlib import Path

from scripts.pcar_en3_repair_preflight import child, extend, read
from scripts.pcar_en5_decision_repair_preflight import DESKTOP, MANIFEST as D2, parent_files
from scripts.pcar_expression_preflight import check_core, source_digest

MANIFEST = 'docs/expression/E.N5-D3连续对照候选清单.json'


def apply(ledger, core, evidence):
    if (len(core), len(evidence)) != (ledger['baseline_core_files'], ledger['baseline_evidence_files']):
        raise ValueError('d3_live_parent_counts')
    extend(core, ledger['declared_core_changes'], ledger['new_core_files'])
    extend(evidence, ledger['declared_evidence_changes'], ledger['new_research_tools'])


def previous(repo):
    commit, core, evidence = parent_files(repo)
    apply(child(repo, D2, DESKTOP, 'en5-d2-strict-decision-contract-v1'), core, evidence)
    return commit, core, evidence


def inspect(repo):
    repo = repo.resolve()
    commit, core, evidence = previous(repo)
    apply(child(repo, MANIFEST, D2, 'en5-d3-continuous-desktop-v1'), core, evidence)
    if core.keys() & evidence.keys():
        raise ValueError('d3_live_overlapping_registries')
    checked = check_core(repo, {'commit':commit, 'files':[{'path':k,'sha256':v} for k,v in core.items()],
                                'allowed_new_core_files_e0':[]})
    errors = [k for k,v in evidence.items() if not (repo/k).is_file() or source_digest(read(repo,k)) != v]
    return {'passed':checked['passed'] and not errors, 'core':checked, 'evidence_files':len(evidence),
            'evidence_errors':errors, 'model_calls':0}


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--repo', type=Path, required=True)
    result = inspect(cli.parse_args().repo)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
