"""Read-only upgrade/rollback evidence; output hashes, never business text or credentials."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

FORMAT='pcar-upgrade-evidence-v1'
BUSINESS_TABLES=('personas','sessions','events','raw_events','input_batches','input_batch_members','input_references',
    'turns','reply_batches','reply_segments','reply_display_messages','memories','memory_sources','memory_facts',
    'profile_versions','character_versions','source_exclusions','summaries','memory_decisions','knowledge_pages',
    'knowledge_documents','knowledge_notes','knowledge_claims','context_uses','context_manifests',
    'attempts','organization_attempts','organization_receipts')
BUSINESS_KEYS=('knowledge.conversation.record.','knowledge.processing.result.','knowledge.native.receipt.',
    'knowledge.native.source_state.','knowledge.native.imported.')
# Keep the read-only checker standalone, including when an old runtime cannot import.
BUSINESS_TABLES+=('recall_support_policies','recall_support_clock','recall_support_units',
    'recall_support_locations','recall_support_terms','recall_support_events','recall_support_searches','recall_support_sync')
BUSINESS_TABLES+=('recall_runtime_policies','recall_runtime_turns','recall_runtime_dirty','recall_runtime_feedback')
EXPRESSION_TABLES=('expression_asset_policies','expression_asset_operations','expression_asset_sources',
    'expression_asset_source_versions','expression_assets','expression_asset_versions','expression_asset_supports',
    'expression_asset_terms','expression_asset_vectors','expression_candidate_snapshots','expression_learning_jobs',
    'expression_asset_proposals','expression_asset_observations','expression_learning_scan')
BUSINESS_TABLES+=EXPRESSION_TABLES
EXPRESSION_RUNTIME_TABLES=('expression_runtime_policies','expression_runtime_turns','expression_main_receipts',
    'expression_runs','expression_publications','expression_publication_versions',
    'expression_presentation_items','expression_utterances','expression_presentation_receipts')
BUSINESS_TABLES+=EXPRESSION_RUNTIME_TABLES


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def scalar(value):
    return {'blob_sha256':hashlib.sha256(value).hexdigest()} if isinstance(value,bytes) else value


def snapshot(home):
    path=Path(home).resolve()/'data/database/runtime.sqlite3'
    if not path.is_file():raise ValueError('upgrade_database_missing')
    result={'format':FORMAT,'schema_version':None,'tables':{},'readable_records':{},'attempts_by_root':{}}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        result['schema_version']=db.execute('PRAGMA user_version').fetchone()[0]
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in BUSINESS_TABLES:
            if table not in tables:continue
            primary=[r['name'] for r in db.execute('PRAGMA table_info('+table+')') if r['pk']]
            rows={}
            for row in db.execute('SELECT rowid AS _upgrade_rowid,* FROM '+table):
                data={key:scalar(row[key]) for key in row.keys() if key!='_upgrade_rowid'}
                key=[row[name] for name in primary] if primary else [row['_upgrade_rowid']]
                rows[digest(key)]=digest(data)
            result['tables'][table]=rows
        if 'knowledge_control' in tables:
            for key,raw in db.execute('SELECT key,value FROM knowledge_control'):
                if key.startswith(BUSINESS_KEYS):result['readable_records'][digest(key)]=digest(raw)
        for table in ('attempts','character_selection_attempts','situation_observation_attempts'):
            if table not in tables:continue
            for row in db.execute('SELECT id,turn_id FROM '+table+' WHERE turn_id IS NOT NULL'):
                result['attempts_by_root'].setdefault(digest(row['turn_id']),[]).append(digest([table,row['id']]))
        if {'organization_attempts','organization_tasks'}<=tables:
            for row in db.execute('SELECT a.id,t.id AS task_id,t.payload_json FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id'):
                payload=json.loads(row['payload_json']);root=payload.get('knowledge_processing',{}).get('root_turn_id') or 'task:'+row['task_id']
                result['attempts_by_root'].setdefault(digest(root),[]).append(digest(['organization_attempts',row['id']]))
        if 'expression_learning_jobs' in tables:
            for ident,attempt in db.execute('SELECT id,attempt_id FROM expression_learning_jobs WHERE attempt_id IS NOT NULL'):
                result['attempts_by_root'].setdefault(digest('expression-learning:'+ident),[]).append(digest(['expression_learning',attempt]))
        if 'expression_runs' in tables:
            for root,attempt in db.execute('SELECT turn_id,attempt_id FROM expression_runs WHERE attempt_id IS NOT NULL'):
                result['attempts_by_root'].setdefault(digest(root),[]).append(digest(['expression',attempt]))
    for values in result['attempts_by_root'].values():values.sort()
    return result


def compare(before,after,*,allow_schema_upgrade=False):
    if before.get('format')!=FORMAT or after.get('format')!=FORMAT:raise ValueError('upgrade_evidence_invalid')
    failures=[]
    if before['schema_version']!=after['schema_version']:
        expected={'recall_support_policies','recall_support_clock','recall_support_units','recall_support_locations',
            'recall_support_terms','recall_support_events','recall_support_searches','recall_support_sync'}
        if after['schema_version']>=29:expected.update(('recall_runtime_policies','recall_runtime_turns','recall_runtime_dirty','recall_runtime_feedback'))
        if after['schema_version']>=30:
            expected.update(EXPRESSION_TABLES)
        if after['schema_version']>=31:expected.update(EXPRESSION_RUNTIME_TABLES)
        if not (allow_schema_upgrade and 27<=before['schema_version']<after['schema_version']<=31
                and expected.issubset(after['tables'])):failures.append({'check':'schema_changed'})
    for table,rows in before['tables'].items():
        new=after['tables'].get(table,{})
        for key,value in rows.items():
            if new.get(key)!=value:failures.append({'check':'existing_row_changed','table':table,'key_hash':key})
    for key,value in before['readable_records'].items():
        if after['readable_records'].get(key)!=value:failures.append({'check':'readable_record_changed','key_hash':key})
    for root,attempts in before['attempts_by_root'].items():
        if after['attempts_by_root'].get(root,[])!=attempts:failures.append({'check':'old_root_attempts_changed','root_hash':root})
    return {'passed':not failures,'checks':['existing_business_rows','readable_records','old_root_no_new_attempts'],
        'failures':failures,'new_business_rows':sum(len(rows)-len(before['tables'].get(table,{})) for table,rows in after['tables'].items())}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    take=sub.add_parser('snapshot');take.add_argument('--home',type=Path,required=True);take.add_argument('--out',type=Path,required=True)
    check=sub.add_parser('compare');check.add_argument('before',type=Path);check.add_argument('after',type=Path)
    check.add_argument('--allow-schema-upgrade',action='store_true',help='Allow declared additive upgrades from 27 through 31; existing rows still must match.')
    args=parser.parse_args()
    if args.action=='snapshot':
        value=snapshot(args.home);args.out.parent.mkdir(parents=True,exist_ok=True)
        with args.out.open('x',encoding='utf-8') as output:json.dump(value,output,ensure_ascii=False,indent=2)
        result={'saved':True,'schema_version':value['schema_version'],'business_rows':sum(map(len,value['tables'].values())),'model_calls':0}
    else:result=compare(json.loads(args.before.read_text(encoding='utf-8-sig')),json.loads(args.after.read_text(encoding='utf-8-sig')),allow_schema_upgrade=args.allow_schema_upgrade)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result.get('passed',True) else 1


if __name__=='__main__':raise SystemExit(main())
