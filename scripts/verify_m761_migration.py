"""Read-only formal backup; isolated 17/18 -> 19, rollback and restoration."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.archive import fingerprint
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.service import RuntimeService


def snapshot(conn,columns=None):
    if columns is None:
        columns={r[0]:[c[1] for c in conn.execute('PRAGMA table_info("'+r[0]+'")')]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    return columns,{t:fingerprint([list(r) for r in conn.execute('SELECT '+','.join('"'+c+'"' for c in cols)+' FROM "'+t+'" ORDER BY rowid')]) for t,cols in columns.items()}


def run(home,evidence):
    evidence.mkdir(parents=True,exist_ok=True);backup=evidence/'source17.sqlite3'
    if backup.exists():raise RuntimeError('Existing migration evidence; resume by checkpoint, never overwrite')
    with closing(sqlite3.connect((home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as source:
        if source.execute('PRAGMA user_version').fetchone()[0]!=17:raise RuntimeError('Baseline changed')
        with closing(sqlite3.connect(backup)) as dest:source.backup(dest)
    with closing(sqlite3.connect(backup)) as db:columns,before=snapshot(db)
    paths=RuntimePaths(evidence/'copy');paths.initialize();store=Store(paths);shutil.copy2(backup,store.path)
    from persona_runtime.schema_disposition import migrate
    def fault(conn):migrate(conn);raise OSError('injected disposition migration failure')
    try:
        with patch('persona_runtime.schema_disposition.migrate',side_effect=fault):store.initialize()
    except OSError:pass
    else:raise AssertionError('Fault not injected')
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==17
        assert snapshot(db,columns)[1]==before
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='disposition_versions'").fetchone()
    chains=[]
    for source_version in (17,18):
        shutil.copy2(backup,store.path)
        if source_version==18:
            with patch('persona_runtime.storage.SCHEMA_VERSION',18):store.initialize()
        with closing(sqlite3.connect(store.path)) as db:cols,old=snapshot(db)
        store.initialize();store.initialize()
        with store.connection() as db:
            assert snapshot(db,cols)[1]==old
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
        export=RuntimeService(store).export_persona()
        payload=json.loads(Path(export['path']).read_text(encoding='utf-8'))
        assert payload['format_version']==16 and 'disposition_versions' in payload
        def credentials(value):
            if isinstance(value,dict):return any(k in ('encrypted_key','admin_token','bridge_token') or credentials(v) for k,v in value.items())
            if isinstance(value,list):return any(credentials(v) for v in value)
            return False
        assert not credentials(payload)
        chains.append({'source_schema':source_version,'target_schema':19,'old_tables_verified':len(old),'old_columns_unchanged':True,'integrity':'ok','foreign_keys':0,'export':16})
    with closing(sqlite3.connect(backup)) as db:assert snapshot(db,columns)[1]==before
    result={'fault_rollback':True,'restored17_before_second_chain':True,'chains':chains,'formal_modified':False}
    (evidence/'report.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps(result))


if __name__=='__main__':run(Path(sys.argv[1]),Path(sys.argv[2]))
