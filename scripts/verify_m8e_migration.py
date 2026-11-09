"""Read-only source → protected isolated 23→24 upgrade and rollback evidence."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.character_embedding import private_directory
from persona_runtime.config import atomic_json
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.private_personas import safe
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService
from scripts.verify_m761_migration import snapshot


def verify(source,out):
    source=Path(source).resolve();out=safe(Path(out).resolve(),DEFAULT_HOME/'exports/acceptance')
    if out.exists():raise RuntimeError('New evidence directory required')
    private_directory(DEFAULT_HOME,out)
    backup=out/'source23.sqlite3'
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0]!=23:raise RuntimeError('Expected schema 23')
        columns,before=snapshot(db)
        with closing(sqlite3.connect(backup)) as dest:db.backup(dest)
    paths=RuntimePaths(out/'copy');private_directory(DEFAULT_HOME,paths.home);paths.initialize()
    store=Store(paths);shutil.copy2(backup,store.path)
    from persona_runtime.schema_m8e import migrate
    def fault(db):migrate(db);raise OSError('injected M8.E migration fault')
    with patch('socket.socket.connect',side_effect=AssertionError('No network in migration verification')):
        try:
            with patch('persona_runtime.schema_m8e.migrate',side_effect=fault):store.initialize()
        except OSError:pass
        else:raise AssertionError('Missing rollback fault')
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==23
            assert snapshot(db,columns)[1]==before
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='proactive_rules'").fetchone()
        for cycle in range(2):
            if cycle:shutil.copy2(backup,store.path)
            store.initialize();store.initialize()
            with store.connection() as db:
                assert snapshot(db,columns)[1]==before
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
            package=json.loads(Path(RuntimeService(store).export_persona('m8e-migration-'+str(cycle))['path']).read_text(encoding='utf-8'))
            assert package['format_version']==19
            def forbidden(value):
                if isinstance(value,dict):return any(k in ('encrypted_key','admin_token','bridge_token','model_snapshot','claim_token','sender_json','console_token','session_token','proactive_policies','organization_execution') or forbidden(v) for k,v in value.items())
                return any(forbidden(v) for v in value) if isinstance(value,list) else False
            assert not forbidden(package)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==23 and snapshot(db,columns)[1]==before
    result={'from':23,'to':24,'fault_rollback':True,'repeat_initialize':True,'old_columns_rows_unchanged':True,
        'isolated_restore_upgrade':True,'integrity':'ok','source_unchanged':True,'network_calls':0,
        'export':19,'credentials_authorization_leases_excluded':True}
    atomic_json(out/'report.json',result)
    return result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('out',type=Path)
    args=parser.parse_args();print(json.dumps(verify(args.source,args.out)))
