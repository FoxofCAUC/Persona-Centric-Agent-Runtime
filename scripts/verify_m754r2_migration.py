"""Isolated 14 -> 15 evidence; never writes the source database."""
from contextlib import closing
import json,sqlite3,sys,shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.storage import Store
from persona_runtime.paths import RuntimePaths
from persona_runtime.character_embedding import digest

def snapshot(db,columns=None):
    if columns is None:columns={r[0]:[c[1] for c in db.execute('PRAGMA table_info("'+r[0]+'")')] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    return columns,{t:digest([list(r) for r in db.execute('SELECT '+','.join('"'+c+'"' for c in cols)+' FROM "'+t+'" ORDER BY rowid')]) for t,cols in columns.items()}

def run(home,evidence):
    evidence.mkdir(parents=True,exist_ok=True);backup=evidence/'source14.sqlite3'
    if backup.exists():raise RuntimeError('Evidence exists; do not replay')
    source=sqlite3.connect((home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)
    assert source.execute('PRAGMA user_version').fetchone()[0]==14
    dest=sqlite3.connect(backup);source.backup(dest);dest.close();source.close()
    with closing(sqlite3.connect(backup)) as db,db:columns,before=snapshot(db)
    paths=RuntimePaths(evidence/'copy');paths.initialize();store=Store(paths)
    shutil.copy2(backup,store.path)
    from persona_runtime.schema_m754r2 import migrate
    def fault(conn):migrate(conn);raise OSError('injected failure')
    try:
        with patch('persona_runtime.schema_m754r2.migrate',side_effect=fault):store.initialize()
    except OSError:pass
    else:raise AssertionError('fault did not fire')
    with closing(sqlite3.connect(store.path)) as db,db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==14
        assert snapshot(db,columns)[1]==before
    chains=[]
    for n in range(2):
        if n:shutil.copy2(backup,store.path)
        store.initialize();store.initialize()
        with store.connection() as db:
            after=snapshot(db,columns)[1];assert after==before
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            chains.append({'chain':n+1,'schema':15,'old_tables_verified':len(before),'unchanged':True})
    result={'fault_rollback':True,'chains':chains,'formal_modified':False}
    (evidence/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':run(Path(sys.argv[1]),Path(sys.argv[2]))
