"""Offline protected compatibility snapshot. The live database is never replaced."""
from contextlib import closing
from pathlib import Path
import hashlib,json,sqlite3,uuid
from .models import RuntimeErrorBase

def _rows(db):
    result={}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        if name in ('recall_runtime_policies','expression_asset_policies','expression_runtime_policies'):continue
        table='"'+name.replace('"','""')+'"';digest=hashlib.sha256()
        columns='*'
        if name in ('expression_learning_jobs','expression_runs','expression_speech_runs'):
            columns=','.join('"'+r[1]+'"' for r in db.execute('PRAGMA table_info('+table+')') if r[1] not in ('state','error_code'))
        if name in ('expression_runtime_turns','expression_speech_units'):
            columns=','.join('"'+r[1]+'"' for r in db.execute('PRAGMA table_info('+table+')') if r[1] not in ('cancelled_at','closed_at'))
        for row in db.execute('SELECT rowid,'+columns+' FROM '+table+' ORDER BY rowid'):digest.update(repr(tuple(row)).encode());digest.update(b'\n')
        result[name]=digest.hexdigest()
    return result

def prepare_snapshot(paths, *, target_schema=28):
    from .migration import _closed,_offline
    from .character_embedding import private_directory
    from .storage import Store
    if target_schema not in (28,29,30,31):raise RuntimeErrorBase('rollback_target_unsupported')
    _closed(paths)
    with _offline(paths):
        root=private_directory(paths.home,paths.home/'backups')
        output=root/('recall-compatible'+str(target_schema)+'-'+uuid.uuid4().hex+'.sqlite3')
        with closing(sqlite3.connect(Store(paths).path.as_uri()+'?mode=ro',uri=True)) as src:
            src.row_factory=sqlite3.Row;Store._check(src)
            if src.execute('PRAGMA user_version').fetchone()[0] not in (29,30,31,32):raise RuntimeErrorBase('recall_rollback_version_unsupported')
            if src.execute("SELECT 1 FROM sqlite_master WHERE name='expression_publications'").fetchone() and src.execute("SELECT 1 FROM expression_publications WHERE state NOT IN ('closed','complete') LIMIT 1").fetchone():
                raise RuntimeErrorBase('expression_rollback_pending_presentation')
            if src.execute("SELECT 1 FROM sqlite_master WHERE name='expression_speech_publications'").fetchone() and src.execute("SELECT 1 FROM expression_speech_publications WHERE state NOT IN ('closed','complete') LIMIT 1").fetchone():
                raise RuntimeErrorBase('speech_rollback_pending_presentation')
            if src.execute("SELECT 1 FROM turns WHERE status IN ('running','queued') LIMIT 1").fetchone():raise RuntimeErrorBase('recall_rollback_pending_requests')
            before=_rows(src)
            with closing(sqlite3.connect(output)) as dst:src.backup(dst)
        with closing(sqlite3.connect(output)) as db,db:
            from .expression_compat import pause
            pause(db)
            for pid,revision,raw in db.execute('SELECT * FROM recall_runtime_policies').fetchall():
                value=json.loads(raw);value['enabled']=False
                db.execute('UPDATE recall_runtime_policies SET revision=?,data_json=? WHERE persona_id=?',(revision+1,json.dumps(value,ensure_ascii=False),pid))
            db.execute('PRAGMA user_version='+str(target_schema))
            if _rows(db)!=before:raise RuntimeErrorBase('recall_rollback_business_changed')
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeErrorBase('recall_rollback_integrity_failed')
        with output.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        return {'path':str(output),'sha256':digest,'target_schema':target_schema,'original_database_replaced':False,
            'all_business_and_recall_rows_preserved':True,'recall_runtime_disabled':True,'model_calls':0,
            'scope':'database snapshot only; keep the instance files/configuration in the same offline backup'}
