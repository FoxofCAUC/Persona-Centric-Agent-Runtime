"""只读核对首批学习源码扩展；预检本身不读取凭据或调用模型。"""
import argparse
from contextlib import closing
import hashlib,json,sqlite3
from pathlib import Path

from scripts.pcar_expression_handoff_preflight import accepted_files
from scripts.pcar_expression_preflight import check_core,source_digest
from scripts.pcar_corpus_dataset_preflight import CHAIN as PRIOR_CHAIN,PARENT


CHAIN=PRIOR_CHAIN+(('docs/expression/E.L3-首批学习扩展清单.json','corpus-learning-first-extension-v1'),)


def inspect(repo,home=None):
    repo=repo.resolve();baseline,files=accepted_files(repo);parent=PARENT;tests=[];prior_count=len(files)
    for filename,version in CHAIN:
        ledger=json.loads((repo/filename).read_text(encoding='utf-8'))
        if (ledger['version']!=version or ledger['parent_manifest']!=parent or ledger['declared_changes']
                or ledger['parent_manifest_sha256']!=hashlib.sha256((repo/parent).read_bytes()).hexdigest()
                or ledger.get('baseline_core_files',len(files))!=len(files)):
            raise ValueError('corpus_learning_baseline_invalid')
        prior_count=len(files)
        for item in ledger['new_core_files']:
            if item['path'] in files:raise ValueError('corpus_learning_duplicate_extension')
            files[item['path']]={'path':item['path'],'sha256':item['sha256']}
        tests.extend(ledger['test_files']);parent=filename
    core=check_core(repo,{'commit':baseline['commit'],'files':list(files.values()),'allowed_new_core_files_e0':[]})
    errors=[]
    for item in tests:
        path=(repo/item['path']).resolve()
        if not path.is_relative_to(repo) or not path.is_file() or source_digest(path.read_bytes())!=item['sha256']:
            errors.append({'code':'corpus_learning_test_file_changed','path':item['path']})
    result={'version':'corpus-learning-preflight-v1','scope':'read_only_preflight',
            'passed':core['passed'] and not errors,'core':core,'unchanged_prior_files':prior_count,
            'new_learning_files':len(files)-prior_count,'test_errors':errors,
            'corpus_schema':3,'learning_ledger_schema':1,'candidate_schema':32,'model_calls_this_check':0,'instance_modified':False}
    if home is not None:
        path=(home/'data/database/runtime.sqlite3').resolve()
        if path.is_file():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                result['instance']={'schema':db.execute('PRAGMA user_version').fetchone()[0],'read_only':True}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--home',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv);repo=args.repo.resolve()
    if args.output and args.output.resolve().is_relative_to(repo):parser.error('output_must_be_outside_source')
    try:result=inspect(repo,args.home.resolve() if args.home else None)
    except (OSError,ValueError,KeyError,TypeError):result={'version':'corpus-learning-preflight-v1','passed':False,'error':'corpus_learning_preflight_invalid','model_calls_this_check':0}
    payload=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(payload,encoding='utf-8')
    print(payload,end='');return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
