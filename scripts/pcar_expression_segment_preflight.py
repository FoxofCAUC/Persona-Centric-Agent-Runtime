"""R3分段运输兼容扩展；历史正文与已发布计划保持冻结。"""
import argparse
from contextlib import closing
import hashlib,json,sqlite3
from pathlib import Path
from scripts.pcar_expression_utterance_preflight import baseline
from scripts.pcar_expression_preflight import check_core,source_digest


def inspect(repo,home=None):
    e0,files,_=baseline(repo)
    directory=repo/'docs/expression';path=directory/'E3.R3-分段兼容扩展清单.json';delta=json.loads(path.read_text(encoding='utf-8'))
    if hashlib.sha256((directory/'E3.R2-统一出口扩展清单.json').read_bytes()).hexdigest()!=delta['prior_manifest_sha256']:raise ValueError('segment_historical_manifest_changed')
    for item in delta['declared_changes']:
        if files[item['path']]['sha256']!=item['baseline_sha256']:raise ValueError('segment_baseline_binding_invalid')
        files[item['path']]['sha256']=item['accepted_sha256']
    for item in delta['new_core_files']:
        if item['path'] in files:raise ValueError('segment_extension_duplicate')
        files[item['path']]={'path':item['path'],'sha256':item['accepted_sha256']}
    core=check_core(repo,{'commit':e0['commit'],'files':list(files.values()),'allowed_new_core_files_e0':[]})
    result={'version':'expression-segment-transport-preflight-v1','passed':core['passed'],'core':core,'candidate_schema':32,
            'model_calls':0,'instance_modified':False,'old_published_plans_rewritten':False}
    if home is not None:
        path=(home/'data/database/runtime.sqlite3').resolve()
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                result['instance']={'schema':db.execute('PRAGMA user_version').fetchone()[0],'read_only':True}
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo',type=Path,required=True);p.add_argument('--home',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args();repo=a.repo.resolve();value=inspect(repo,a.home.resolve() if a.home else None)
    if a.output:
        if a.output.resolve().is_relative_to(repo):raise ValueError('preflight_output_must_be_outside_source')
        a.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False));return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
