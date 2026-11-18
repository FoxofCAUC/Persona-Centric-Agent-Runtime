"""Verify candidate files against the E.N3 parent and its effective style decision."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

from scripts.pcar_en3_repair_preflight import (
    MANIFEST as REPAIR, PARENT as HOLDOUT, child, extend, previous_candidate, read)
from scripts.pcar_expression_preflight import check_core, source_digest


MANIFEST='docs/expression/E.N4-桌面候选清单.json'
DECISION='docs/expression/E.N3-发言风格基线决议.json'


def parent_files(repo):
    commit,core,evidence,_=previous_candidate(repo)
    parent=child(repo,REPAIR,HOLDOUT,'en3-repair-expanded-tools-v1')
    if (len(core),len(evidence))!=(parent['baseline_core_files'],parent['baseline_evidence_files']):
        raise ValueError('en4_parent_size_changed')
    extend(core,parent['declared_core_changes'],parent['new_core_files'])
    extend(evidence,parent['declared_evidence_changes'],parent['new_research_tools'])
    return commit,core,evidence


def inspect(repo):
    from persona_runtime.config import DEFAULTS
    from persona_runtime.context_policy import new_persona_defaults
    from persona_runtime.dialogue_generation import DEFAULTS as direct
    from persona_runtime.expression_runtime import DEFAULTS as expression
    repo=repo.resolve()
    commit,core,evidence=parent_files(repo)
    ledger=child(repo,MANIFEST,REPAIR,'en4-v8-pro-desktop-v1')
    if (len(core),len(evidence))!=(ledger['baseline_core_files'],ledger['baseline_evidence_files']):
        raise ValueError('en4_baseline_size_changed')
    extend(core,ledger['declared_core_changes'],ledger['new_core_files'])
    extend(evidence,ledger['declared_evidence_changes'],ledger['new_research_tools'])
    if core.keys() & evidence.keys():
        raise ValueError('en4_duplicate_registration')
    decision_raw=read(repo,DECISION)
    decision=json.loads(decision_raw)
    policy=new_persona_defaults()
    if (sha256(decision_raw).hexdigest()!=ledger['decision_sha256']
            or decision['selected_branch']!='B' or DEFAULTS['model']['name']!=decision['wire_settings']['model']
            or policy['data']['companion']['version']!=decision['companion_profile']
            or policy['reply_mode']!=decision['reply_mode'] or expression['enabled'] or direct['enabled']):
        raise ValueError('en4_style_baseline_mismatch')
    checked=check_core(repo,{'commit':commit,'files':[{'path':k,'sha256':v} for k,v in core.items()],
                             'allowed_new_core_files_e0':[]})
    changed=[k for k,v in evidence.items() if not (repo/k).is_file() or source_digest(read(repo,k))!=v]
    return {'passed':checked['passed'] and not changed,'schema':ledger['schema'],'style':decision['branch_name'],
            'effective_feedback_sha256':decision['effective_author_export_sha256'],'core':checked,
            'evidence_files':len(evidence),'evidence_errors':changed,'model_calls':0}


if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('--repo',required=True,type=Path)
    args=cli.parse_args()
    try:
        result=inspect(args.repo)
    except (OSError,KeyError,TypeError,ValueError) as error:
        result={'passed':False,'error':str(error)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
