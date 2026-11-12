"""Stopped, role-scoped M9.A B rollout; never dispatches models or starts QQ."""
from contextlib import ExitStack,closing,contextmanager
import hashlib,json,shutil,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.character_embedding import private_directory
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.instance import InstanceLock,process_identity_alive
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.private_personas import PrivatePersonas,provider_identity,safe,guard_cloud_dispatch
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.storage import Store,SCHEMA_VERSION
from persona_runtime.models import RuntimeErrorBase,timestamp
from scripts.stage7_deploy import idle
from scripts.verify_m761_migration import snapshot

def require(value,reason):
    if not value:raise RuntimeErrorBase(reason)
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def config_identity(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def expected_config(plan,out,paths):
    if 'config_effective_sha256' in plan:return plan['config_effective_sha256']
    backup=out/'workbench-before.json';require(digest(backup)==plan['config_sha256'],'knowledge_rollout_configuration_backup_changed')
    value=read(backup);value.setdefault('qq_backend','napcat');value.setdefault('snowluma_port',5099)
    ConfigStore(paths).validate(value)
    return config_identity(value)

@contextmanager
def stopped(paths):
    with ExitStack() as locks:
        for name in ('desktop','console','service'):locks.enter_context(closing(InstanceLock(paths.home/'runtime'/(name+'.lock'))))
        marker=paths.home/'runtime/console.json'
        if marker.exists():
            lease=read(marker);require(not(lease.get('state')=='active' and process_identity_alive(lease.get('process'))),'knowledge_rollout_console_running')
        yield

def prepare(home,out):
    paths=RuntimePaths(Path(home).resolve());out=safe(Path(out).resolve(),paths.home/'exports/acceptance')
    require(not out.exists(),'knowledge_rollout_existing_checkpoint')
    paths.initialize();store=Store(paths)
    with stopped(paths),closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;version=db.execute('PRAGMA user_version').fetchone()[0]
        require(version in (24,25,26),'knowledge_rollout_schema_changed');pid=Store.current(db)
        idle(db);columns,before=snapshot(db);cfg=ConfigStore(paths).load();router=configured_model(paths)
        require(router['_routing_gate_valid'],'knowledge_routing_gate_required')
        require(cfg['model']['base_url']=='https://api.deepseek.com' and cfg['model']['encrypted_key'],'web_requires_official_main_configuration')
        private=PrivatePersonas(paths);policy=private.record_for_persona(pid)
        require(policy is None or private.model_authorized(pid,policy,'main',cfg['model']),'private_main_grant_required')
        private_directory(paths.home,out)
        backup=out/'before.sqlite3'
        with closing(sqlite3.connect(backup)) as target:db.backup(target)
        shutil.copy2(ConfigStore(paths).file,out/'workbench-before.json')
        shutil.copy2(paths.home/'private/knowledge-router/config.json',out/'router-before.json')
        plan={'at':timestamp(),'home':str(paths.home),'persona_id':pid,'from_schema':version,'to_schema':SCHEMA_VERSION,
            'backup':str(backup),'backup_sha256':digest(backup),'columns':columns,'before_tables':before,
            'config_sha256':digest(ConfigStore(paths).file),'config_effective_sha256':config_identity(cfg),'router_sha256':digest(paths.home/'private/knowledge-router/config.json'),
            'main_provider':provider_identity(cfg['model']),'router_provider':provider_identity(router),
            'router_name':router['name'],'router_base_url':router['base_url'],'router_scope':'current question and necessary recent dialogue; no whole character package',
            'private_router_grant_required':bool(policy and not private.model_authorized(pid,policy,'knowledge_router',router)),
            'channels':['desktop'],'variant':'web_only','learning_model':'main','daily_scan':True,'discover_public_pages':True,
            'formal_limits':{'hour':cfg['organization']['hourly_limit'],'rolling_day':cfg['organization']['daily_limit']},
            'services_started':False,'model_calls':0,'operation_prefix':'m9a6-'+out.name+'-'+pid}
        atomic_json(out/'plan.json',plan)
    return migrate(paths.home,out)

def migrate(home,out):
    paths=RuntimePaths(Path(home).resolve());out=safe(Path(out).resolve(),paths.home/'exports/acceptance');plan=read(out/'plan.json');store=Store(paths)
    require(plan['home']==str(paths.home) and digest(plan['backup'])==plan['backup_sha256'],'knowledge_rollout_backup_changed')
    with stopped(paths):
        require(config_identity(ConfigStore(paths).load())==expected_config(plan,out,paths),'knowledge_rollout_configuration_changed')
        with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;idle(db);require(Store.current(db)==plan['persona_id'],'persona_selection_conflict')
            require(snapshot(db,plan['columns'])[1]==plan['before_tables'],'knowledge_rollout_baseline_changed')
        store.initialize()
        with store.connection() as db:
            require(snapshot(db,plan['columns'])[1]==plan['before_tables'],'knowledge_rollout_business_changed')
            require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall(),'knowledge_rollout_integrity_failed')
        result={'schema':SCHEMA_VERSION,'old_business_unchanged':True,'integrity':'ok','knowledge_enabled':False,'services_started':False,'model_calls':0,'awaiting_router_consent':plan['private_router_grant_required']}
        atomic_json(out/'migration.json',result)
    return result

