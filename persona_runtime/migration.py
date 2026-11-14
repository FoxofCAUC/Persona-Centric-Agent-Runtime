"""Offline whole-business migration. IDs and canon stay; machine secrets do not."""
from contextlib import closing,contextmanager,ExitStack
from datetime import datetime,timezone
import copy
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

from .installation import atomic_json,read_json,validate_home
from .models import RuntimeErrorBase

FORMAT='pcar-business-migration-v1'
SECRET_FIELDS={'encrypted_key','api_key','admin_token','bridge_token','claim_token','console_token','session_token','access_token','refresh_token'}
PRIVATE_FILES={'source-package.json','source-archive.md','initial-relationship.txt'}

def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def marker_file(paths):return paths.home/'runtime/migration-state.json'

def migration_status(paths):
    marker=marker_file(paths)
    if marker.exists():
        try:
            value=read_json(marker)
            if value.get('version')!=1 or value.get('state') not in ('installing','needs_configuration','active','failed'):
                raise ValueError()
            return value
        except (OSError,ValueError):raise RuntimeErrorBase('migration_state_unavailable') from None
    return None

def migration_pending(paths):
    value=migration_status(paths)
    return bool(value and value['state']!='active')

def background_allowed(paths):
    value=migration_status(paths)
    return value is None or value['state']=='active' and value.get('resume_background',False)

def ensure_cloud_ready(paths):
    if migration_pending(paths):raise RuntimeErrorBase('migration_configuration_required')

def privacy_binding(paths):
    value=migration_status(paths)
    if value is None:return None
    from .config import ConfigStore
    from .archive import fingerprint
    instance=ConfigStore(paths).load()['instance_id']
    if value.get('instance_id')!=instance:raise RuntimeErrorBase('migration_instance_changed')
    return fingerprint({'migration_id':value['migration_id'],'instance_id':instance,'home':str(paths.home.resolve())})

def _closed(paths):
    from .instance import process_identity_alive
    file=paths.home/'runtime/console.json'
    if file.exists():
        value=read_json(file)
        if value.get('state')=='active' and process_identity_alive(value.get('process')):
            raise RuntimeErrorBase('请先正常退出当前实例，再进行整库迁移。')
    from .lifecycle import same_process
    cfg=paths.home/'config/workbench.json'
    instance=read_json(cfg).get('instance_id') if cfg.exists() else None
    for role in ('service','astrbot','napcat'):
        file=paths.home/'runtime'/(role+'.json')
        if file.exists():
            value=read_json(file)
            if instance and value.get('instance_id')==instance and any(same_process(value.get(k)) for k in ('host','loader','qq_listener','qq_parent')):
                raise RuntimeErrorBase('请先正常退出当前实例所属组件，再进行整库迁移。')

@contextmanager
def _offline(paths):
    from .instance import InstanceLock
    with ExitStack() as stack:
        for name in ('desktop','console','service'):
            lock=InstanceLock(paths.home/'runtime'/(name+'.lock'));stack.callback(lock.close)
        _closed(paths)
        yield


def _clean(value):
    if isinstance(value,dict):return {k:('' if k in SECRET_FIELDS else _clean(v)) for k,v in value.items()}
    if isinstance(value,list):return [_clean(v) for v in value]
    return value

def _safe_config(value):
    result={}
    for key in ('service_port','model','interaction','organization','character_selector'):
        if key in value:result[key]=_clean(copy.deepcopy(value[key]))
    return result

def _snapshot(source,target):
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _row_positions(db):
    """History coverage uses SQLite row positions as well as declared IDs."""
    result={}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        quoted='"'+table.replace('"','""')+'"'
        try:rows=db.execute('SELECT rowid,* FROM '+quoted+' ORDER BY rowid')
        except sqlite3.OperationalError:continue  # WITHOUT ROWID uses its declared key.
        digest=hashlib.sha256()
        for row in rows:digest.update(repr(tuple(row)).encode('utf-8'));digest.update(b'\n')
        result[table]=digest.hexdigest()
    return result

