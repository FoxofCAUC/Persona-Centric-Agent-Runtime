"""Deterministic stop, storage, installation and local-repair fault boundaries."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock,patch

from persona_runtime.app_runtime import AppRuntime
from persona_runtime.automatic_backup import Backups,INTERVAL
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.lifecycle import ServiceController
from persona_runtime.local_repair import LocalRepair,RepairInProgress,require_open,read
from persona_runtime.models import PersonaSpec,RuntimeErrorBase,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests import test_desktop_recovery as desktop


class InstanceTest(unittest.TestCase):
    def setUp(self):
        root=RuntimePaths.from_environment().home/'tmp'
        root.mkdir(parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temp.cleanup)
        self.base=Path(temp.name)
        self.paths=RuntimePaths(self.base/'instance')
        self.config=ConfigStore(self.paths);self.config.initialize()
        self.store=Store(self.paths);self.store.initialize()
        self.service=RuntimeService(self.store)
        pid=self.service.create_persona(PersonaSpec(1,'641','641',['641'],'641',['641'],'641'))['id']
        self.service.use_persona(pid)
        self.sid=self.service.create_session()['id']
        self.first=self.service.append_event(self.sid,'user','642')['id']
        self.controller=ServiceController(self.paths)

    def own(self):
        self.controller.start_console()
        self.addCleanup(self.controller.end_console)


def crash_capture(home,point,now):
    import persona_runtime.automatic_backup as module
    backups=Backups(RuntimePaths(Path(home)))
    if point=='copy':
        def stopping():
            if list(backups.root.glob('.building-*/*.sqlite3')):os._exit(83)
            return False
        backups.create(now,stopping)
    elif point=='manifest':
        original=module.backup_manifest
        def after_manifest(path,**opts):
            original(path,**opts);os._exit(83)
        with patch.object(module,'backup_manifest',after_manifest):backups.create(now)
    elif point=='index':
        with patch.object(backups,'_publish_index',side_effect=lambda _:os._exit(83)):backups.create(now)
    elif point in ('rotation','mid_rotation'):
        original=backups._retire
        def rotate(index):
            if index['retired']:
                if point=='mid_rotation':
                    victim=backups.root/index['retired'][0]['file']
                    unlink=Path.unlink
                    def interrupt(path,*args,**opts):
                        result=unlink(path,*args,**opts)
                        if path==victim:os._exit(83)
                        return result
                    with patch.object(Path,'unlink',interrupt):return original(index)
                os._exit(83)
            original(index)
        with patch.object(backups,'_retire',rotate):backups.create(now)
    raise AssertionError('backup fault was not reached')


class BackupTests(InstanceTest):
    def test_process_loss_at_copy_manifest_index_and_rotation(self):
        backups=Backups(self.paths);now=200000
        backups.create(now)
        self.service.append_event(self.sid,'user','643')
        manual=self.paths.home/'backups'/'manual.sqlite3'
        manual.write_bytes(b'649')
        for point in ('copy','manifest','index','rotation','mid_rotation'):
            if point in ('rotation','mid_rotation'):
                while len(backups.status()['entries'])<7:
                    now+=INTERVAL+1;backups.create(now)
            old=backups.status()['entries'][0]['file']
            now+=INTERVAL+1
            result=subprocess.run([sys.executable,'-B','-m','tests.test_workbench_operations','--crash',
                str(self.paths.home),point,str(now)],timeout=30,capture_output=True)
            self.assertEqual(result.returncode,83,result.stderr)
            before=backups.restore(self.base/('before-'+point))
            if point not in ('rotation','mid_rotation'):self.assertEqual(before['file'],old)
            else:self.assertNotEqual(before['file'],old)
            backups.create(now)
            self.assertFalse((backups.root/'pending.json').exists())
            self.assertFalse(list(backups.root.glob('.building-*')))
            restored=backups.restore(self.base/('after-'+point))
            self.assertEqual(restored['counts']['events'],2)
            with closing(sqlite3.connect(Path(restored['directory'])/'runtime.sqlite3')) as db:
                self.assertEqual(db.execute("SELECT text FROM events WHERE id=?",(self.first,)).fetchone()[0],'642')
        self.assertEqual(len(list(backups.root.glob('*.sqlite3'))),7)
        self.assertEqual(manual.read_bytes(),b'649')

    def test_changed_file_and_legacy_manual_file_survive_rotation(self):
        backups=Backups(self.paths)
        first=backups.create(100000)
        unknown=backups.root/(identifier()+'.sqlite3');unknown.write_bytes(b'650')
        changed=backups.root/first['file'];changed.write_bytes(b'651')
        for n in range(1,9):backups.create(100000+n*(INTERVAL+1))
        self.assertEqual(changed.read_bytes(),b'651')
        self.assertEqual(unknown.read_bytes(),b'650')
        self.assertEqual(backups.status()['deferred_retirement'],1)
        # Earlier versions recorded only names and dates, without a deletion proof.
        old=backups.status()['entries'][-1]['file'];path=backups.root/old
        atomic_json(backups.root/'index.json',{'entries':[{'file':old,'at':2}]})
        for n in range(8):backups.create(100000+(n+10)*(INTERVAL+1))
        self.assertTrue(path.exists())
        self.assertTrue(any(v['file']==old and v['legacy'] for v in backups.status()['entries']))

    def test_exclusively_open_snapshot_defers_rotation_and_allows_restore_fallback(self):
        import _winapi
        backups=Backups(self.paths)
        now=100000;old=backups.create(now)['file']
        now+=INTERVAL+1;latest=backups.create(now)['file']
        def hold(name):
            return _winapi.CreateFile(str(backups.root/name),0x80000000,0,0,3,0x80,0)
        handle=hold(latest)
        try:
            self.assertEqual(backups.restore(self.base/'prior')['file'],old)
            with self.assertRaisesRegex(RuntimeErrorBase,'backup_invalid'):
                backups.restore(self.base/'locked',latest)
        finally:_winapi.CloseHandle(handle)
        handle=hold(old)
        try:
            for _ in range(7):
                now+=INTERVAL+1
                self.assertEqual(backups.create(now)['state'],'published')
            self.assertEqual(backups.status()['deferred_retirement'],1)
            self.assertEqual(len(backups.status()['entries']),7)
        finally:_winapi.CloseHandle(handle)
        backups.create(now)
        self.assertEqual(backups.status()['deferred_retirement'],0)
        self.assertFalse((backups.root/old).exists())

    def test_latest_valid_fallback_explicit_errors_and_independent_copy(self):
        backups=Backups(self.paths)
        a=backups.create(100000)
        self.service.append_event(self.sid,'user','645')
        b=backups.create(100000+INTERVAL+1)
        manifest=backups.root/(b['file']+'.knowledge.json')
        value=json.loads(manifest.read_text());value['required_packages']=[{'sha256':'0'*64,'data_revision':'646'}]
        atomic_json(manifest,value)
        result=backups.restore(self.base/'restored')
        self.assertEqual((result['file'],result['counts']['events'],result['live_counts']['events']),(a['file'],1,2))
        with self.assertRaisesRegex(RuntimeErrorBase,'backup_invalid'):backups.restore(self.base/'bad',b['file'])
        self.assertFalse((self.base/'bad').exists())
        with self.assertRaises(RuntimeErrorBase):backups.restore(self.base/'restored')
        with self.assertRaises(RuntimeErrorBase):backups.restore(self.paths.home/'data/test')
        with self.assertRaises(RuntimeErrorBase):backups.restore(self.base/'invalid','../manual.sqlite3')

    def test_unsigned_entries_and_unverified_operation_preserve_user_files(self):
        backups=Backups(self.paths)
        old=backups.create(100000)['file']
        (backups.root/(old+'.knowledge.json')).unlink()
        manual=backups.root.parent/'manual.sqlite3'
        manual.write_bytes(b'649')
        unsigned={'entries':[{'file':old,'at':1},{'file':'../manual.sqlite3','at':2},655]}
        atomic_json(backups.root/'index.json',unsigned)
        orphan=backups.root/'.building-manual';orphan.mkdir();(orphan/'note').write_bytes(b'648')
        (backups.root/'pending.json').write_bytes(b'647')
        original=backups.restore(self.base/'unsigned',old)
        self.assertEqual(original['counts']['events'],1)
        self.assertEqual(original['required_packages'],[])
        self.assertFalse((backups.root/(old+'.knowledge.json')).exists())
        newer=backups.create(100001)
        self.assertEqual(backups.restore(self.base/'signed')['file'],newer['file'])
        self.assertEqual(backups.status()['unverified_legacy_count'],2)
        self.assertEqual([_read.read_bytes() for _read in backups.root.glob('unverified-operation-*')],[b'647'])
        self.assertEqual([json.loads(p.read_text()) for p in backups.root.glob('index-legacy-*')],[unsigned])
        self.assertEqual((orphan/'note').read_bytes(),b'648')
        self.assertEqual(manual.read_bytes(),b'649')

    def test_rebuild_preserves_unreadable_indices_and_preexisting_snapshots(self):
        backups=Backups(self.paths);first=backups.create(100000)['file']
        snapshot=backups.root/first;original=snapshot.read_bytes()
        (backups.root/'index.json').write_bytes(b'645')
        (backups.root/'index.previous.json').write_bytes(b'646')
        new=backups.create(100001)
        self.assertEqual(backups.restore(self.base/'fresh')['file'],new['file'])
        self.assertEqual(snapshot.read_bytes(),original)
        self.assertEqual({p.read_bytes() for p in backups.root.glob('index-unverified-*')},{b'645',b'646'})

    def test_restore_cli_reads_only_committed_files_while_publisher_holds_lock(self):
        backups=Backups(self.paths);entry=backups.create(100101)
        target=self.base/'CLI 核对副本'
        lock=InstanceLock(backups.root/'backup.lock')
        try:
            result=subprocess.run([sys.executable,'-B','-m','persona_runtime','--json','--home',str(self.paths.home),
                'data','restore-backup','--name',entry['file'],'--destination',str(target)],capture_output=True,timeout=15)
        finally:lock.close()
        self.assertEqual(result.returncode,0,result.stderr)
        summary=json.loads(result.stdout)
        self.assertEqual(summary['counts']['events'],1)
        self.assertEqual(summary['live_counts'],summary['counts'])
        self.assertEqual((target/'runtime.sqlite3').read_bytes(),(backups.root/entry['file']).read_bytes())
        # Read the recovered database with the actual service reader in a separate offline data home.
        import shutil
        reader_paths=RuntimePaths(self.base/'offline-reader')
        reader_paths.initialize()
        restored_store=Store(reader_paths)
        shutil.copyfile(target/'runtime.sqlite3',restored_store.path)
        service=RuntimeService(restored_store)
        self.assertEqual(service.show_session(self.sid)['events'][0]['id'],self.first)
        self.assertEqual(service.show_persona()['spec']['name'],'641')

    def test_damaged_index_recovers_trusted_previous_commit_and_dependency_digest(self):
        with self.store.connection(write=True) as db:
            packages={'1'*64:{'manifest':{'data_revision':'647'}}}
            db.execute("INSERT OR REPLACE INTO knowledge_control VALUES('knowledge.registry',?)",
                       (json.dumps({'revision':1,'data':{'packages':packages}}),))
        backups=Backups(self.paths)
        first=backups.create(100000);backups.create(100000+INTERVAL+1)
        (backups.root/'index.json').write_text('647')
        result=backups.restore(self.base/'verified-previous')
        self.assertEqual(result['file'],first['file'])
        self.assertEqual(result['required_packages'],[{'sha256':'1'*64,'data_revision':'647'}])
        self.assertTrue(backups.status()['index_fallback'])
        backups.create(100000+2*(INTERVAL+1))
        self.assertFalse(backups.status()['index_fallback'])
        saved=list(backups.root.glob('index-unverified-*.json'))
        self.assertEqual([f.read_text() for f in saved],['647'])


class AcceptedRepairTests(unittest.IsolatedAsyncioTestCase):
    setUp=desktop.DesktopRecoveryTests.setUp
    reopen=desktop.DesktopRecoveryTests.reopen
    asyncTearDown=desktop.DesktopRecoveryTests.asyncTearDown

    async def test_storage_failure_before_headers_returns_to_same_turn(self):
        from persona_runtime.chat import ModelError
        from persona_runtime.maintenance import mark_write_fault,inspect_disk
        async def not_sent(*_):
            mark_write_fault(self.paths,self.store.path.parent)
            raise ModelError('storage_write_fault',diagnostics={'request_headers_sent':False,'response_headers_received':False})
        with patch.object(self.model,'complete',side_effect=not_sent):
            turn=(await self.chat.submit('desktop','653',identifier(),self.sid))['id']
            await self.chat.wait()
        self.assertEqual(self.chat.get(turn)['error_code'],'desktop_storage_wait')
        self.reopen();self.chat.recover();self.ingress.recover()
        self.assertFalse(inspect_disk(self.paths)['blocked'])
        self.ingress.network_wake();self.ingress.tick()
        await self.chat.wait()
        self.assertEqual(self.chat.get(turn)['status'],'completed')
        self.assertEqual(self.model.calls,1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts WHERE turn_id=?',(turn,)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM model_call_meter').fetchone()[0],1)


class SupervisionTests(InstanceTest):
    def test_interrupted_installer_releases_its_inflight_child(self):
        from persona_runtime.instance import process_identity_alive
        for operation,script_name in (('sync','sync'),('prepare-model','persona_runtime.py')):
            with self.subTest(operation=operation):
                folder=self.base/operation;folder.mkdir();marker=folder/'step.json'
                (folder/script_name).write_text(
                    "import json,os,sys,time\nfrom pathlib import Path\nsys.path.insert(0,os.environ['PYTHONPATH'])\n"
                    "from persona_runtime.instance import current_process_identity\n"
                    "part=Path('step.tmp');part.write_text(json.dumps(current_process_identity()));part.rename('step.json')\n"
                    "time.sleep(30)\n",encoding='utf8')
                result=subprocess.run([sys.executable,'-B','-m','tests.test_workbench_operations','--step-crash',
                                       str(folder),operation],capture_output=True,timeout=20)
                self.assertEqual(result.returncode,85,result.stderr)
                identity=json.loads(marker.read_text())
                deadline=time.monotonic()+4
                while process_identity_alive(identity) and time.monotonic()<deadline:time.sleep(.05)
                self.assertFalse(process_identity_alive(identity))

    def test_atomic_state_write_retries_a_temporary_windows_reader(self):
        file=self.paths.home/'runtime/wakeup.json'
        atomic_json(file,{'id':'650'})
        held=threading.Event()
        def reader():
            with file.open('rb'):
                held.set();time.sleep(.12)
        worker=threading.Thread(target=reader);worker.start()
        self.assertTrue(held.wait(1))
        atomic_json(file,{'id':'651'})
        worker.join(2)
        self.assertEqual(json.loads(file.read_text()),{'id':'651'})

    def test_unverified_running_process_is_preserved_and_close_is_rechecked(self):
        from persona_runtime.lifecycle import process_info
        self.own();lease=self.controller.console_lease
        outsider=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(20)'],creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            identity=process_info(outsider.pid)
            atomic_json(self.controller.record_path('service'),{'instance_id':lease['instance_id'],'console_token':lease['token'],'host':identity})
            wait=self.controller.shutdown()
            self.assertFalse(wait['processes_released']);self.assertIsNone(outsider.poll())
            self.assertEqual(wait['blocker'],'component_ownership_or_release_unconfirmed')
            self.assertTrue(wait['retry_after'])
            self.assertIsNotNone(self.controller.console_lock)
        finally:
            outsider.terminate();outsider.wait(timeout=4)
        final=self.controller.shutdown()
        self.assertEqual(final['exit_id'],wait['exit_id'])
        self.assertTrue(final['processes_released']);self.assertIsNone(self.controller.console_lock)

    def test_unreadable_repair_receipt_requires_live_verification(self):
        self.own()
        file=self.paths.home/'runtime/local-repair-check_storage.json'
        file.write_bytes(b'659')
        self.assertEqual(read(self.paths)['phase'],'read_only')
        no_action=Mock()
        self.assertTrue(LocalRepair(self.controller).run('check_storage',lambda:True,no_action,lambda:False))
        no_action.assert_not_called();require_open(self.paths)
        preserved=list(file.parent.glob('local-repair-check_storage-unverified-*.json'))
        self.assertEqual([p.read_bytes() for p in preserved],[b'659'])
    def test_restart_budget_survives_unwritable_diagnostics_and_reopen(self):
        self.own()
        now=[1000.];runtime=AppRuntime(self.controller,clock=lambda:now[0])
        runtime.expected_running['service']=True
        with patch.object(self.controller,'health',return_value={'state':'已停止'}),patch.object(runtime,'persist',side_effect=OSError()), \
                patch.object(runtime,'launch_operation',side_effect=lambda role,start,*,fault=False:not fault or runtime.reserve_restart(role)) as start:
            for _ in range(3):runtime.tick(('service',));now[0]+=1
            self.assertEqual(start.call_count,1)
        reopened=AppRuntime(self.controller,clock=lambda:now[0])
        self.assertEqual(reopened.restarts['service'],[1002])
        self.assertEqual(reopened.next['service'],1004)

    def test_storage_repair_waits_for_sqlite_writer_while_backup_is_running(self):
        from persona_runtime import automatic_backup,maintenance
        from persona_runtime.local_repair import RepairInProgress,require_open
        self.own()
        now=[200000.]
        supervisor=AppRuntime(self.controller,clock=lambda:now[0])
        supervisor.repair.on_recovered=None
        entered,release=threading.Event(),threading.Event()
        manifest=automatic_backup.backup_manifest
        def delay(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise AssertionError("backup barrier expired")
            return manifest(*args,**kwargs)
        with patch.object(automatic_backup,'backup_manifest',side_effect=delay):
            supervisor._background('backup',60,lambda:Backups(self.paths).create(now[0]))
            try:
                self.assertTrue(entered.wait(5))
                with closing(sqlite3.connect(self.store.path)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    maintenance.mark_write_fault(self.paths,self.store.path.parent)
                    supervisor._disk()
                    with self.assertRaises(RepairInProgress):require_open(self.paths)
                    self.assertEqual(read(self.paths)['phase'],'read_only')
                    writer.rollback()
                now[0]+=6
                supervisor._disk()
                require_open(self.paths)
                self.assertEqual(read(self.paths)['phase'],'resolved')
            finally:
                release.set()
                supervisor.stop()
                supervisor.join()
        backup=Backups(self.paths).restore(self.base/'restored-writer')
        self.assertEqual(backup['counts']['events'],1)

    def test_service_budget_expiry_after_reopen_admits_one_repair(self):
        self.own()
        now=[1000.]
        before=AppRuntime(self.controller,clock=lambda:now[0])
        for _ in range(5):
            self.assertTrue(before.reserve_restart('service'))
            now[0]+=65
        reopened=AppRuntime(self.controller,clock=lambda:now[0])
        reopened.expected_running['service']=True
        running=[]
        with patch.object(self.controller,'owned',side_effect=lambda role:bool(running)), \
                patch.object(self.controller,'health',side_effect=lambda role:{'state':'运行' if running else '已停止'}), \
                patch.object(self.controller,'start',side_effect=lambda role,**kw:running.append(role)), \
                patch.object(reopened.repair,'on_recovered'):
            for _ in range(3):reopened.tick(('service',))
            self.assertEqual(running,[])
            self.assertEqual(reopened.states['service']['next_check_epoch'],2800.)
            now[0]=2799.
            reopened.tick(('service',))
            self.assertEqual(running,[])
            now[0]=2800.
            reopened.tick(('service',))
            deadline=time.monotonic()+5
            while reopened.operation and time.monotonic()<deadline:time.sleep(.02)
            reopened.tick(('service',))
            self.assertEqual(running,['service'])
            self.assertEqual(read(self.paths,action='restart_service')['phase'],'resolved')

    def test_busy_repair_lock_preserves_service_restart_budget(self):
        self.own();runtime=AppRuntime(self.controller)
        held=InstanceLock(self.paths.home/'runtime/local-repair.lock')
        try:
            with patch.object(self.controller,'start') as start:
                self.assertTrue(runtime.launch_operation('service',True,fault=True))
                deadline=time.monotonic()+3
                while runtime.operation and time.monotonic()<deadline:time.sleep(.01)
                self.assertIsNone(runtime.operation);start.assert_not_called()
                self.assertFalse(runtime.restarts.get('service'))
        finally:held.close()

    def test_foreign_service_listener_is_not_contacted_or_released(self):
        import socket
        from persona_runtime.desktop_client import LocalClient,LocalAPIError
        self.own()
        with socket.socket() as external:
            external.bind(('127.0.0.1',0));external.listen()
            number=external.getsockname()[1];self.config.update({'service_port':number})
            host=LocalClient(self.config,bind_instance=True);host.endpoint_check=self.controller.service_endpoint_state
            with self.assertRaisesRegex(LocalAPIError,'desktop_instance_changed'):host.call('GET','/v1/health')
            external.settimeout(.1)
            with self.assertRaises(TimeoutError):external.accept()
            runtime=AppRuntime(self.controller)
            with patch.object(runtime,'launch_operation') as start:runtime.tick(('service',))
            start.assert_not_called()
            self.assertEqual(read(self.paths)['reason'],'external_owner')
            self.assertTrue(self.controller.shutdown()['processes_released'])
            external.getsockname()

    def test_write_probe_does_not_clear_a_later_fault(self):
        from persona_runtime.maintenance import inspect_disk,mark_write_fault,require_writable_storage
        mark_write_fault(self.paths,self.store.path.parent)
        fsync=os.fsync
        def switched(fd):
            mark_write_fault(self.paths,self.paths.home/'runtime')
            return fsync(fd)
        with patch('os.fsync',side_effect=switched):self.assertTrue(inspect_disk(self.paths)['blocked'])
        with self.assertRaisesRegex(RuntimeErrorBase,'storage_write_fault'):require_writable_storage(self.paths)
        self.assertFalse(inspect_disk(self.paths)['blocked'])
        require_writable_storage(self.paths)
    def test_damaged_storage_latch_rechecks_own_writable_directories(self):
        from persona_runtime.maintenance import inspect_disk,require_writable_storage
        runtime=self.paths.home/'runtime'
        latch=runtime/'storage-fault.json'
        unrelated=self.base/'other';unrelated.mkdir()
        for data in (b'{',b'{"directory":"../other"}'):
            latch.write_bytes(data)
            self.assertFalse(inspect_disk(self.paths)['blocked'])
            require_writable_storage(self.paths)
        self.assertEqual({f.read_bytes() for f in runtime.glob('storage-unverified-*')},
                         {b'{',b'{"directory":"../other"}'})
        self.assertEqual(list(unrelated.iterdir()),[])

    def test_failed_health_and_diagnostics_survive_a_slow_single_backup(self):
        self.own()
        runtime=AppRuntime(self.controller)
        entered=threading.Event();release=threading.Event();attempted=threading.Event()
        self.addCleanup(runtime.join);self.addCleanup(runtime.stop);self.addCleanup(release.set)
        def slow():entered.set();release.wait(4)
        def failed(*args):attempted.set();raise OSError()
        runtime._background('backup',60,slow);self.assertTrue(entered.wait(2))
        other=Mock();runtime._background('backup',60,other)
        with patch.object(self.controller,'health',side_effect=failed),patch.object(runtime,'persist',side_effect=OSError()), \
                patch('persona_runtime.app_runtime.atomic_json',side_effect=OSError()):
            runtime.tick(('service',))
            self.assertTrue(attempted.is_set())
            self.assertIn('service',runtime.last_errors)
        with patch.object(self.controller,'health',return_value={'state':'已停止'}),patch.object(runtime,'launch_operation',return_value=True) as start:
            runtime.tick(('service',))
            self.assertEqual(start.call_count,1)
            runtime.stop();runtime.tick(('service',))
            self.assertEqual(start.call_count,1)
        other.assert_not_called();release.set();self.assertTrue(runtime.join())

    def test_repair_is_single_owner_and_reuses_original_id_after_interruption(self):
        self.own();repair=LocalRepair(self.controller)
        held=threading.Event();release=threading.Event();verified=threading.Event()
        def run():held.set();release.wait(4);verified.set()
        worker=threading.Thread(target=lambda:repair.run('check_storage',verified.is_set,run,lambda:False))
        worker.start()
        try:
            self.assertTrue(held.wait(2));record=read(self.paths)
            self.assertEqual(record['phase'],'executing')
            with self.assertRaises(RepairInProgress):require_open(self.paths)
            duplicate=Mock()
            self.assertFalse(LocalRepair(self.controller).run('check_storage',verified.is_set,duplicate,lambda:False))
            duplicate.assert_not_called()
        finally:release.set();worker.join(5)
        self.assertEqual(read(self.paths)['attempts'],1);require_open(self.paths)
        record['owner']={'pid':0,'created':0};record['console']=identifier()
        atomic_json(self.paths.home/'runtime/local-repair-check_storage.json',record)
        repeated=Mock()
        self.assertTrue(LocalRepair(self.controller).run('check_storage',lambda:True,repeated,lambda:False))
        repeated.assert_not_called();self.assertEqual(read(self.paths)['id'],record['id'])

    def test_failed_attempts_are_bounded_and_retry_survives_reopen(self):
        self.own();now=[1000.]
        repair=LocalRepair(self.controller,clock=lambda:now[0])
        action=Mock(side_effect=OSError())
        for _ in range(5):
            self.assertFalse(repair.run('check_storage',lambda:False,action,lambda:False))
            now[0]+=65
        reopened=LocalRepair(self.controller,clock=lambda:now[0])
        self.assertFalse(reopened.run('check_storage',lambda:False,action,lambda:False))
        self.assertEqual(action.call_count,5)
        self.assertEqual(read(self.paths)['reason'],'cooldown')
        self.assertTrue(reopened.run('check_storage',lambda:True,action,lambda:False))
        require_open(self.paths)

    def test_shutdown_retries_owned_handles_and_duplicate_close_reuses_result(self):
        self.own();jobs=self.controller.app_processes
        original=jobs.close;attempts=[]
        def transient():
            attempts.append(1)
            if len(attempts)==1:raise PermissionError()
            original()
        with patch.object(jobs,'close',transient):
            report=self.controller.shutdown()
            again=self.controller.shutdown()
        self.assertEqual(len(attempts),2)
        self.assertEqual(again['exit_id'],report['exit_id'])
        self.assertTrue(report['processes_released'])
        self.assertEqual([p['phase'] for p in report['phases']],
                         ['stop_admission','draining','logout','checkpoint','settled','releasing','components_released'])
        self.assertEqual(report['outstanding'],{})


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--crash':crash_capture(sys.argv[2],sys.argv[3],float(sys.argv[4]))
    elif len(sys.argv)>1 and sys.argv[1]=='--step-crash':
        from persona_runtime.installation import _run_install_step,SOURCE_ROOT
        folder=Path(sys.argv[2]);marker=folder/'step.json'
        def interrupt():
            while not marker.exists():time.sleep(.01)
            os._exit(85)
        threading.Thread(target=interrupt,daemon=True).start()
        arguments=['sync'] if sys.argv[3]=='sync' else ['-B','-m','persona_runtime','--home',str(folder),'prepare-model']
        _run_install_step([sys.executable,*arguments],folder,os.environ|{'PYTHONPATH':str(SOURCE_ROOT),
                          'PERSONA_RUNTIME_HOME':str(folder)},folder/'step.log',12)
    else:unittest.main()