def activate(home,out):
    from persona_runtime.chat import ChatEngine
    from persona_runtime.organization import OrganizationEngine
    from persona_runtime.knowledge_service import POLICY,read as control
    paths=RuntimePaths(Path(home).resolve());out=safe(Path(out).resolve(),paths.home/'exports/acceptance');plan=read(out/'plan.json');pid=plan['persona_id']
    require(read(out/'migration.json')['schema']==SCHEMA_VERSION,'knowledge_rollout_migration_required')
    with stopped(paths):
        config=ConfigStore(paths);store=Store(paths)
        require(config_identity(config.load())==expected_config(plan,out,paths) and digest(paths.home/'private/knowledge-router/config.json')==plan['router_sha256'],'knowledge_rollout_configuration_changed')
        with store.connection() as db:idle(db);require(Store.current(db)==pid,'persona_selection_conflict')
        guard_cloud_dispatch(store,pid,'main',config.load()['model']);guard_cloud_dispatch(store,pid,'knowledge_router',configured_model(paths))
        if (out/'activation.json').exists():return read(out/'activation.json')
        chat=ChatEngine(store,config);engine=OrganizationEngine(store,config,None,chat);prefix=plan['operation_prefix']
        try:
            with store.connection() as db:
                unrelated={r[0]:r[1] for r in db.execute('SELECT key,value FROM knowledge_control') if pid not in r[0]}
            def step(name,current,action,change):
                path=out/('activate-'+name+'-input.json')
                if path.exists():args=read(path)
                else:
                    policy=current(pid);args={'persona_id':pid,'data':change(policy['data']),'expected_revision':policy['revision'],'operation_id':prefix+'-'+name}
                    atomic_json(path,args)
                result=action(**args);atomic_json(out/('activate-'+name+'-result.json'),result);return result
            # Freeze arguments before each write. The same operation and revision recover an interrupted response.
            # Reader is enabled last; interrupted configuration cannot leak through a partial route.
            step('web',engine.web.policy,engine.web.update_policy,lambda d:d|{'enabled':True,'topics':{},'discover_public_pages':True,'wikimedia_fallback':True})
            step('route',engine.routing.policy,engine.routing.update_policy,lambda d:d|{'enabled':True,'variant':'web_only'})
            step('processing',engine.processing.policy,engine.processing.update_policy,lambda d:d|{'enabled':True,'channels':['desktop'],'prompt_profile':'question_focused_v6'})
            step('learning',engine.knowledge.policy,engine.knowledge.update_policy,lambda d:engine.knowledge.defaults('main')|{'enabled':True,'daily_scan':True})
            step('reader',chat.knowledge.current_policy,chat.knowledge.update_policy,lambda d:d|{'enabled':True,'channels':['desktop']})
            with store.connection() as db:
                for key,value in unrelated.items():require(db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()[0]==value,'knowledge_rollout_other_role_changed')
                columns={k:v for k,v in plan['columns'].items() if k!='knowledge_control'}
                require(snapshot(db,columns)[1]=={k:plan['before_tables'][k] for k in columns},'knowledge_rollout_business_changed')
                result={'schema':SCHEMA_VERSION,'persona_id':pid,'channels':['desktop'],'variant':'web_only','learning_model':'main',
                    'reader':chat.knowledge.current_policy(pid),'routing':engine.routing.policy(pid),'processing':engine.processing.policy(pid),'maintenance':engine.knowledge.policy(pid),
                    'other_roles_unchanged':True,'old_business_unchanged':True,'formal_limits_unchanged':True,'services_started':False,'model_calls':0}
            atomic_json(out/'activation.json',result)
            return result
        finally:chat.knowledge.close()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','migrate','activate'));parser.add_argument('out',type=Path);parser.add_argument('--home',type=Path,default=DEFAULT_HOME);args=parser.parse_args()
    value=globals()[args.action](args.home,args.out);print(json.dumps({k:v for k,v in value.items() if k not in ('persona_id','reader','routing','processing','maintenance')},ensure_ascii=False))
