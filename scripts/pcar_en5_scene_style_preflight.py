"""Verify the presentation repair against the continuous Jev study."""
import argparse
import json
from pathlib import Path

from scripts.pcar_en3_repair_preflight import child, read
from scripts.pcar_en5_scene_continuous_preflight import MANIFEST as CONTINUOUS, TIMING, apply, timing_files
from scripts.pcar_expression_preflight import check_core, source_digest

MANIFEST='docs/expression/E.N5-Jev表达修复候选清单.json'


def prior(repo):
    commit,core,evidence=timing_files(repo)
    apply(child(repo,CONTINUOUS,TIMING,'en5-jev-continuous-v1'),core,evidence)
    return commit,core,evidence


def inspect(repo):
    repo=repo.resolve()
    commit,core,evidence=prior(repo)
    apply(child(repo,MANIFEST,CONTINUOUS,'en5-jev-continuous-style-repair-v1'),core,evidence)
    if core.keys() & evidence.keys():
        raise ValueError('scene_style_source_overlap')
    checked=check_core(repo,{'commit':commit,'files':[{'path':k,'sha256':v} for k,v in core.items()],
                             'allowed_new_core_files_e0':[]})
    errors=[k for k,v in evidence.items() if not (repo/k).is_file() or source_digest(read(repo,k))!=v]
    return {'passed':checked['passed'] and not errors,'core':checked,'research_files':len(evidence),
            'research_errors':errors,'model_calls':0}


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--repo',type=Path,required=True)
    result=inspect(cli.parse_args().repo)
    print(json.dumps(result,ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
