"""Read-only provenance check for the strict Flash decision repair."""
import argparse
import json
from pathlib import Path

from scripts.pcar_en3_repair_preflight import child, extend, read
from scripts.pcar_en5_decision_desktop_preflight import MANIFEST as DESKTOP, STYLE, previous
from scripts.pcar_expression_preflight import check_core, source_digest

MANIFEST = 'docs/expression/E.N5-D2决策协议修复候选清单.json'


def parent_files(repo):
    commit, core, evidence = previous(repo)
    base = child(repo, DESKTOP, STYLE, 'en5-decision-desktop-engineering-v1')
    if (len(core),len(evidence)) != (base['baseline_core_files'],base['baseline_evidence_files']):
        raise ValueError('decision_repair_parent_count')
    extend(core,base['declared_core_changes'],base['new_core_files'])
    extend(evidence,base['declared_evidence_changes'],base['new_research_tools'])
    return commit,core,evidence


def inspect(repo):
    repo = repo.resolve()
    commit,core,evidence = parent_files(repo)
    ledger = child(repo, MANIFEST, DESKTOP, 'en5-d2-strict-decision-contract-v1')
    if (len(core),len(evidence)) != (ledger['baseline_core_files'],ledger['baseline_evidence_files']):
        raise ValueError('decision_repair_count')
    extend(core,ledger['declared_core_changes'],ledger['new_core_files'])
    extend(evidence,ledger['declared_evidence_changes'],ledger['new_research_tools'])
    if core.keys() & evidence.keys():
        raise ValueError('decision_repair_registry_overlap')
    checked = check_core(repo,{'commit':commit,'files':[{'path':p,'sha256':v} for p,v in core.items()],
                               'allowed_new_core_files_e0':[]})
    errors = [p for p,v in evidence.items() if not (repo/p).is_file() or source_digest(read(repo,p))!=v]
    return {'passed':checked['passed'] and not errors,'stage':'E.N5-D2-strict-tool','core':checked,
            'evidence_files':len(evidence),'evidence_errors':errors,'model_calls':0}


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--repo',type=Path,required=True)
    report=inspect(cli.parse_args().repo)
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
