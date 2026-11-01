"""Explicit read-only 19 backup, isolated 20 upgrade/rollback/restoration evidence."""
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
from persona_runtime.service import RuntimeService


def run(home,out):
    home=home.resolve();out=out.resolve()
    if not out.is_relative_to(home/'exports/acceptance'):raise ValueError('Explicit evidence directory required')
    out.mkdir(parents=True,exist_ok=True);backup=out/'source19.sqlite3'
    if backup.exists():raise ValueError('Existing evidence; inspect before resuming')
    with closing(sqlite3.connect((home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as source:
        assert source.execute('PRAGMA user_version').fetchone()[0]==19
        with closing(sqlite3.connect(backup)) as dest:source.backup(dest)
    with closing(sqlite3.connect(backup)) as db:columns,before=snapshot(db)
    paths=RuntimePaths(out/'copy');paths.initialize();store=Store(paths);shutil.copy2(backup,store.path)
    from persona_runtime.schema_observation import migrate
    def fail(conn):migrate(conn);raise OSError('injected migration fault')
    try:
        with patch('persona_runtime.schema_observation.migrate',side_effect=fail):store.initialize()
    except OSError:pass
    else:raise AssertionError('Fault did not run')
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==19
        assert snapshot(db,columns)[1]==before
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='situation_observation_attempts'").fetchone()
    chains=[]
    for cycle in (1,2):
        if cycle==2:shutil.copy2(backup,store.path)
        store.initialize();store.initialize()
        with store.connection() as db:
            assert snapshot(db,columns)[1]==before
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        export=RuntimeService(store).export_persona('m763-copy-'+str(cycle))
        payload=json.loads(Path(export['path']).read_text(encoding='utf-8'))
        assert payload['format_version']==17 and 'situation_observation_attempts' in payload
        def secret(v):
            if isinstance(v,dict):return any(k in ('encrypted_key','admin_token','bridge_token','observation_model_snapshot','model_snapshot') or secret(x) for k,x in v.items())
            if isinstance(v,list):return any(secret(x) for x in v)
            return False
        assert not secret(payload)
        chains.append({'cycle':cycle,'from':19,'to':20,'old_tables':len(before),'old_columns_unchanged':True,'export':17})
    with closing(sqlite3.connect((home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as source:assert snapshot(source,columns)[1]==before
    report={'chains':chains,'fault_rollback':True,'restored19_then_upgraded':True,'formal_unchanged':True,'credentials_excluded':True}
    (out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))


if __name__=='__main__':run(Path(sys.argv[1]),Path(sys.argv[2]))
