"""Portable installation and whole-instance migration, using artificial data only."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
import zipfile

from persona_runtime.config import ConfigStore
from persona_runtime.installation import resolve_home,runtime_python,validate_home,atomic_json,child_environment,register_existing
from persona_runtime.migration import export_instance,import_instance,activate_instance,rollback_instance,migration_pending,background_allowed
from persona_runtime.models import RuntimeErrorBase,generic_persona
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class InstallationTests(unittest.TestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root,prefix='decoupling-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir()
        self.paths=RuntimePaths(self.root/'data with 中文')

    def test_precedence_and_no_machine_fallback(self):
        with self.assertRaisesRegex(ValueError,'尚未选择'):resolve_home(source_root=self.source,environ={})
        self.assertIsNone(resolve_home(source_root=self.source,environ={},required=False))
        atomic_json(self.source/'.pcar-local.json',{'version':1,'runtime_home':str(self.paths.home)})
        self.assertEqual(resolve_home(source_root=self.source,environ={}),self.paths.home)
        self.assertEqual(resolve_home(source_root=self.source,environ={'PERSONA_RUNTIME_HOME':str(self.root/'env')}),self.root/'env')
        self.assertEqual(resolve_home(self.root/'explicit',source_root=self.source,environ={'PERSONA_RUNTIME_HOME':str(self.root/'env')}),self.root/'explicit')

    def test_unsafe_and_overlapping_directories_rejected(self):
        for path in (Path('relative'),Path(self.root.anchor),self.source/'inside',self.source.parent):
            with self.subTest(path=path),self.assertRaises(ValueError):validate_home(path,source_root=self.source)
        self.assertEqual(validate_home(self.paths.home,source_root=self.source),self.paths.home)

    def test_interpreter_scoped_to_selected_instance(self):
        with self.assertRaises(ValueError):runtime_python(self.paths,environ={})
        exe=self.paths.home/'.venv/Scripts/python.exe';exe.parent.mkdir(parents=True);exe.touch()
        self.assertEqual(runtime_python(self.paths,environ={}),exe)
        env=child_environment(self.paths,source_root=self.source)
        for name in ('UV_CACHE_DIR','PIP_CACHE_DIR','HF_HOME','TEMP','UV_PROJECT_ENVIRONMENT'):
            self.assertTrue(Path(env[name]).is_relative_to(self.paths.home))

    def test_registration_changes_only_locator(self):
        cfg=ConfigStore(self.paths);cfg.initialize();before=cfg.file.read_bytes()
        exe=self.paths.home/'.venv/Scripts/python.exe';exe.parent.mkdir(parents=True);exe.touch()
        register_existing(self.paths,source_root=self.source)
        self.assertEqual(before,cfg.file.read_bytes())
        self.assertEqual(resolve_home(source_root=self.source,environ={}),self.paths.home)

    def test_prepare_download_has_no_direct_fallback(self):
        from persona_runtime.installation import provisioning_environment
        with patch('persona_runtime.network_proxy.configured_proxy',return_value=None),self.assertRaisesRegex(ValueError,'不会回退直连'):
            provisioning_environment(self.paths)

    def test_windows_powershell_51_preserves_non_ascii_paths(self):
        helper=Path(__file__).resolve().parents[1]/'scripts/pcar-location.ps1'
        quote=lambda s:"'"+str(s).replace("'","''")+"'"
        script=self.root/'probe.ps1'
        base=Path(sys.base_prefix)/'python.exe'
        script.write_text("$ErrorActionPreference='Stop'\n. "+quote(helper)+"\nSet-PcarProcessEnvironment -RuntimeHome "+quote(self.paths.home)+"\nfunction Get-Command { throw 'Explicit BasePython must not require py or PATH discovery' }\n$p=Get-PcarBasePython -BasePython "+quote(base)+"\n@{python=$p;data=(Get-PcarRuntimeHome)} | ConvertTo-Json -Compress\n",encoding='utf-8-sig')
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],capture_output=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout);self.assertEqual(Path(value['data']),self.paths.home);self.assertEqual(Path(value['python']),base);self.assertTrue(base.is_file())


class MigrationTests(unittest.TestCase):
    def setUp(self):
        InstallationTests.setUp(self)
        self.cfg=ConfigStore(self.paths);self.cfg.initialize()
        self.store=Store(self.paths);self.store.initialize();self.service=RuntimeService(self.store)
        self.pid=self.service.create_persona(generic_persona())['id'];self.service.use_persona(self.pid)
        self.session=self.service.create_session()['id']
        self.event=self.service.append_event(self.session,'user','历史原文含 api_key 和旧路径 D:/example，不能按字符串替换。')
        self.memory=self.service.add_memory('preference','人工偏好保留原状态',self.event['id'])
        self.target=RuntimePaths(self.root/'new data 迁移')

    def package(self):return Path(export_instance(self.paths)['path'])

    def test_roundtrip_preserves_business_and_excludes_secrets(self):
        before=self.store.path.read_bytes();cfg=self.cfg.load();cfg['model']['encrypted_key']='old-machine-dpapi-fixture';atomic_json(self.cfg.file,cfg)
        package=self.package();self.assertEqual(before,self.store.path.read_bytes())
        with zipfile.ZipFile(package) as z:
            self.assertNotIn(b'old-machine-dpapi-fixture',b''.join(z.read(n) for n in z.namelist()))
        preview=import_instance(self.target,package);self.assertFalse(preview['applied']);self.assertFalse(self.target.home.exists())
        result=import_instance(self.target,package,apply=True);self.assertTrue(result['applied'])
        new_cfg=ConfigStore(self.target).load();self.assertNotEqual(new_cfg['instance_id'],cfg['instance_id']);self.assertNotEqual(new_cfg['admin_token'],cfg['admin_token']);self.assertEqual(new_cfg['model']['encrypted_key'],'')
        dest=Store(self.target)
        with dest.connection() as db:
            self.assertEqual(dest.current(db),self.pid)
            self.assertEqual(db.execute('SELECT text FROM events WHERE id=?',(self.event['id'],)).fetchone()[0],self.event['text'])
            self.assertEqual(dict(db.execute('SELECT * FROM memories WHERE id=?',(self.memory['id'],)).fetchone()),self.memory)
        self.assertTrue(migration_pending(self.target));self.assertFalse(background_allowed(self.target))
        with self.assertRaisesRegex(RuntimeErrorBase,'主模型'):activate_instance(self.target,apply=True)
        rollback_instance(self.target,apply=True);self.assertFalse(migration_pending(self.target));self.assertFalse(dest.path.exists())

    def test_nonempty_target_never_merged(self):
        with self.assertRaisesRegex(RuntimeErrorBase,'migration_target_not_empty'):import_instance(self.paths,self.package(),apply=True)

    def test_active_desktop_lock_prevents_export(self):
        from persona_runtime.instance import InstanceLock
        lock=InstanceLock(self.paths.home/'runtime/desktop.lock')
        try:
            with self.assertRaisesRegex(RuntimeErrorBase,'already running'):export_instance(self.paths)
        finally:lock.close()

    def test_sparse_history_row_positions_survive_compaction(self):
        with self.store.connection(write=True) as db:db.execute('UPDATE events SET rowid=101 WHERE id=?',(self.event['id'],))
        import_instance(self.target,self.package(),apply=True)
        with Store(self.target).connection() as db:self.assertEqual(db.execute('SELECT rowid FROM events WHERE id=?',(self.event['id'],)).fetchone()[0],101)

    def test_pending_work_and_embedded_model_snapshots(self):
        from persona_runtime.models import timestamp,LOCAL_USER_ID
        now=timestamp();model=json.dumps({'encrypted_key':'old-dpapi-in-snapshot','name':'fixture'})
        with self.store.connection(write=True) as db:
            for state in ('queued','running','completed','failed'):
                db.execute('INSERT INTO turns(id,source_key,persona_id,session_id,channel,status,model_snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    (state,state,self.pid,self.session,'desktop',state,model,now,now))
            db.execute('INSERT INTO attempts(id,turn_id,started_epoch,status,model_snapshot) VALUES (?,?,?,?,?)',('old-attempt','running',1,'running',model))
            db.execute('INSERT INTO organization_tasks(id,request_id,persona_id,user_id,mode,fingerprint,payload_json,model_snapshot,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('task','task',self.pid,LOCAL_USER_ID,'test','fixture','{}',model,'running',now,now))
            db.execute('INSERT INTO organization_attempts(id,task_id,started_epoch,status,model_snapshot) VALUES (?,?,?,?,?)',('task-attempt','task',1,'running',model))
            db.execute('INSERT INTO knowledge_control(key,value) VALUES (?,?)',('knowledge.test-note',json.dumps({'text':'人工游戏笔记','source':'https://community.fixture.invalid/post'})))
        package=self.package()
        with zipfile.ZipFile(package) as z:self.assertNotIn(b'old-dpapi-in-snapshot',z.read('database.sqlite3'))
        import_instance(self.target,package,apply=True)
        with Store(self.target).connection() as db:
            self.assertEqual(dict(db.execute('SELECT id,status FROM turns')),{'queued':'interrupted','running':'interrupted','completed':'completed','failed':'failed'})
            self.assertEqual(db.execute('SELECT status FROM organization_tasks').fetchone()[0],'failed')
            self.assertEqual(db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.test-note',)).fetchone()[0],json.dumps({'text':'人工游戏笔记','source':'https://community.fixture.invalid/post'}))

    def test_bad_hash_and_traversal_do_not_create_target(self):
        original=self.package()
        for traversal in (False,True):
            bad=self.root/('bad-'+str(traversal)+'.zip')
            with zipfile.ZipFile(original) as src,zipfile.ZipFile(bad,'w') as dst:
                manifest=json.loads(src.read('manifest.json'))
                if traversal:manifest['files']['../escape.json']={'bytes':2,'sha256':hashlib.sha256(b'{}').hexdigest()}
                else:manifest['files']['database.sqlite3']['sha256']='0'*64
                for name in src.namelist():dst.writestr(name,json.dumps(manifest) if name=='manifest.json' else src.read(name))
                if traversal:dst.writestr('../escape.json',b'{}')
            with self.assertRaises(RuntimeErrorBase):import_instance(self.target,bad,apply=True)
            self.assertFalse(self.target.home.exists())

    def test_import_failure_restores_prepared_empty_target(self):
        target_cfg=ConfigStore(self.target);target_cfg.initialize();Store(self.target).initialize()
        previous=target_cfg.file.read_bytes();package=self.package()
        real_replace=os.replace
        def fail_database(src,dst):
            if Path(dst)==Store(self.target).path:raise OSError('artificial install failure')
            return real_replace(src,dst)
        with patch('persona_runtime.migration.os.replace',side_effect=fail_database),self.assertRaises(OSError):import_instance(self.target,package,apply=True)
        self.assertEqual(target_cfg.file.read_bytes(),previous)
        with Store(self.target).connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM personas').fetchone()[0],0)
        rollback_instance(self.target,apply=True);import_instance(self.target,package,apply=True)

    def test_dispatch_cannot_use_process_home_or_pending_migration(self):
        import_instance(self.target,self.package(),apply=True)
        from persona_runtime.generation_retry import _one
        client=type('Client',(),{'complete':AsyncMock(return_value=('reply',None))})()
        with self.assertRaisesRegex(RuntimeErrorBase,'migration_configuration_required'):
            asyncio.run(_one(client,{'timeout':2},[],'provider_timeout',paths=self.target))
        client.complete.assert_not_called()

    def test_pending_service_can_read_but_never_dispatch(self):
        from aiohttp.test_utils import TestClient,TestServer
        from persona_runtime.server import create_app
        import_instance(self.target,self.package(),apply=True)
        provider=type('Client',(),{'complete':AsyncMock()})()
        async def check():
            app=create_app(self.target,provider)
            async with TestClient(TestServer(app)) as client:
                headers={'Authorization':'Bearer '+ConfigStore(self.target).token()}
                read=await client.post('/v1/manage',json={'action':'persona.list','args':{}},headers=headers)
                self.assertEqual(read.status,200)
                send=await client.post('/v1/chat',json={'text':'人工问题','request_id':'pending-migration'},headers=headers)
                self.assertEqual(send.status,400);self.assertEqual((await send.json())['error'],'migration_configuration_required')
                provider.complete.assert_not_called()
        asyncio.run(check())


class PrivateMigrationTests(unittest.TestCase):
    from tests.test_pre_m8 import PrivateTests
    setUp=PrivateTests.setUp

    def test_jev_keeps_its_address_but_needs_new_key_and_grant(self):
        from io import StringIO
        from persona_runtime.installation import configure_model
        from persona_runtime.private_personas import PrivatePersonas,guard_cloud_dispatch
        from persona_runtime.knowledge_routing import configured_model
        record=self.private.import_original(self.store,self.rec['id']);pid=record['persona_id'];self.service.use_persona(pid)
        old={'base_url':'https://jev.fixture.invalid/v1','name':'jev-fixture','encrypted_key':'old-jev-dpapi-fixture'}
        atomic_json(self.paths.home/'private/knowledge-router/config.json',old)
        with self.store.connection(write=True) as db:
            db.execute('INSERT INTO knowledge_control(key,value) VALUES (?,?)',('knowledge.policy.'+pid,json.dumps({'revision':1,'data':{'enabled':True,'channels':['desktop']}})))
        target=RuntimePaths(Path(self.temp.name)/'new-jev-home');package=Path(export_instance(self.paths)['path'])
        with zipfile.ZipFile(package) as z:self.assertNotIn(b'old-jev-dpapi-fixture',b''.join(z.read(n) for n in z.namelist()))
        import_instance(target,package,apply=True)
        new=json.loads((target.home/'private/knowledge-router/config.json').read_text(encoding='utf-8'));self.assertEqual(new,dict(old,encrypted_key=''))
        cfg=ConfigStore(target);value=cfg.load();value['model'].update(base_url='https://main.fixture.invalid/v1',name='main-fixture',encrypted_key='new-main-fixture');atomic_json(cfg.file,value)
        with self.assertRaisesRegex(RuntimeErrorBase,'jev_configuration_required'):activate_instance(target,apply=True,authorize_visible=True)
        with patch('sys.stdin',StringIO('new artificial Jev key\n')):configure_model(target,'jev',key_stdin=True)
        activate_instance(target,apply=True,authorize_visible=True)
        guard_cloud_dispatch(Store(target),pid,'knowledge_router',configured_model(target))
        self.assertNotEqual(configured_model(target)['encrypted_key'],value['model']['encrypted_key'])

    def test_canon_preserved_and_new_grant_bound_to_target(self):
        from persona_runtime.private_personas import PrivatePersonas,guard_cloud_dispatch,provider_identity
        record=self.private.import_original(self.store,self.rec['id']);pid=record['persona_id'];self.service.use_persona(pid)
        source={p.name:p.read_bytes() for p in (self.private.root/self.rec['id']).iterdir() if p.is_file()}
        target=RuntimePaths(Path(self.temp.name)/'target');package=Path(export_instance(self.paths)['path'])
        import_instance(target,package,apply=True);private=PrivatePersonas(target);cfg=ConfigStore(target);model=cfg.load()['model']
        self.assertEqual(source,{p.name:p.read_bytes() for p in (private.root/self.rec['id']).iterdir() if p.is_file()})
        self.assertTrue(private.check(Store(target),self.rec['id'],pid)['original_bytes_unchanged'])
        with self.assertRaisesRegex(RuntimeErrorBase,'migration_configuration_required'):guard_cloud_dispatch(Store(target),pid,'main',model)
        data=cfg.load();data['model'].update(base_url='https://provider.fixture.invalid/v1',name='fixture',encrypted_key='new-machine-fixture');atomic_json(cfg.file,data)
        with self.assertRaisesRegex(RuntimeErrorBase,'可见资料'):activate_instance(target,apply=True)
        activate_instance(target,apply=True,authorize_visible=True);guard_cloud_dispatch(Store(target),pid,'main',cfg.load()['model'])
        other=RuntimePaths(Path(self.temp.name)/'other');shutil.copytree(target.home,other.home)
        with self.assertRaisesRegex(RuntimeErrorBase,'private_cloud_provider_not_authorized'):guard_cloud_dispatch(Store(other),pid,'main',cfg.load()['model'])
        self.assertFalse((target.home/'private/knowledge-router/config.json').exists())


if __name__=='__main__':unittest.main()
