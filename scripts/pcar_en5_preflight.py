"""Verify E.N5 instrumentation against the accepted v8 Pro desktop build."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from scripts.pcar_en4_preflight import DECISION, MANIFEST as DESKTOP, REPAIR, parent_files
from scripts.pcar_en3_repair_preflight import child, extend, read
from scripts.pcar_expression_preflight import check_core, source_digest


MANIFEST = 'docs/expression/E.N5-连续体验候选清单.json'


def desktop_files(repo):
    commit, core, evidence = parent_files(repo)
    previous = child(repo, DESKTOP, REPAIR, 'en4-v8-pro-desktop-v1')
    if (len(core),len(evidence)) != (previous['baseline_core_files'],previous['baseline_evidence_files']):
        raise ValueError('en5_desktop_parent_count')
    if sha256(read(repo,DECISION)).hexdigest()!=previous['decision_sha256']:
        raise ValueError('en5_style_decision_changed')
    extend(core,previous['declared_core_changes'],previous['new_core_files'])
    extend(evidence,previous['declared_evidence_changes'],previous['new_research_tools'])
    return commit,core,evidence


def inspect(repo):
    repo=repo.resolve()
    commit,core,evidence=desktop_files(repo)
    ledger=child(repo,MANIFEST,DESKTOP,'en5-v8-pro-experience-v1')
    if (len(core),len(evidence)) != (ledger['baseline_core_files'],ledger['baseline_evidence_files']):
        raise ValueError('en5_parent_count_changed')
    extend(core,ledger['declared_core_changes'],ledger['new_core_files'])
    extend(evidence,ledger['declared_evidence_changes'],ledger['new_research_tools'])
    if core.keys() & evidence.keys():
        raise ValueError('en5_conflicting_registration')
    checked=check_core(repo,{'commit':commit,'files':[{'path':k,'sha256':v} for k,v in core.items()],
                             'allowed_new_core_files_e0':[]})
    errors=[k for k,v in evidence.items() if not (repo/k).is_file() or source_digest(read(repo,k))!=v]
    return {'passed':checked['passed'] and not errors,'stage':'E.N5','core':checked,
            'evidence_files':len(evidence),'evidence_errors':errors,'model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=inspect(args.repo)
    except (OSError,ValueError,KeyError,TypeError) as error:
        result={'passed':False,'error':str(error)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
