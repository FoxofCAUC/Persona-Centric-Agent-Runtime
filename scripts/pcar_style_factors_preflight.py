"""Trace the two E.N5 study factors back to the accepted desktop build."""
import argparse
import json
from pathlib import Path
from scripts.pcar_en3_repair_preflight import child, extend, read
from scripts.pcar_en5_preflight import DESKTOP, MANIFEST as EXPERIENCE, desktop_files
from scripts.pcar_expression_preflight import check_core, source_digest


MANIFEST='docs/expression/E.N5-接话修复候选清单.json'


def parent_files(repo):
    commit,core,evidence=desktop_files(repo)
    previous=child(repo,EXPERIENCE,DESKTOP,'en5-v8-pro-experience-v1')
    if (len(core),len(evidence))!=(previous['baseline_core_files'],previous['baseline_evidence_files']):
        raise ValueError('style_parent_count')
    extend(core,previous['declared_core_changes'],previous['new_core_files'])
    extend(evidence,previous['declared_evidence_changes'],previous['new_research_tools'])
    return commit,core,evidence


def inspect(repo):
    repo=repo.resolve()
    commit,core,evidence=parent_files(repo)
    ledger=child(repo,MANIFEST,EXPERIENCE,'en5-style-factors-v1')
    if (len(core),len(evidence))!=(ledger['baseline_core_files'],ledger['baseline_evidence_files']):
        raise ValueError('style_candidate_count')
    extend(core,ledger['declared_core_changes'],ledger['new_core_files'])
    extend(evidence,ledger['declared_evidence_changes'],ledger['new_research_tools'])
    if core.keys() & evidence.keys():raise ValueError('style_registry_overlap')
    checked=check_core(repo,{'commit':commit,'files':[{'path':k,'sha256':v} for k,v in core.items()],
                             'allowed_new_core_files_e0':[]})
    errors=[p for p,v in evidence.items() if not (repo/p).is_file() or source_digest(read(repo,p))!=v]
    return {'passed':checked['passed'] and not errors,'core':checked,'evidence_files':len(evidence),
            'evidence_errors':errors,'stage':'E.N5-style','model_calls':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',type=Path,required=True)
    args=parser.parse_args()
    try:result=inspect(args.repo)
    except (ValueError,TypeError,KeyError,OSError) as error:result={'passed':False,'error':str(error)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
