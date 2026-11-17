"""只读核对E.L2数据视图追加边界；不读取聊天正文或配置密钥。"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from scripts.pcar_expression_handoff_preflight import accepted_files
from scripts.pcar_expression_preflight import check_core, source_digest


PARENT = 'docs/expression/E阶段归档-README发布保护整合清单.json'
CHAIN = (
    ('docs/expression/E.L0-L1-语料基础扩展清单.json','corpus-foundation-extension-v1'),
    ('docs/expression/E.L1-微信私聊适配扩展清单.json','corpus-moect-import-extension-v1'),
    ('docs/expression/E.L2-数据视图扩展清单.json','corpus-dataset-view-extension-v1'),
)


def inspect(repo, home=None):
    repo = repo.resolve()
    baseline, files = accepted_files(repo)
    parent, tests = PARENT, []
    previous_count = len(files)
    for filename, version in CHAIN:
        ledger = json.loads((repo/filename).read_text(encoding='utf-8'))
        if (ledger['version'] != version or ledger['parent_manifest'] != parent
                or ledger['parent_manifest_sha256'] != hashlib.sha256((repo/parent).read_bytes()).hexdigest()
                or ledger['declared_changes']
                or ledger.get('baseline_core_files',len(files)) != len(files)):
            raise ValueError('corpus_dataset_baseline_invalid')
        previous_count = len(files)
        for item in ledger['new_core_files']:
            if item['path'] in files:
                raise ValueError('corpus_dataset_duplicate_extension')
            files[item['path']] = {'path':item['path'],'sha256':item['sha256']}
        tests.extend(ledger['test_files'])
        parent = filename
    core = check_core(repo,{'commit':baseline['commit'],'files':list(files.values()),'allowed_new_core_files_e0':[]})
    errors=[]
    for item in tests:
        path=(repo/item['path']).resolve()
        if not path.is_relative_to(repo) or not path.is_file() or source_digest(path.read_bytes()) != item['sha256']:
            errors.append({'code':'corpus_dataset_test_file_changed','path':item['path']})
    result={'version':'corpus-dataset-preflight-v1','passed':core['passed'] and not errors,'core':core,
            'unchanged_prior_files':previous_count,'new_dataset_files':len(files)-previous_count,
            'test_errors':errors,'corpus_schema':3,'candidate_schema':32,'model_calls':0,
            'instance_modified':False,'model_input_approved':False}
    if home is not None:
        path=(home/'data/database/runtime.sqlite3').resolve()
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                result['instance']={'schema':db.execute('PRAGMA user_version').fetchone()[0],'read_only':True}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--home',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv);repo=args.repo.resolve()
    if args.output and args.output.resolve().is_relative_to(repo):
        parser.error('corpus_dataset_preflight_output_must_be_outside_source')
    try:result=inspect(repo,args.home.resolve() if args.home else None)
    except (OSError,ValueError,KeyError,TypeError):
        result={'version':'corpus-dataset-preflight-v1','passed':False,'error':'corpus_dataset_preflight_invalid','model_calls':0,'instance_modified':False}
    payload=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(payload,encoding='utf-8')
    print(payload,end='')
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
