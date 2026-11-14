"""Read-only local migration evidence. No PCAR imports, network or model calls."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

SECRETS={'encrypted_key','api_key','admin_token','bridge_token','claim_token','console_token','session_token','access_token','refresh_token'}
CORE={'personas','settings','relationships','sessions','events','memories','summaries','candidates','memory_sources','candidate_reviews','source_exclusions'}
PREFIXES=('memory_','profile_','character_','context_','archive_','knowledge_')
CANON={'source-package.json','source-archive.md','initial-relationship.txt'}
STAGES={'conversation_web_decision','conversation_search_plan','conversation_search','conversation_fetch','conversation_organize'}


def read(path):return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
def quote(name):return '"'+name.replace('"','""')+'"'
def clean(value):
    if isinstance(value,dict):return {k:('' if k in SECRETS else clean(v)) for k,v in value.items()}
    if isinstance(value,list):return [clean(v) for v in value]
    return value


def normalize(column,value):
    if column in SECRETS:return ''
    if column=='sender_json':return {}
    if isinstance(value,bytes):return {'blob_sha256':hashlib.sha256(value).hexdigest()}
    if column.endswith(('_json','model_snapshot')) or column=='value':
        try:return clean(json.loads(value))
        except (ValueError,TypeError):pass
    return value


def table_evidence(db,table):
    columns=[r[1] for r in db.execute('PRAGMA table_info('+quote(table)+')')]
    order=[r[1] for r in sorted(db.execute('PRAGMA table_info('+quote(table)+')'),key=lambda r:r[5]) if r[5]]
    order=order or columns
    digest=hashlib.sha256();count=0
    for row in db.execute('SELECT * FROM '+quote(table)+' ORDER BY '+','.join(map(quote,order))):
        digest.update(fingerprint({c:normalize(c,v) for c,v in zip(columns,row)}).encode('ascii'));digest.update(b'\n');count+=1
    return {'count':count,'sha256':digest.hexdigest()}


def safe_file(path,root):
    for part in (path,*path.parents):
        if part.is_symlink() or part.is_junction():raise ValueError('不核对链接目录，请使用实例本身的本地目录。')
    if not path.resolve().is_relative_to(root.resolve()):raise ValueError('文件超出所选实例目录。')
    return path


def token_hash(value):return fingerprint(value) if isinstance(value,str) and value else None


def collect(home):
    home=Path(home)
    if not home.is_absolute() or not home.is_dir():raise ValueError('需要已存在实例的绝对目录。')
    safe_file(home,home)
    cfg=read(safe_file(home/'config/workbench.json',home))
    jev=read(safe_file(home/'private/knowledge-router/config.json',home))
    grant=read(safe_file(home/'private/personas/model-grants.json',home))
    marker=read(safe_file(home/'runtime/migration-state.json',home))
    exit_record=read(safe_file(home/'runtime/last-exit.json',home))
    console=read(safe_file(home/'runtime/console.json',home))
    result={'format':'pcar-acceptance-evidence-v1','read_only':True,'network_requests':0,
        'identity':{k:token_hash(cfg.get(k)) for k in ('instance_id','admin_token','bridge_token')},
        'configured_keys':{'main':bool(cfg.get('model',{}).get('encrypted_key')),
            'organization':bool(cfg.get('organization',{}).get('model',{}).get('encrypted_key')),
            'selector':bool(cfg.get('character_selector',{}).get('encrypted_key')),'jev':bool(jev.get('encrypted_key'))},
        'model_grant_count':len(grant.get('grants',{})),
        'migration':{k:marker.get(k) for k in ('state','resume_background')},
        'exit':{'processes_released':exit_record.get('processes_released'),'qq_was_running':exit_record.get('qq_was_running'),'console_state':console.get('state')},
        'canon_files':{},'database':{'exists':False},'business':{},'ledger':{},'retrieval_stages':{},
        'organized_notes':{'records':0,'with_notes':0}}
    private=home/'private/personas'
    if private.exists():
        files=[private/'registry.json']+[p for p in private.glob('*/*') if p.name in CANON]
        for path in sorted(files):
            if path.is_file():
                safe_file(path,home)
                with path.open('rb') as stream:result['canon_files'][path.relative_to(private).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
    file=home/'data/database/runtime.sqlite3'
    if not file.exists():return result
    safe_file(file,home)
    with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        result['database']={'exists':True,'schema_version':db.execute('PRAGMA user_version').fetchone()[0],
            'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],'foreign_key_violations':len(db.execute('PRAGMA foreign_key_check').fetchall())}
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(tables):
            if table in CORE or table.startswith(PREFIXES) and not table.endswith('_attempts'):
                result['business'][table]=table_evidence(db,table)
        for table in ('attempts','organization_attempts'):
            if table not in tables:continue
            known=unknown=total=0
            for raw, in db.execute('SELECT usage_json FROM '+quote(table)):
                try:usage=json.loads(raw or '{}')
                except ValueError:usage={}
                value=usage.get('total_tokens') if isinstance(usage,dict) else None
                if type(value) is int and value>=0:known+=1;total+=value
                else:unknown+=1
            result['ledger'][table]={'attempt_rows':known+unknown,'known_usage_rows':known,'unknown_usage_rows':unknown,'known_total_tokens':total}
        if 'organization_tasks' in tables:
            for ident,raw,status,snapshot in db.execute('SELECT id,payload_json,status,model_snapshot FROM organization_tasks'):
                payload=json.loads(raw);stage=payload.get('knowledge_processing',{}).get('stage')
                if stage not in STAGES:continue
                summary=result['retrieval_stages'].setdefault(stage,{'tasks':0,'attempt_rows':0,'received_rows':0,'routes':{},'reasoning_efforts':{},'jev_choices':{},'task_statuses':{},'output_token_limits':{}})
                summary['tasks']+=1
                summary['task_statuses'][status]=summary['task_statuses'].get(status,0)+1
                limit=json.loads(snapshot).get('max_tokens')
                if type(limit) is int:summary['output_token_limits'][str(limit)]=summary['output_token_limits'].get(str(limit),0)+1
                summary['attempt_rows']+=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(ident,)).fetchone()[0]
                if 'organization_receipts' not in tables:continue
                for response,raw_audit in db.execute('SELECT r.response_text,r.diagnostic_json FROM organization_receipts r JOIN organization_attempts a ON a.id=r.attempt_id WHERE a.task_id=?',(ident,)):
                    summary['received_rows']+=1;provider=json.loads(raw_audit).get('provider',{})
                    for field,destination,allowed in [('network_route','routes',{'system_proxy','loopback','local_fixture'}),('reasoning_effort','reasoning_efforts',{'high','medium','low'})]:
                        value=provider.get(field);label=value if value in allowed else 'not_recorded'
                        summary[destination][label]=summary[destination].get(label,0)+1
                    if stage=='conversation_web_decision':
                        try:choice=json.loads(response)['answers']['route']['choice']
                        except (ValueError,KeyError,TypeError):choice='not_recorded'
                        if choice not in ('web','none'):choice='not_recorded'
                        summary['jev_choices'][choice]=summary['jev_choices'].get(choice,0)+1
        if 'knowledge_control' in tables:
            for raw, in db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.conversation.record.%'"):
                data=json.loads(raw).get('data',{});result['organized_notes']['records']+=1
                if data.get('organized',{}).get('notes'):result['organized_notes']['with_notes']+=1
    return result


def compare(source,target):
    a,b=read(Path(source)),read(Path(target))
    if a.get('format')!='pcar-acceptance-evidence-v1' or b.get('format')!=a['format']:raise ValueError('两份记录必须由同一版本的核对工具生成。')
    changed=[t for t in sorted(set(a['business'])|set(b['business'])) if a['business'].get(t)!=b['business'].get(t)]
    new_instance=bool(a['identity']['instance_id'] and b['identity']['instance_id'] and a['identity']['instance_id']!=b['identity']['instance_id'])
    new_tokens=all(a['identity'].get(k)!=b['identity'].get(k) for k in ('admin_token','bridge_token') if a['identity'].get(k))
    db=b['database'];no_keys=not any(b['configured_keys'].values());no_grants=b['model_grant_count']==0
    checks={'business_fingerprints_match':not changed,'canon_hashes_match':a['canon_files']==b['canon_files'],
        'new_instance_identity':new_instance,'new_local_tokens':new_tokens,'before_new_keys_configured':no_keys,
        'old_grants_not_imported':no_grants,'database_integrity_ok':db.get('integrity')=='ok' and db.get('foreign_key_violations')==0}
    return {'comparison':'source_before_export_vs_target_before_first_open','checks':checks,'changed_business_tables':changed,
        'passed':all(checks.values()),'note':'只适用于新机重新输入密钥、授权和首次打开之前的正式保留性比较。'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--home',type=Path);group.add_argument('--compare',nargs=2,type=Path);parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    try:
        result=compare(*args.compare) if args.compare else collect(args.home)
        text=json.dumps(result,ensure_ascii=False,indent=2)
        if args.out:
            args.out.parent.mkdir(parents=True,exist_ok=True)
            with args.out.open('x',encoding='utf-8') as output:output.write(text+'\n')
        print(text)
        return 2 if args.compare and not result['passed'] else 0
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error) as error:
        print('核对未完成：'+str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
