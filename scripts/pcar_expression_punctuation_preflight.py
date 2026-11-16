"""只读核对本次标点职责迁移，沿用E0→E2→E3历史封口。"""
import argparse
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3

from scripts.pcar_expression_preflight import check_core,source_digest


def inspect(repo,home=None):
    folder=repo/'docs/expression'
    load=lambda name:json.loads((folder/name).read_text(encoding='utf-8'))
    e0,e2,e3=load('E0-core-baseline.json'),load('E2-核心扩展清单.json'),load('E3-核心扩展清单.json')
    delta=load('E3.R1-标点职责迁移清单.json')
    hashes={'E0-core-baseline.json':e3['e0_manifest_sha256'],'E2-核心扩展清单.json':e3['e2_manifest_sha256'],
            'E3-核心扩展清单.json':delta['e3_manifest_sha256']}
    for name,value in hashes.items():
        if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=value:raise ValueError('punctuation_historical_manifest_changed')
    baseline={item['path']:dict(item) for item in e0['files']}
    for declaration in (e2,e3):
        for item in declaration['declared_changes']:
            if baseline[item['path']]['sha256']!=item['baseline_sha256']:raise ValueError('punctuation_baseline_binding_invalid')
            baseline[item['path']]['sha256']=item['accepted_sha256']
        for item in declaration.get('E0_extensions',[])+declaration['new_core_files']:
            if item['path'] in baseline:raise ValueError('punctuation_core_extension_duplicate')
            baseline[item['path']]={'path':item['path'],'sha256':item['accepted_sha256']}
    for item in delta['declared_changes']:
        if baseline[item['path']]['sha256']!=item['baseline_sha256']:raise ValueError('punctuation_delta_binding_invalid')
        baseline[item['path']]['sha256']=item['accepted_sha256']
    additions=delta['new_core_files']
    core=check_core(repo,{'commit':e0['commit'],'files':list(baseline.values()),'allowed_new_core_files_e0':[item['path'] for item in additions]})
    for item in additions+delta['research_files']:
        path=(repo/item['path']).resolve()
        if not path.is_relative_to(repo.resolve()) or not path.is_file() or source_digest(path.read_bytes())!=item['accepted_sha256']:
            core['errors'].append({'code':'punctuation_extension_changed','path':item['path']})
    core['passed']=not core['errors']
    result={'version':'expression-punctuation-preflight-v1','passed':core['passed'],'core':core,
        'E3_baseline_core_files':len(baseline),'changed_existing_files':len(delta['declared_changes']),
        'new_core_files':len(additions),'model_calls':0,'instance_modified':False}
    if home is not None:
        path=(home/'data/database/runtime.sqlite3').resolve()
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                result['instance']={'schema':db.execute('PRAGMA user_version').fetchone()[0],'read_only':True}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--home',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();repo=args.repo.resolve();result=inspect(repo,args.home.resolve() if args.home else None)
    if args.output:
        if args.output.resolve().is_relative_to(repo):raise ValueError('preflight_output_must_be_outside_source')
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
