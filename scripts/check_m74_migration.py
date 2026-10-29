"""Read-only formal source; prove v13 backup, two v14 upgrades and exact restoration."""
import argparse
import hashlib
import json
from pathlib import Path
from scripts.check_m73_migration import copy_db,snapshot
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def check(source,out):
    if out.exists() or not out.is_relative_to(RuntimePaths.from_environment().home/'exports/acceptance'):
        raise ValueError('Use a new private acceptance directory')
    out.mkdir(parents=True);backup=out/'schema13-backup.sqlite3';copy_db(source,backup)
    version,original=snapshot(backup)
    if version!=13:raise ValueError('Expected actual schema13 source')
    reports=[]
    for name in ('first-upgrade','restored-upgrade'):
        paths=RuntimePaths(out/name);store=Store(paths);copy_db(backup,store.path)
        store.initialize();store.initialize();version,after=snapshot(store.path,original)
        with store.connection() as c:
            integrity=c.execute('PRAGMA integrity_check').fetchone()[0]
            foreign=len(c.execute('PRAGMA foreign_key_check').fetchall())
            no_backfill=c.execute('SELECT count(*) FROM turns WHERE expression_snapshot IS NOT NULL OR new_input_event_id IS NOT NULL').fetchone()[0]==0
        export=RuntimeService(store).export_persona('migration-export11.json')
        text=Path(export['path']).read_text(encoding='utf-8');data=json.loads(text)
        preserved={t:after[t]==original[t] for t in original}
        result={'chain':name,'schema':version,'old_table_original_columns_preserved':preserved,'integrity':integrity,
                'foreign_key_errors':foreign,'no_history_backfill':no_backfill,'export_version':data['format_version'],
                'no_credentials':'encrypted_key' not in text and 'claim_token' not in text,
                'export_sha256':hashlib.sha256(text.encode()).hexdigest()}
        result['passed']=all(preserved.values()) and version==14 and integrity=='ok' and not foreign and no_backfill and result['no_credentials'] and data['format_version']==11
        reports.append(result)
    restored=out/'restored-schema13.sqlite3';copy_db(backup,restored)
    restored_same=snapshot(restored)==(13,original)
    report={'source_read_only':True,'old_table_hashes':original,'chains':reports,'restored_schema13_exact':restored_same,
            'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),'passed':restored_same and all(r['passed'] for r in reports)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'old_tables':len(original),'chains':2}))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();check(a.source.resolve(),a.output.resolve())
