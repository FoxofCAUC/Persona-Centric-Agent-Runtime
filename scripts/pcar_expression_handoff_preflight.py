"""只读核对表达候选与已批准README发布保护的整合边界。"""
import argparse
from contextlib import closing
import hashlib,json,sqlite3
from pathlib import Path
from scripts.pcar_expression_utterance_preflight import baseline
from scripts.pcar_expression_preflight import check_core


def accepted_files(repo):
    e0,files,_=baseline(repo)
    folder=repo/'docs/expression'
    for filename,parent in (
        ('E3.R3-分段兼容扩展清单.json','E3.R2-统一出口扩展清单.json'),
        ('E阶段归档-README发布保护整合清单.json','E3.R3-分段兼容扩展清单.json')):
        delta=json.loads((folder/filename).read_text(encoding='utf-8'))
        if hashlib.sha256((folder/parent).read_bytes()).hexdigest()!=delta['prior_manifest_sha256']:
            raise ValueError('handoff_historical_manifest_changed')
        for item in delta['declared_changes']:
            if files[item['path']]['sha256']!=item['baseline_sha256']:raise ValueError('handoff_baseline_binding_invalid')
            files[item['path']]['sha256']=item['accepted_sha256']
        for item in delta['new_core_files']:
            if item['path'] in files:raise ValueError('handoff_extension_duplicate')
            files[item['path']]={'path':item['path'],'sha256':item['accepted_sha256']}
    return e0,files


def inspect(repo,home=None):
    e0,files=accepted_files(repo)
    core=check_core(repo,{'commit':e0['commit'],'files':list(files.values()),'allowed_new_core_files_e0':[]})
    result={'version':'expression-readme-handoff-preflight-v1','passed':core['passed'],'core':core,
            'candidate_schema':32,'next_planned_schema':33,'new_route_implemented':False,'model_calls':0,'instance_modified':False}
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
    print(json.dumps(result,ensure_ascii=False));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
