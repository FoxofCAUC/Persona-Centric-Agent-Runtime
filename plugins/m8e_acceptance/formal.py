"""Gated, stopped-instance M8.E rollout. No model or QQ startup on import."""
from contextlib import closing,ExitStack
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from .runtime import ROOT,EVIDENCE,read,core_hashes
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.models import identifier,timestamp
from persona_runtime.lifecycle import ServiceController,port_open
from persona_runtime.instance import InstanceLock
from persona_runtime.character_embedding import private_directory


def preview():
    paths=RuntimePaths(DEFAULT_HOME);cfg=ConfigStore(paths).load()
    raw=read(ConfigStore(paths).file)
    from persona_runtime.qq_testing import QQTesting
    testing=QQTesting(paths).load(True)
    assert testing['state']=='inactive' and testing['version']==2
    assert (cfg['bot_qq'],cfg['owner_qq'])==(testing['daily_bot'],testing['daily_peer'])
    with closing(sqlite3.connect((DEFAULT_HOME/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        pid=db.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()[0]
        persona=db.execute('SELECT id,name FROM personas WHERE id=?',(pid,)).fetchone()
        tasks=[dict(r) for r in db.execute("SELECT id,persona_id,status,automatic,mode,error_code FROM organization_tasks WHERE status NOT IN ('completed','failed','cancelled','stale')")]
        policies=[dict(r) for r in db.execute('SELECT p.channel,p.revision,p.id,p.mode,p.reply_mode,p.data_json FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE p.persona_id=?',(pid,))]
        for policy in policies:policy['data']=json.loads(policy.pop('data_json'))
        count=db.execute('SELECT count(*) FROM turns WHERE persona_id=? AND status=\'completed\'',(pid,)).fetchone()[0]
        result={'at':timestamp(),'schema':db.execute('PRAGMA user_version').fetchone()[0],'config_version':raw['version'],
            'persona_id':pid,'persona_name':persona['name'],'bot':cfg['bot_qq'],'peer':cfg['owner_qq'],
            'background_limits':{k:cfg['organization'][k] for k in ('hourly_limit','daily_limit')},
            'current_policies':policies,'existing_tasks':tasks,'completed_role_turns':count,'reminders_default':False,
            'new_private_proactive_authorization':False,'component_paths':{'napcat':str(DEFAULT_HOME/'components/napcat'),'astrbot':str(DEFAULT_HOME/'components/astrbot')},
            'backup_path':str(DEFAULT_HOME/'backups'/('M8.E-before-'+cfg['instance_id'])),
            'automatic_components':{'service':True,'astrbot':True,'napcat':True}}
    if cfg['runtime']['qq_persona_id'] not in ('',pid):raise RuntimeError('Formal pinned role differs from selected role')
    atomic_json(EVIDENCE/'formal-preview.json',result)
    return result


def deploy():
    if (EVIDENCE/'formal-deployment.json').exists():raise RuntimeError('Deployment already recorded; inspect instead of repeating')
    for name in ('I1','I2','I3A','I3B','L1'):
        if not read(EVIDENCE/(name+'-result.json'))['passed']:raise RuntimeError('Isolated gate missing: '+name)
    verification=read(EVIDENCE/'formal-copy-migration/report.json')
    assert verification['from']==23 and verification['to']==24 and verification['fault_rollback'] and verification['source_unchanged']
    paths=RuntimePaths(DEFAULT_HOME);controller=ServiceController(paths);cfg=ConfigStore(paths).load()
    with ExitStack() as stack:
        for name in ('desktop','console','service','service.host','astrbot.host','napcat.host'):
            lock=InstanceLock(DEFAULT_HOME/'runtime'/(name+'.lock'));stack.callback(lock.close)
        if controller.has_resident_processes() or any(port_open(cfg[k]) for k in ('service_port','astrbot_port','napcat_port','onebot_port')):
            raise RuntimeError('Formal instance or port is active; do not stop unknown applications')
        p=preview();assert p['schema']==23 and p['config_version']==2
        assert p['background_limits']=={'hourly_limit':60,'daily_limit':240}
        assert all(t['status'] in ('queued','waiting_quota') and t['persona_id']==p['persona_id'] for t in p['existing_tasks'])
        backup=Path(p['backup_path'])
        if backup.exists():raise RuntimeError('Pre-deployment backup exists; inspect interrupted rollout')
        private_directory(DEFAULT_HOME,backup)
        database=DEFAULT_HOME/'data/database/runtime.sqlite3'
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as src:
            for table in ('attempts','organization_attempts'):
                assert not src.execute('SELECT 1 FROM '+table+" WHERE status IN ('running','uncertain') LIMIT 1").fetchone()
            assert not src.execute("SELECT 1 FROM reply_delivery_parts WHERE state IN ('pending','sending','uncertain') LIMIT 1").fetchone()
            with closing(sqlite3.connect(backup/'runtime.sqlite3')) as dest:src.backup(dest)
        shutil.copy2(ConfigStore(paths).file,backup/'workbench.json')
        # Verify the exact protected backup without any model/transport capability.
        isolated=EVIDENCE/'formal-backup-restore/copy';private_directory(DEFAULT_HOME,isolated)
        test_paths=RuntimePaths(isolated);test_paths.initialize()
        from persona_runtime.storage import Store
        restored=Store(test_paths);shutil.copy2(backup/'runtime.sqlite3',restored.path)
        with patch('socket.socket.connect',side_effect=AssertionError('No network in backup verification')):
            restored.initialize()
            with restored.connection() as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
        atomic_json(EVIDENCE/'formal-backup-restore/result.json',{'at':timestamp(),'restored_schema':24,'integrity':'ok','network_calls':0,'backup':str(backup)})
        store=Store(paths);store.initialize()
        config=ConfigStore(paths);cfg=config.initialize()
        cfg['runtime']['qq_persona_id']=p['persona_id'];cfg['runtime']['components']=p['automatic_components'];cfg['runtime']['revision']+=1
        atomic_json(config.file,cfg)
        from persona_runtime.private_personas import guard_cloud_dispatch
        model=cfg['organization']['model']|({k:cfg['model'][k] for k in ('base_url','name','encrypted_key')} if cfg['organization']['reuse_chat'] else {})
        guard_cloud_dispatch(store,p['persona_id'],'main',cfg['model']);guard_cloud_dispatch(store,p['persona_id'],'organization',model)
        from persona_runtime.proactive import ProactiveService
        policy=ProactiveService(SimpleNamespace(store=store,config=config)).current(p['persona_id'])
        assert policy['data']['enabled'] is False
        plugin=DEFAULT_HOME/'components/astrbot/bot/data/plugins/astrbot_plugin_persona_runtime';plugin.mkdir(parents=True,exist_ok=True)
        for source in (ROOT/'integrations/astrbot/astrbot_plugin_persona_runtime').iterdir():
            if source.is_file():shutil.copy2(source,plugin/source.name)
        deployment=read(DEFAULT_HOME/'runtime/deployment.json');deployment['plugin']='0.7.3';atomic_json(DEFAULT_HOME/'runtime/deployment.json',deployment)
        result={'at':timestamp(),'applied':True,'from_schema':23,'to_schema':24,'config_version':3,'bridge':'0.7.3',
            'persona_id':p['persona_id'],'backup':str(backup),'restore_verified':True,'reminders_enabled':False,
            'private_grants_changed':False,'background_limits':p['background_limits'],'old_tasks_preserved':True,'model_calls':0,'core_hashes':core_hashes()}
        atomic_json(EVIDENCE/'formal-deployment.json',result)
        atomic_json(EVIDENCE/'formal-owner.json',{'home':str(DEFAULT_HOME),'instance_id':cfg['instance_id'],'persona_id':p['persona_id'],'core_hashes':core_hashes()})
        print(json.dumps({k:result[k] for k in ('applied','to_schema','config_version','bridge','reminders_enabled','private_grants_changed','model_calls')},ensure_ascii=False))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('preview','deploy'));args=parser.parse_args()
    if args.action=='preview':
        p=preview();print(json.dumps({k:p[k] for k in ('schema','config_version','persona_name','background_limits','completed_role_turns','reminders_default')},ensure_ascii=False))
    else:deploy()
