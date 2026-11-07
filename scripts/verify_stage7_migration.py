"""20 -> 21 offline verification. Source is read-only; no config or models copied."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.verify_m761_migration import snapshot
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from persona_runtime.character_embedding import private_directory
from persona_runtime.private_personas import safe


def verify(source, out):
    source=Path(source).resolve();out=Path(out).resolve()
    home=RuntimePaths.from_environment().home
    safe(out, home/'exports/acceptance')
    if out.exists():raise ValueError('Existing evidence must not be overwritten')
    private_directory(home,out)
    backup=out/'source20.sqlite3'
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0]!=20:raise ValueError('Expected schema 20')
        columns,before=snapshot(db)
        with closing(sqlite3.connect(backup)) as target:db.backup(target)
    paths=RuntimePaths(out/'copy');paths.initialize();store=Store(paths)
    shutil.copy2(backup,store.path)
    from persona_runtime.organization_output import migrate
    def fault(conn):
        migrate(conn)
        raise OSError('injected receipt migration failure')
    with patch('socket.socket.connect',side_effect=AssertionError('offline migration')):
        try:
            with patch('persona_runtime.organization_output.migrate',side_effect=fault):store.initialize()
        except OSError:pass
        else:raise AssertionError('Fault not injected')
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==20
            assert snapshot(db,columns)[1]==before
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='organization_receipts'").fetchone()
        for cycle in range(2):
            if cycle:shutil.copy2(backup,store.path)
            store.initialize();store.initialize()
            with store.connection() as db:
                assert db.execute('PRAGMA user_version').fetchone()[0]==21
                assert snapshot(db,columns)[1]==before
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
            exported=RuntimeService(store).export_persona('stage7-migration-'+str(cycle))
            payload=json.loads(Path(exported['path']).read_text(encoding='utf-8'))
            def secret(v):
                if isinstance(v,dict):return any(k in ('encrypted_key','admin_token','bridge_token','model_snapshot','observation_model_snapshot') or secret(x) for k,x in v.items())
                if isinstance(v,list):return any(secret(x) for x in v)
                return False
            assert payload['format_version']==17 and not secret(payload)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==20
        assert snapshot(db,columns)[1]==before
    report={'from':20,'to':21,'fault_rollback':True,'repeat_initialize':True,
        'old_tables_columns_rows_unchanged':True,'integrity':'ok','export':17,
        'credentials_excluded':True,'restore_then_upgrade':True,'source_unchanged':True,
        'network_calls':0,'old_table_count':len(columns)}
    (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    print(json.dumps(verify(sys.argv[1],sys.argv[2])))