def _sanitize_database(path):
    changes={}
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('PRAGMA trusted_schema=OFF')
        from .storage import Store
        Store._check(db)
        db.execute('PRAGMA foreign_keys=ON')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(tables):
            if table.startswith('sqlite_'):continue
            columns=[r[1] for r in db.execute('PRAGMA table_info("'+table.replace('"','""')+'")')]
            quoted='"'+table.replace('"','""')+'"'
            for column in columns:
                if column not in SECRET_FIELDS and not column.endswith(('_json','model_snapshot')):continue
                q='"'+column.replace('"','""')+'"'
                for rowid,raw in db.execute('SELECT rowid,'+q+' FROM '+quoted+' WHERE '+q+' IS NOT NULL').fetchall():
                    if column in SECRET_FIELDS:clean=''
                    elif column=='sender_json':clean='{}'
                    else:
                        try:
                            parsed=json.loads(raw);cleaned=_clean(parsed)
                            if cleaned==parsed:continue
                            clean=json.dumps(cleaned,ensure_ascii=False)
                        except (ValueError,TypeError):continue
                    if clean!=raw:
                        db.execute('UPDATE '+quoted+' SET '+q+'=? WHERE rowid=?',(clean,rowid));changes['structured_credentials_scrubbed']=changes.get('structured_credentials_scrubbed',0)+1
        # Pending work has no new-machine execution authority. Completed and
        # unknown outcomes and their usage stay untouched.
        for table,states,target in [('turns',('queued','running'),'interrupted'),('organization_tasks',('queued','running','waiting_quota'),'failed')]:
            if table in tables:
                cur=db.execute('UPDATE '+table+" SET status=?,error_code='migration_requires_review' WHERE status IN ("+','.join('?' for _ in states)+')',(target,*states));changes[table+'_paused']=cur.rowcount
        if 'attempts' in tables:db.execute("UPDATE attempts SET status='interrupted',error_code='migration_requires_review' WHERE status='running'")
        if 'organization_attempts' in tables:db.execute("UPDATE organization_attempts SET status='interrupted',error_code='migration_requires_review' WHERE status='running'")
        if 'organization_contracts' in tables:
            for ident,raw in db.execute('SELECT task_id,data_json FROM organization_contracts').fetchall():
                value=json.loads(raw);value['process']=None
                task=db.execute('SELECT status FROM organization_tasks WHERE id=?',(ident,)).fetchone()
                if task and task[0]=='failed':
                    unknown=db.execute("SELECT 1 FROM organization_attempts a WHERE task_id=? AND status='interrupted' AND NOT EXISTS(SELECT 1 FROM organization_receipts r WHERE r.attempt_id=a.id)",(ident,)).fetchone()
                    value['phase']='unknown' if unknown else 'failed_known'
                    if unknown:db.execute("UPDATE organization_tasks SET status='blocked_unknown' WHERE id=?",(ident,))
                db.execute('UPDATE organization_contracts SET data_json=? WHERE task_id=?',(json.dumps(value,ensure_ascii=False),ident))
        if 'organization_execution' in tables:db.execute('UPDATE organization_execution SET task_id=NULL,owner=NULL,lease_until=0')
        for table in ('quota_reservations','organization_reservations'):
            if table in tables:db.execute("UPDATE "+table+" SET state='released' WHERE state='active'")
        if 'deliveries' in tables:db.execute("UPDATE deliveries SET state='uncertain' WHERE state='sending'")
        if 'reply_delivery_parts' in tables:
            db.execute("UPDATE reply_delivery_parts SET state='skipped',error='migration_requires_review' WHERE state='pending'")
            db.execute("UPDATE reply_delivery_parts SET state='uncertain',error='migration_requires_review' WHERE state='sending'")
        if 'input_batches' in tables:db.execute("UPDATE input_batches SET state='cancelled',seal_reason='migration_requires_review' WHERE state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')")
        for table in ('disposition_compile_attempts','situation_observation_attempts','character_selection_attempts'):
            if table in tables:db.execute("UPDATE "+table+" SET status='interrupted' WHERE status='running'")
        if 'proactive_rules' in tables:
            for rowid,raw in db.execute('SELECT rowid,data_json FROM proactive_rules').fetchall():
                value=json.loads(raw);value['enabled']=False
                db.execute('UPDATE proactive_rules SET data_json=? WHERE rowid=?',(json.dumps(value,ensure_ascii=False),rowid))
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchone():raise RuntimeErrorBase('migration_database_invalid')
        counts={t:db.execute('SELECT count(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in ('personas','sessions','events','memories','knowledge_control') if t in tables}
        # Rebuild pages so overwritten DPAPI values do not survive in freelists.
        positions=_row_positions(db)
        db.commit();db.execute('VACUUM')
        if _row_positions(db)!=positions:raise RuntimeErrorBase('migration_history_positions_changed')
        return changes,counts,db.execute('PRAGMA user_version').fetchone()[0]

def export_instance(paths,name=None):
    validate_home(paths.home)
    _closed(paths)
    with _offline(paths):
        source=paths.home/'data/database/runtime.sqlite3'
        from .private_personas import safe
        safe(source,paths.home)
        if not source.is_file():raise RuntimeErrorBase('migration_source_missing')
        from .character_embedding import private_directory
        exports=private_directory(paths.home,paths.home/'exports');temporary=private_directory(paths.home,paths.home/'tmp')
        name=name or 'instance-migration-'+uuid.uuid4().hex+'.pcar-migration.zip'
        if Path(name).name!=name or not name.endswith('.zip'):raise RuntimeErrorBase('migration_package_name_invalid')
        destination=exports/name
        if destination.exists():raise RuntimeErrorBase('migration_package_exists')
        with tempfile.TemporaryDirectory(prefix='migration-export-',dir=temporary) as folder:
            root=Path(folder);database=root/'database.sqlite3';_snapshot(source,database)
            changes,counts,version=_sanitize_database(database)
            from .storage import SCHEMA_VERSION
            if version!=SCHEMA_VERSION:raise RuntimeErrorBase('请先在原实例完成正常升级，再导出迁移包。')
            atomic_json(root/'config-public.json',_safe_config(read_json(paths.home/'config/workbench.json')))
            entries={'database.sqlite3':database,'config-public.json':root/'config-public.json'}
            private=paths.home/'private/personas'
            if (private/'registry.json').exists():
                registry=read_json(private/'registry.json');entries['private/personas/registry.json']=private/'registry.json'
                for record in registry['records']:
                    ident=record['id']
                    if Path(ident).name!=ident:raise RuntimeErrorBase('migration_private_path_invalid')
                    for file in PRIVATE_FILES:
                        path=private/ident/file
                        from .private_personas import safe
                        safe(path,paths.home)
                        if not path.is_file():raise RuntimeErrorBase('migration_private_file_missing')
                        entries['private/personas/'+ident+'/'+file]=path
            jev=paths.home/'private/knowledge-router/config.json'
            if jev.exists():
                value=read_json(jev);atomic_json(root/'jev-public.json',{k:value.get(k,'') for k in ('base_url','name')});entries['jev-public.json']=root/'jev-public.json'
            manifest={'format':FORMAT,'version':1,'schema_version':version,'created_at':datetime.now(timezone.utc).isoformat(),'counts':counts,
                'execution_changes':changes,'credentials_included':False,'files':{n:{'sha256':digest(p),'bytes':p.stat().st_size} for n,p in entries.items()}}
            pending=destination.with_suffix(destination.suffix+'.tmp')
            try:
                with zipfile.ZipFile(pending,'w',zipfile.ZIP_DEFLATED) as z:
                    z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
                    for n,p in entries.items():z.write(p,n)
                os.replace(pending,destination)
            finally:pending.unlink(missing_ok=True)
        return {'exported':True,'path':str(destination),'counts':counts,'credentials_included':False,'source_modified':False}

def _manifest(package):
    with zipfile.ZipFile(package) as z:
        value=json.loads(z.read('manifest.json'))
        if value.get('format')!=FORMAT or value.get('version')!=1 or value.get('credentials_included') is not False:raise RuntimeErrorBase('migration_package_invalid')
        from .storage import SCHEMA_VERSION
        if value.get('schema_version')!=SCHEMA_VERSION:raise RuntimeErrorBase('migration_schema_incompatible')
        names=z.namelist()
        if len(names)!=len({n.casefold() for n in names}) or set(names)!={'manifest.json',*value['files']}:raise RuntimeErrorBase('migration_package_invalid')
        if not {'database.sqlite3','config-public.json'}<=set(value['files']):raise RuntimeErrorBase('migration_package_invalid')
        for name,entry in value['files'].items():
            path=PurePosixPath(name)
            if path.is_absolute() or str(path)!=name or '..' in path.parts or '\\' in name or ':' in name:raise RuntimeErrorBase('migration_package_path_invalid')
            if name not in ('database.sqlite3','config-public.json','jev-public.json','private/personas/registry.json'):
                if len(path.parts)!=4 or path.parts[:2]!=('private','personas') or path.name not in PRIVATE_FILES:raise RuntimeErrorBase('migration_package_path_invalid')
            info=z.getinfo(name)
            if (info.external_attr>>16)&0o170000==0o120000 or info.file_size!=entry['bytes']:raise RuntimeErrorBase('migration_package_invalid')
            with z.open(name) as stream:
                if hashlib.file_digest(stream,'sha256').hexdigest()!=entry['sha256']:raise RuntimeErrorBase('migration_package_hash_invalid')
    return value

def _target_empty(paths):
    _closed(paths);validate_home(paths.home)
    from .private_personas import safe
    for child in ('config','data/database/runtime.sqlite3','private/personas','private/knowledge-router','runtime','tmp','backups'):safe(paths.home/child,paths.home)
    if paths.home.exists() and any(paths.home.iterdir()) and not (paths.home/'config/workbench.json').is_file() and not (paths.home/'runtime/install-progress.json').is_file():
        raise RuntimeErrorBase('migration_target_not_empty')
    database=paths.home/'data/database/runtime.sqlite3'
    if database.exists():
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            for table in ('personas','events','memories'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() and db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone():raise RuntimeErrorBase('migration_target_not_empty')
    private=paths.home/'private/personas/registry.json'
    if private.exists() and read_json(private).get('records'):raise RuntimeErrorBase('migration_target_not_empty')
    if private.parent.exists() and any(p.name not in ('registry.json','registry.lock','model-grants.json') for p in private.parent.iterdir()):raise RuntimeErrorBase('migration_target_not_empty')

def import_instance(paths,package,*,apply=False):
    if migration_status(paths):raise RuntimeErrorBase('已有迁移记录，请先完成配置或执行迁移回退。')
    package=Path(package).absolute();manifest=_manifest(package);_target_empty(paths)
    preview={'format':FORMAT,'counts':manifest['counts'],'execution_changes':manifest['execution_changes'],'credentials_included':False,'paid_calls':0,'applied':False}
    if not apply:return preview
    from .character_embedding import private_directory
    from .config import ConfigStore
    paths.initialize();private_directory(paths.home,paths.home)
    with _offline(paths):
        required=sum(x['bytes'] for x in manifest['files'].values())*2
        if shutil.disk_usage(paths.home).free<required:raise RuntimeErrorBase('migration_insufficient_space')
        operation=uuid.uuid4().hex;temporary=private_directory(paths.home,paths.home/'tmp')
        config=ConfigStore(paths);config.initialize();target_cfg=config.load()
        previous_db=paths.home/'data/database/runtime.sqlite3'
        with tempfile.TemporaryDirectory(prefix='migration-import-',dir=temporary) as folder:
            root=Path(folder)
            with zipfile.ZipFile(package) as z:
                for name,entry in manifest['files'].items():
                    dest=root.joinpath(*PurePosixPath(name).parts);dest.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(name) as src,dest.open('wb') as dst:shutil.copyfileobj(src,dst)
                    if digest(dest)!=entry['sha256']:raise RuntimeErrorBase('migration_package_hash_invalid')
            # Validate and sanitize the actual database, not the package's claim.
            _sanitize_database(root/'database.sqlite3')
            public=read_json(root/'config-public.json')
            for k,v in _safe_config(public).items():target_cfg[k]=v
            target_cfg['runtime']['components']={'service':True,'astrbot':False,'napcat':False}
            target_cfg['bot_qq']=target_cfg['owner_qq']=''
            ConfigStore.validate(target_cfg)
            installed=[n for n in manifest['files'] if n.startswith('private/personas/')]
            if 'jev-public.json' in manifest['files']:installed.append('private/knowledge-router/config.json')
            marker={'version':1,'migration_id':operation,'instance_id':target_cfg['instance_id'],'state':'installing','resume_background':False,'counts':manifest['counts'],'installed_files':installed}
            backup=paths.home/'backups'/('before-migration-'+operation);backup.mkdir(parents=True)
            shutil.copyfile(config.file,backup/'workbench.json')
            if previous_db.exists():shutil.copyfile(previous_db,backup/'runtime.sqlite3')
            for name in (*installed,'private/personas/model-grants.json'):
                old=paths.home/name
                if old.is_file():
                    saved=backup/name;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(old,saved)
            marker['original_private_files']=[p.relative_to(backup).as_posix() for p in (backup/'private').rglob('*') if p.is_file()] if (backup/'private').exists() else []
            atomic_json(marker_file(paths),marker)
            try:
                if (root/'private/personas').exists():
                    dest=private_directory(paths.home,paths.home/'private/personas');shutil.copytree(root/'private/personas',dest,dirs_exist_ok=True)
                    (dest/'model-grants.json').unlink(missing_ok=True)
                if (root/'jev-public.json').exists():
                    dest=private_directory(paths.home,paths.home/'private/knowledge-router');jev=read_json(root/'jev-public.json')
                    atomic_json(dest/'config.json',{k:jev.get(k,'') for k in ('base_url','name')}|{'encrypted_key':''})
                os.replace(root/'database.sqlite3',previous_db);atomic_json(config.file,target_cfg)
                marker['state']='needs_configuration';atomic_json(marker_file(paths),marker)
            except BaseException:
                _restore_empty_target(paths,marker)
                marker['state']='failed';atomic_json(marker_file(paths),marker);raise
        return preview|{'applied':True,'state':'needs_configuration','instance_identity_recreated':True,'permissions_imported':False}


def _restore_empty_target(paths,value):
    """Restore only files owned by this import; never remove the environment."""
    from .private_personas import safe
    operation=value['migration_id']
    if not isinstance(operation,str) or len(operation)!=32 or any(c not in '0123456789abcdef' for c in operation):raise RuntimeErrorBase('migration_state_unavailable')
    backup=paths.home/'backups'/('before-migration-'+operation)
    safe(backup,paths.home)
    database=paths.home/'data/database/runtime.sqlite3'
    for suffix in ('-wal','-shm'):safe(Path(str(database)+suffix),paths.home).unlink(missing_ok=True)
    if (backup/'runtime.sqlite3').exists():shutil.copyfile(backup/'runtime.sqlite3',database)
    else:database.unlink(missing_ok=True)
    shutil.copyfile(backup/'workbench.json',paths.home/'config/workbench.json')
    names=set(value.get('installed_files',[]))|set(value.get('original_private_files',[]))|{'private/personas/model-grants.json'}
    for name in names:
        part=PurePosixPath(name)
        if part.is_absolute() or '..' in part.parts or '\\' in name or not name.startswith(('private/personas/','private/knowledge-router/')):raise RuntimeErrorBase('migration_state_unavailable')
        dest=safe(paths.home.joinpath(*part.parts),paths.home);old=safe(backup.joinpath(*part.parts),backup)
        if old.is_file():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(old,dest)
        else:dest.unlink(missing_ok=True)
    # Only remove now-empty canonical role folders created by this operation.
    private=paths.home/'private/personas'
    if private.exists():
        for folder in private.iterdir():
            if folder.is_dir() and not any(folder.iterdir()):folder.rmdir()


def rollback_instance(paths,*,apply=False):
    _closed(paths);value=migration_status(paths)
    if not value:raise RuntimeErrorBase('migration_not_pending')
    if value['state']=='active':raise RuntimeErrorBase('已启用的目标可能有新业务，不可执行空实例回退。原机保持不变。')
    if not apply:return {'applied':False,'restore':'pre_import_empty_target','source_modified':False}
    with _offline(paths):
        _restore_empty_target(paths,value)
        atomic_json(paths.home/'backups'/('before-migration-'+value['migration_id'])/'rollback.json',{'restored':True})
        marker_file(paths).unlink()
        return {'applied':True,'restored':True,'source_modified':False}

def activate_instance(paths,*,apply=False,authorize_visible=False,resume_background=False):
    value=migration_status(paths)
    if not value:raise RuntimeErrorBase('migration_not_pending')
    if value['state']=='installing' or value['state']=='failed':raise RuntimeErrorBase('迁移未完整安装，请检查记录或从空目标重新导入。')
    from .config import ConfigStore
    from .storage import Store
    cfg=ConfigStore(paths).load();store=Store(paths)
    with store.connection() as db:pid=store.current(db,required=False)
    result={'applied':False,'state':value['state'],'authorize_current_role_visible_only':authorize_visible,'resume_background':resume_background,'permissions_inherited':False}
    if not apply:return result
    from urllib.parse import urlsplit
    main=cfg['model'];host=urlsplit(main['base_url']).hostname
    if not main['base_url'] or not main['name'] or host not in ('localhost','127.0.0.1','::1') and not main['encrypted_key']:raise RuntimeErrorBase('请先配置新机主模型和密钥。')
    from .private_personas import PrivatePersonas,provider_identity
    private=PrivatePersonas(paths)
    if pid and private.protected(pid):
        if not authorize_visible:raise RuntimeErrorBase('请确认当前角色可见资料的模型使用权限。')
        private.authorize_main(store,pid,provider_identity(main),'migration-main-'+value['migration_id']+'-'+pid)
    if cfg['character_selector']['enabled']:
        selector=cfg['character_selector']
        if not selector['base_url'] or not selector['name'] or not selector['encrypted_key']:raise RuntimeErrorBase('请先配置新机候选选择模型和密钥。')
        if pid and private.protected(pid):private.authorize_model(store,pid,'selector',provider_identity(selector),'migration-selector-'+value['migration_id']+'-'+pid)
    if resume_background and pid and private.protected(pid):
        model=main if cfg['organization']['reuse_chat'] else cfg['organization']['model']
        if not model['base_url'] or not model['name'] or not model['encrypted_key']:raise RuntimeErrorBase('请先配置后台整理模型和密钥。')
        private.authorize_model(store,pid,'organization',provider_identity(model),'migration-organization-'+value['migration_id']+'-'+pid)
    with store.connection() as db:
        from .knowledge_service import read,POLICY
        knowledge=bool(pid and read(db,'policy.'+pid,POLICY)['data']['enabled'])
    if knowledge:
        from .knowledge_routing import configured_model
        jev=configured_model(paths)
        if private.protected(pid):private.authorize_model(store,pid,'knowledge_router',provider_identity(jev),'migration-jev-'+value['migration_id']+'-'+pid)
    value.update(state='active',resume_background=bool(resume_background));atomic_json(marker_file(paths),value)
    return result|{'applied':True,'state':'active','other_private_roles_require_own_grants':True,'old_incomplete_work':'paused_issue_new_request_after_review','proactive_rules':'disabled'}
