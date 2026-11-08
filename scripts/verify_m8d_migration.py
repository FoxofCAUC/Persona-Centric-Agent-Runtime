"""22 -> 23 offline verification: source is read-only, network is prohibited."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.verify_m761_migration import snapshot
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.character_embedding import private_directory
from persona_runtime.private_personas import safe
from persona_runtime.service import RuntimeService


def verify(source,out):
    source=Path(source).resolve();out=Path(out).resolve();home=RuntimePaths.from_environment().home
    safe(out,home/'exports/acceptance')
    if out.exists():raise ValueError('Evidence already exists')
    private_directory(home,out);backup=out/'source22.sqlite3'
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==22
        columns,before=snapshot(db)
        with closing(sqlite3.connect(backup)) as target:db.backup(target)
    paths=RuntimePaths(out/'copy');paths.initialize();store=Store(paths);shutil.copy2(backup,store.path)
    from persona_runtime.schema_m8d import migrate
    def fault(db):migrate(db);raise OSError('injected automatic memory migration failure')
    with patch('socket.socket.connect',side_effect=AssertionError('offline only')):
        try:
            with patch('persona_runtime.schema_m8d.migrate',side_effect=fault):store.initialize()
        except OSError:pass
        else:raise AssertionError('Fault not injected')
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==22
            assert snapshot(db,columns)[1]==before
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_batches'").fetchone()
        for cycle in range(2):
            if cycle:shutil.copy2(backup,store.path)
            store.initialize();store.initialize()
            with store.connection() as db:
                assert snapshot(db,columns)[1]==before
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
                assert not db.execute('SELECT 1 FROM memory_batches').fetchone()
                assert not db.execute('SELECT 1 FROM memory_defaults').fetchone()
            payload=json.loads(Path(RuntimeService(store).export_persona('m8d-migration-'+str(cycle))['path']).read_text(encoding='utf-8'))
            def forbidden(v):
                if isinstance(v,dict):return any(k in ('organization_policies','organization_contracts','organization_execution','encrypted_key','admin_token','bridge_token','model_snapshot','claim_token') or forbidden(x) for k,x in v.items())
                if isinstance(v,list):return any(forbidden(x) for x in v)
                return False
            assert payload['format_version']==18 and not forbidden(payload)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==22 and snapshot(db,columns)[1]==before
    result={'from':22,'to':23,'fault_rollback':True,'repeated_initialization':True,'old_tables_columns_rows_unchanged':True,'existing_roles_not_enabled':True,'old_tasks_not_replayed':True,'export':18,'credentials_authorization_leases_excluded':True,'restore_then_upgrade':True,'source_unchanged':True,'network_calls':0,'integrity':'ok'}
    (out/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8');return result


if __name__=='__main__':print(json.dumps(verify(sys.argv[1],sys.argv[2])))
