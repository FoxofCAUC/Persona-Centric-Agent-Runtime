"""Read-only provenance check for the fixed-decision permission and isolated D3 workbench."""
import argparse
import json
from pathlib import Path

from scripts.pcar_en3_repair_preflight import child, extend, read
from scripts.pcar_expression_preflight import check_core, source_digest
from scripts.pcar_style_factors_preflight import EXPERIENCE, MANIFEST as STYLE, parent_files


MANIFEST = 'docs/expression/E.N5-接话决策与隔离桌面候选清单.json'


def previous(repo):
    commit, core, evidence=parent_files(repo)
    parent=child(repo,STYLE,EXPERIENCE,'en5-style-factors-v1')
    if (len(core),len(evidence))!=(parent['baseline_core_files'],parent['baseline_evidence_files']):
        raise ValueError('d3_parent_count')
    extend(core,parent['declared_core_changes'],parent['new_core_files'])
    extend(evidence,parent['declared_evidence_changes'],parent['new_research_tools'])
    return commit,core,evidence


def inspect(repo):
    repo=repo.resolve()
    commit,core,evidence=previous(repo)
    ledger=child(repo,MANIFEST,STYLE,'en5-decision-desktop-engineering-v1')
    if (len(core),len(evidence))!=(ledger['baseline_core_files'],ledger['baseline_evidence_files']):
        raise ValueError('d3_candidate_count')
    extend(core,ledger['declared_core_changes'],ledger['new_core_files'])
    extend(evidence,ledger['declared_evidence_changes'],ledger['new_research_tools'])
    if core.keys() & evidence.keys(): raise ValueError('d3_registry_overlap')
    check=check_core(repo,{'commit':commit,'files':[{'path':p,'sha256':s} for p,s in core.items()],
                           'allowed_new_core_files_e0':[]})
    errors=[p for p,s in evidence.items() if not (repo/p).is_file() or source_digest(read(repo,p))!=s]
    return {'passed':check['passed'] and not errors,'stage':'E.N5-D2/D3-engineering','core':check,
            'evidence_files':len(evidence),'evidence_errors':errors,'model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',required=True,type=Path)
    out=inspect(parser.parse_args().repo)
    print(json.dumps(out,ensure_ascii=False))
    raise SystemExit(0 if out['passed'] else 1)
