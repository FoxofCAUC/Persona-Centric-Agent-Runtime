"""Read a stopped schema-11 DB; migrate/restore copies only, emit hashes."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def copy_db(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def snapshot(path,contract=None):
    with closing(sqlite3.connect(path)) as db:
        version=db.execute('PRAGMA user_version').fetchone()[0]
        tables=contract or {r[0]:None for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        result={}
        for table,old in tables.items():
            columns=old['columns'] if old else [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            sql='SELECT '+','.join('"'+c+'"' for c in columns)+' FROM "'+table+'" ORDER BY rowid'
            rows=db.execute(sql).fetchall();rows=rows[:old['count']] if old else rows
            result[table]={'columns':columns,'count':len(rows),'hash':fingerprint(rows)}
        return version,result


def check(source,out):
    if out.exists() or out.is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError('Use a new D-drive evidence folder')
    out.mkdir(parents=True);backup=out/'schema11-backup.sqlite3';copy_db(source,backup)
    version,original=snapshot(backup)
    if version!=11:raise ValueError('Expected actual schema11 source')
    reports=[]
    for name in ('first-upgrade','restored-upgrade'):
        paths=RuntimePaths(out/name);store=Store(paths);copy_db(backup,store.path)
        store.initialize();store.initialize();after_version,after=snapshot(store.path,original)
        preservation={t:after[t]==original[t] for t in original if t!='interaction_heads'}
        with store.connection() as db:
            integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
            foreign=len(db.execute('PRAGMA foreign_key_check').fetchall())
            historical_null=db.execute('SELECT count(*) FROM turns WHERE context_policy_snapshot IS NOT NULL').fetchone()[0]==0
            rows=db.execute('SELECT p.data_json,p.mode,p.reply_mode FROM interaction_heads h JOIN interaction_policies p ON p.id=h.version_id').fetchall()
            policy_defaults=all(json.loads(r[0]).get('context')=={'context_mode':'working_set','cross_session':False} for r in rows)
            policy_count=len(rows)
        exported=RuntimeService(store).export_persona('migration-export9.json')
        text=Path(exported['path']).read_text(encoding='utf-8');data=json.loads(text)
        report={'chain':name,'schema':after_version,'old_table_original_columns_preserved':preservation,
                'intentional_pointer_change':'interaction_heads now point at new context-policy revisions',
                'historical_turns_stay_null':historical_null,'context_policy_default_valid':policy_defaults,
                'policy_heads':policy_count,'integrity':integrity,'foreign_key_errors':foreign,
                'export_version':data['format_version'],'export_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'no_credential_fields':'encrypted_key' not in text and 'claim_token' not in text,
                'new_export_keys_present':all(k in data for k in ('context_manifests','turn_context_policies'))}
        report['passed']=all(preservation.values()) and after_version==12 and historical_null and policy_defaults and integrity=='ok' and foreign==0 and report['no_credential_fields'] and report['new_export_keys_present']
        reports.append(report)
    restored=out/'restored-schema11.sqlite3';copy_db(backup,restored)
    restored_same=snapshot(restored)==(version,original)
    report={'source_was_read_only':True,'source_schema':version,'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),
            'old_table_hashes':original,'restored_schema11_exact':restored_same,'chains':reports,
            'passed':restored_same and all(r['passed'] for r in reports)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'chains':len(reports),'old_tables':len(original),'schema11_restore':restored_same}))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.source.resolve(),args.output.resolve())
