"""只读核对结构32扩展，保留E0、E2、E3及R1的历史清单。"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from scripts.pcar_expression_preflight import check_core,source_digest


def baseline(repo,*,include_r2=True):
    directory=repo/'docs/expression'
    def read(name):return json.loads((directory/name).read_text(encoding='utf-8'))
    e0=read('E0-core-baseline.json');e2=read('E2-核心扩展清单.json');e3=read('E3-核心扩展清单.json')
    punctuation=read('E3.R1-标点职责迁移清单.json');continuation=read('E3.R1续-核心容错扩展清单.json')
    links={'E0-core-baseline.json':e3['e0_manifest_sha256'],'E2-核心扩展清单.json':e3['e2_manifest_sha256'],
       'E3-核心扩展清单.json':punctuation['e3_manifest_sha256'],
       'E3.R1-标点职责迁移清单.json':continuation['punctuation_manifest_sha256']}
    declarations=[e2,e3,punctuation,continuation]
    if include_r2:
        r2=read('E3.R2-统一出口扩展清单.json');declarations.append(r2)
        links['E3.R1续-核心容错扩展清单.json']=r2['prior_manifest_sha256']
    for name,digest in links.items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:raise ValueError('speech_historical_manifest_changed')
    files={i['path']:dict(i) for i in e0['files']}
    for delta in declarations:
        for item in delta['declared_changes']:
            name=item['path']
            if name not in files or files[name]['sha256']!=item['baseline_sha256']:raise ValueError('speech_baseline_binding_invalid')
            files[name]['sha256']=item['accepted_sha256']
        for item in delta.get('E0_extensions',[])+delta.get('new_core_files',[]):
            if item['path'] in files:raise ValueError('speech_duplicate_core_extension')
            files[item['path']]={'path':item['path'],'sha256':item['accepted_sha256']}
    return e0,files,declarations[-1]


def inspect(repo,home=None):
    e0,files,delta=baseline(repo)
    core=check_core(repo,{'commit':e0['commit'],'files':list(files.values()),'allowed_new_core_files_e0':[]})
    for item in delta['extra_files']:
        path=(repo/item['path']).resolve()
        if not path.is_relative_to(repo.resolve()) or not path.is_file() or source_digest(path.read_bytes())!=item['accepted_sha256']:
            core['errors'].append({'code':'speech_extra_file_changed','path':item['path']})
    result={'version':'expression-speech-preflight-v1','passed':not core['errors'],'core':core,
            'declared_existing':len(delta['declared_changes']),'declared_new':len(delta['new_core_files']),
            'candidate_schema':32,'model_calls':0,'instance_modified':False}
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
        a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False));return 0 if value['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
