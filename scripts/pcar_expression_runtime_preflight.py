"""只读核对E3声明扩展、E0/E2封口记录和实例结构；不升级实例或调用模型。"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from scripts.pcar_expression_preflight import check_core,source_digest


def inspect(repo,home=None):
    declaration=json.loads((repo/'docs/expression/E3-核心扩展清单.json').read_text(encoding='utf-8'))
    e0path=repo/'docs/expression/E0-core-baseline.json';e2path=repo/'docs/expression/E2-核心扩展清单.json'
    for path,key in ((e0path,'e0_manifest_sha256'),(e2path,'e2_manifest_sha256')):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=declaration[key]:raise ValueError('expression_historical_manifest_changed')
    e0=json.loads(e0path.read_text(encoding='utf-8'));e2=json.loads(e2path.read_text(encoding='utf-8'))
    baseline={item['path']:dict(item) for item in e0['files']}
    for item in e2['declared_changes']:
        if baseline[item['path']]['sha256']!=item['baseline_sha256']:raise ValueError('expression_E2_baseline_binding_invalid')
        baseline[item['path']]['sha256']=item['accepted_sha256']
    for item in e2['E0_extensions']+e2['new_core_files']:
        baseline[item['path']]={'path':item['path'],'sha256':item['accepted_sha256']}
    changed=set()
    for item in declaration['declared_changes']:
        name=item['path']
        if name in changed or name not in baseline or baseline[name]['sha256']!=item['baseline_sha256']:
            raise ValueError('expression_E3_baseline_binding_invalid')
        changed.add(name);baseline[name]['sha256']=item['accepted_sha256']
    additions=declaration['new_core_files']
    if len({i['path'] for i in additions})!=len(additions) or any(i['path'] in baseline for i in additions):
        raise ValueError('expression_E3_extension_duplicate')
    current={'commit':e0['commit'],'files':list(baseline.values()),'allowed_new_core_files_e0':[item['path'] for item in additions]}
    core=check_core(repo,current)
    for item in additions:
        path=(repo/item['path']).resolve()
        if not path.is_relative_to(repo.resolve()) or not path.is_file() or source_digest(path.read_bytes())!=item['accepted_sha256']:
            core['errors'].append({'code':'extension_file_changed','path':item['path']})
    core['passed']=not core['errors']
    result={'version':'expression-E3-preflight-v1','passed':core['passed'],'core':core,
            'E2_core_files':len(baseline),'E3_changed_existing_files':len(changed),'E3_new_core_files':len(additions),
            'model_calls':0,'instance_modified':False}
    if home is not None:
        database=home/'data/database/runtime.sqlite3'
        if database.is_file():
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
                result['instance']={'schema':db.execute('PRAGMA user_version').fetchone()[0],'read_only':True}
        else:result['instance']={'exists':False,'read_only':True}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--home',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();repo=args.repo.resolve();result=inspect(repo,args.home.resolve() if args.home else None)
    if args.output:
        if args.output.resolve().is_relative_to(repo):raise ValueError('preflight_output_must_be_outside_source')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
