"""Artificial application lifecycle, automatic billing and delivery fault checks."""
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from tests import test_m4 as fixtures
from persona_runtime.app_processes import AppProcesses
from persona_runtime.config import atomic_json
from persona_runtime.delivery import DeliveryService
from persona_runtime.instance import current_process_identity
from persona_runtime.models import RuntimeErrorBase,identifier
from persona_runtime.organization import encoded
from persona_runtime.storage import Store


class Fake:
    gate=None
    def __init__(self):self.calls=[];self.reply='记得带上雨伞。'
    async def complete(self,model,messages):
        self.calls.append((model,messages))
        if self.gate:await self.gate.wait()
        return self.reply,{'total_tokens':7}


class M8ETests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    asyncTearDown=fixtures.M4Tests.asyncTearDown

    def prepare(self):
        self.cfg.update({'owner_qq':'123','bot_qq':'456'})
        raw=self.cfg.load();raw['runtime']['qq_persona_id']=self.persona['id'];atomic_json(self.cfg.file,raw)
        self.now=int(time.time())+5;self.pid=self.persona['id'];self.proactive=self.org.proactive
        self.proactive.clock=lambda:self.now
        atomic_json(self.paths.home/'runtime/console.json',{'state':'active','token':identifier(),
            'instance_id':self.cfg.load()['instance_id'],'process':current_process_identity(),
            'at':datetime.fromtimestamp(self.now-60,timezone.utc).isoformat()})
        self.policy=self.proactive.update(self.pid,{'enabled':True,'quiet_start':0,'quiet_end':0},0,self.pid,'enable')
        self.model=Fake();self.org.client=self.model

    def rule(self,mode='model',**updates):
        data={'enabled':True,'kind':'once','mode':mode,'content':'提醒带伞，不增加其他安排。',
            'at':datetime.fromtimestamp(self.now,timezone.utc).isoformat(),'time':'09:00'}|updates
        return self.proactive.update_rule(self.pid,data,0,self.pid,identifier())

    async def test_default_off_and_no_fake_turn_or_manual_waiver(self):
        self.assertFalse(self.org.proactive.current()['data']['enabled'])
        self.prepare();rule=self.rule();self.proactive.tick(self.now)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM turns').fetchone()[0],0)
            t=dict(db.execute('SELECT * FROM organization_tasks').fetchone())
            self.assertEqual(t['automatic'],3);self.assertIsNone(t['session_id'])
            self.assertNotIn('manual_quota_request',json.loads(t['payload_json']))
        self.assertTrue(await self.org.run_next())
        with self.store.connection() as db:
            task=db.execute('SELECT * FROM organization_tasks').fetchone();self.assertEqual(task['status'],'completed')
            batch=db.execute('SELECT * FROM reply_batches').fetchone();self.assertIsNone(batch['turn_id']);self.assertIsNone(batch['attempt_id'])
            self.assertIsNotNone(batch['outbound_id']);self.assertEqual(db.execute('SELECT count(*) FROM memories').fetchone()[0],0)
            manifest=json.loads(db.execute('SELECT manifest_json FROM context_manifests').fetchone()[0])
            self.assertEqual(manifest['purpose'],'scheduled_reply')
            self.assertEqual(db.execute("SELECT count(*) FROM organization_reservations WHERE state='consumed'").fetchone()[0],1)
        self.proactive.tick(self.now);self.assertFalse(await self.org.run_next());self.assertEqual(len(self.model.calls),1)

    async def test_pause_after_receipt_stops_publication_without_new_charge(self):
        self.prepare();self.rule();self.proactive.tick(self.now)
        original=self.org.proactive.publish
        def pause(db,task,payload,reply):
            raise RuntimeErrorBase('proactive_authorization_changed')
        with patch.object(self.proactive,'publish',side_effect=pause):await self.org.run_next()
        self.assertEqual(len(self.model.calls),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reply_batches').fetchone()[0],0)
        self.proactive.tick(self.now);self.assertFalse(await self.org.run_next());self.assertEqual(len(self.model.calls),1)

    async def test_invalid_model_output_is_retained_without_regeneration(self):
        self.prepare();self.model.reply=' ';self.rule();self.proactive.tick(self.now)
        await self.org.run_next();self.proactive.tick(self.now);await self.org.run_next()
        self.assertEqual(len(self.model.calls),1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reply_batches').fetchone()[0],0)

    async def test_automatic_quota_shared_and_manual_probe_exempt(self):
        self.prepare();self.cfg.update({'organization':{'hourly_limit':1}});self.rule();self.proactive.tick(self.now)
        await self.org.run_next()
        self.rule(at=datetime.fromtimestamp(self.now+3601,timezone.utc).isoformat());self.now+=3601
        self.proactive.tick(self.now);self.assertFalse(await self.org.run_next())
        with self.store.connection() as db:self.assertEqual(db.execute("SELECT count(*) FROM organization_tasks WHERE status='waiting_quota'").fetchone()[0],1)
        self.org.test_model('manual-probe');await self.org.run_next();self.assertEqual(len(self.model.calls),2)

    async def test_fixed_outbound_permission_and_expiry_do_not_block_next_stream_work(self):
        self.prepare();self.rule(mode='fixed');self.proactive.tick(self.now)
        delivery=DeliveryService(self.store)
        with self.store.connection() as db:bid=db.execute('SELECT id FROM reply_batches').fetchone()[0]
        view=delivery.by_batch(bid);part=view['parts'][0]
        with patch('persona_runtime.delivery.time.time',return_value=self.now+601):
            result=delivery.claim(view['id'],part['id'],view['revision'],identifier())
        self.assertFalse(result['claimed']);self.assertEqual(result['state'],'skipped');self.assertFalse(self.model.calls)

    async def test_closed_period_is_no_action_and_rule_edit_never_replays(self):
        self.prepare();rule=self.rule(mode='fixed')
        lease=json.loads((self.paths.home/'runtime/console.json').read_text());lease['at']=datetime.fromtimestamp(self.now+1,timezone.utc).isoformat()
        atomic_json(self.paths.home/'runtime/console.json',lease);self.proactive.tick(self.now+2)
        self.assertEqual(self.proactive.occurrences()[0]['state'],'NO_ACTION')
        self.proactive.update_rule(self.pid,rule['data']|{'content':'变更文本'},1,self.pid,identifier(),rule['rule_id'])
        self.proactive.tick(self.now+3);self.assertEqual(len(self.proactive.occurrences()),1)

    async def test_live_sender_survives_service_recovery_then_unknown_isolated(self):
        self.prepare();self.rule(mode='fixed',content='甲'*1000);self.proactive.tick(self.now)
        delivery=DeliveryService(self.store)
        with self.store.connection() as db:bid=db.execute('SELECT id FROM reply_batches').fetchone()[0]
        view=delivery.by_batch(bid);part=view['parts'][0]
        sender={'process':current_process_identity(),'generation':identifier()}
        with patch.object(delivery,'validate_sender'):
            claim=delivery.claim(view['id'],part['id'],view['revision'],identifier(),sender=sender)
        delivery.recover();self.assertEqual(delivery.get(view['id'])['parts'][0]['state'],'sending')
        delivery.result(view['id'],part['id'],claim['claim_token'],'uncertain',claim['part_revision'],identifier(),transport_done=True)
        delivery.reconcile_unknown(time.time()+121)
        final=delivery.get(view['id']);self.assertTrue(all(p['state']=='skipped' for p in final['parts']))
        late=delivery.result(view['id'],part['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),platform_message_id='late')
        self.assertTrue(late['late_evidence']);self.assertEqual(delivery.get(view['id'])['parts'][0]['state'],'skipped')

    async def test_unknown_sender_cannot_take_send_authority(self):
        self.prepare();self.rule(mode='fixed');self.proactive.tick(self.now)
        delivery=DeliveryService(self.store)
        with self.store.connection() as db:bid=db.execute('SELECT id FROM reply_batches').fetchone()[0]
        view=delivery.by_batch(bid)
        with self.assertRaisesRegex(RuntimeErrorBase,'sender_not_current'):
            delivery.claim(view['id'],view['parts'][0]['id'],view['revision'],identifier(),sender={'process':current_process_identity()})
        self.assertEqual(delivery.get(view['id'])['parts'][0]['state'],'pending')

    async def test_export_has_no_executable_sender_and_import_is_disabled(self):
        self.prepare();self.rule(mode='fixed');self.proactive.tick(self.now)
        exported=self.service.export_persona();package=json.loads(Path(exported['path']).read_text(encoding='utf-8'))
        self.assertEqual(package['format_version'],20);self.assertNotIn('sender_json',encoded(package));self.assertNotIn('console_token',encoded(package))
        self.assertTrue(all(not json.loads(r['data_json'])['enabled'] for r in package['proactive_rules']))

    async def test_revocation_during_call_keeps_receipt_and_usage_without_publication(self):
        self.prepare();self.rule();self.proactive.tick(self.now);self.model.gate=asyncio.Event()
        running=asyncio.create_task(self.org.run_next())
        for _ in range(100):
            if self.model.calls:break
            await asyncio.sleep(.01)
        self.assertEqual(len(self.model.calls),1)
        self.proactive.update(self.pid,{'enabled':False},self.policy['revision'],self.pid,'revoke')
        self.model.gate.set();await running
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_receipts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reply_batches').fetchone()[0],0)
            self.assertIsNotNone(db.execute('SELECT usage_json FROM organization_attempts').fetchone()[0])

    async def test_cancel_queued_and_running_preserves_dispatch_facts(self):
        self.prepare();self.rule();self.proactive.tick(self.now)
        task=self.org.list()[0];self.org.cancel(task['id']);self.assertFalse(await self.org.run_next());self.assertFalse(self.model.calls)
        self.now+=3601;self.rule();self.proactive.tick(self.now);self.model.gate=asyncio.Event()
        running=asyncio.create_task(self.org.run_next())
        for _ in range(100):
            if self.model.calls:break
            await asyncio.sleep(.01)
        active=next(t for t in self.org.list() if t['status']=='running');self.org.cancel(active['id'])
        await running
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reply_batches').fetchone()[0],0)
        self.proactive.tick(self.now);self.assertFalse(await self.org.run_next());self.assertEqual(len(self.model.calls),1)

    async def test_live_unsettled_sender_not_released_by_timeout(self):
        self.prepare();self.rule(mode='fixed',content='甲'*1000);self.proactive.tick(self.now)
        delivery=DeliveryService(self.store)
        with self.store.connection() as db:bid=db.execute('SELECT id FROM reply_batches').fetchone()[0]
        view=delivery.by_batch(bid);part=view['parts'][0]
        with patch.object(delivery,'validate_sender'):
            claim=delivery.claim(view['id'],part['id'],view['revision'],identifier(),sender={'process':current_process_identity()})
        delivery.result(view['id'],part['id'],claim['claim_token'],'uncertain',claim['part_revision'],identifier(),transport_done=False)
        delivery.reconcile_unknown(time.time()+1000)
        self.assertEqual(delivery.get(view['id'])['parts'][0]['state'],'uncertain')
        late=delivery.result(view['id'],part['id'],claim['claim_token'],'sent',claim['part_revision'],identifier(),platform_message_id='proved-late',transport_done=True)
        self.assertEqual(late['state'],'sent');self.assertEqual(delivery.get(view['id'])['parts'][1]['state'],'pending')

    async def test_runtime_operation_recovers_config_write_failure_idempotently(self):
        from persona_runtime import runtime_control
        cfg=self.cfg.load();revision=cfg['runtime']['revision'];instance=cfg['instance_id']
        original=runtime_control.atomic_json
        def fail(path,value):
            if path==self.cfg.file:raise OSError('injected config write failure')
            return original(path,value)
        with patch.object(runtime_control,'atomic_json',side_effect=fail):
            with self.assertRaises(OSError):runtime_control.update(self.paths,{'service':False},revision,instance,'same-op')
        result=runtime_control.update(self.paths,{'service':False},revision,instance,'same-op')
        self.assertFalse(result['components']['service'])
        self.assertEqual(runtime_control.update(self.paths,{'service':False},revision,instance,'same-op'),result)
        with self.assertRaisesRegex(RuntimeErrorBase,'operation_conflict'):runtime_control.update(self.paths,{'service':True},revision,instance,'same-op')

    async def test_config2_upgrade_preserves_limits_and_missing_v3_runtime_fails_closed(self):
        raw=self.cfg.load();raw['version']=2;raw.pop('runtime');raw['model']['hourly_limit']=0
        atomic_json(self.cfg.file,raw);self.cfg.initialize()
        upgraded=self.cfg.load();self.assertEqual(upgraded['version'],3);self.assertEqual(upgraded['model']['hourly_limit'],0)
        self.assertEqual(upgraded['organization']['hourly_limit'],raw['organization']['hourly_limit'])
        backups=list((self.paths.home/'backups').glob('workbench-before-v3-*.dpapi.json'));self.assertEqual(len(backups),1)
        self.cfg.initialize();self.assertEqual(len(list((self.paths.home/'backups').glob('workbench-before-v3-*.dpapi.json'))),1)
        upgraded.pop('runtime');atomic_json(self.cfg.file,upgraded)
        with self.assertRaisesRegex(RuntimeErrorBase,'runtime settings'):self.cfg.load()

    async def test_storage_pause_and_safe_probe_resume(self):
        from persona_runtime.maintenance import mark_write_fault,require_writable_storage,inspect_disk
        directory=self.paths.home/'runtime';mark_write_fault(self.paths,directory)
        with self.assertRaisesRegex(RuntimeErrorBase,'storage_write_fault'):require_writable_storage(self.paths)
        self.assertFalse(inspect_disk(self.paths)['blocked']);require_writable_storage(self.paths)
        with patch('persona_runtime.maintenance.shutil.disk_usage',return_value=__import__('types').SimpleNamespace(free=1024)):
            with self.assertRaisesRegex(RuntimeErrorBase,'storage_low'):require_writable_storage(self.paths)

    async def test_backup_retains_seven_owned_files_and_preserves_manual(self):
        from persona_runtime.maintenance import automatic_backup
        manual=self.paths.home/'backups/manual.sqlite3';manual.write_bytes(b'manual backup sentinel')
        for i in range(9):automatic_backup(self.paths,1000000+i*86401)
        root=self.paths.home/'backups/workbench-automatic'
        self.assertEqual(len(list(root.glob('*.sqlite3'))),7);self.assertEqual(manual.read_bytes(),b'manual backup sentinel')
        before=len(list(root.glob('*.sqlite3')));automatic_backup(self.paths,1000000+8*86401+1)
        self.assertEqual(len(list(root.glob('*.sqlite3'))),before)

    async def test_quiet_hours_and_role_switch_do_not_drift_scope(self):
        from persona_runtime.proactive import ProactiveService,LOCAL
        self.assertTrue(ProactiveService.quiet({'quiet_start':23,'quiet_end':8},datetime(2026,11,9,0,tzinfo=LOCAL).timestamp()))
        self.assertFalse(ProactiveService.quiet({'quiet_start':23,'quiet_end':8},datetime(2026,11,9,12,tzinfo=LOCAL).timestamp()))
        self.prepare();self.rule();self.proactive.tick(self.now)
        from persona_runtime.models import generic_persona
        second=self.service.create_persona(generic_persona());self.service.use_persona(second['id'])
        await self.org.run_next()
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT persona_id FROM reply_batches').fetchone()[0],self.pid)

    async def test_native_identity_retains_full_filetime_and_refuses_reused_pid(self):
        from persona_runtime.lifecycle import process_info,same_process
        record=process_info(__import__('os').getpid());self.assertTrue(same_process(record))
        changed=dict(record,native=record['native']|{'created':record['native']['created']+1})
        self.assertFalse(same_process(changed))

    def transport_fixture(self):
        self.prepare();self.rule(mode='fixed',content='固定传输内容。'*90);self.proactive.tick(self.now)
        delivery=DeliveryService(self.store)
        with self.store.connection() as db:bid=db.execute('SELECT id FROM reply_batches').fetchone()[0]
        view=delivery.by_batch(bid)
        from persona_runtime.qq_bridge import QQBridge
        bridge=QQBridge(self.paths)
        async def request(method,path,value=None):
            part_id=path.split('/')[-2]
            if path.endswith('/claim'):
                with patch.object(delivery,'validate_sender'):
                    return delivery.claim(view['id'],part_id,value['expected_revision'],value['operation_id'],capabilities=value['capabilities'],sender=value['sender'])
            if path.endswith('/result'):
                return delivery.result(view['id'],part_id,value['claim_token'],value['state'],value['expected_revision'],value['operation_id'],
                    platform_message_id=value.get('platform_message_id'),error=value.get('error'),transport_done=value.get('transport_done',False))
            if method=='GET':return delivery.get(view['id'])
            raise AssertionError('Unexpected transport fixture action')
        bridge.request=request
        return bridge,delivery,view,request

    async def test_durable_platform_receipt_replays_ack_without_resend(self):
        bridge,delivery,view,request=self.transport_fixture();sent=[];lost=False
        async def send(text):sent.append(text);return {'message_id':str(len(sent))}
        async def lose(method,path,value=None):
            nonlocal lost
            result=await request(method,path,value)
            if path.endswith('/result') and not lost:lost=True;raise OSError('injected ack lost')
            return result
        bridge.request=lose
        with self.assertRaises(OSError):await bridge.deliver_stream(view,send)
        self.assertEqual(len(sent),1);self.assertTrue(list(bridge.receipts.glob('*.json')))
        await bridge.flush_receipts();await bridge.deliver_stream(delivery.get(view['id']),send)
        self.assertEqual(len(sent),len(view['parts']));self.assertTrue(all(p['state']=='sent' for p in delivery.get(view['id'])['parts']))
        self.assertFalse(list(bridge.receipts.glob('*.json')))

    async def test_unsettled_platform_cancellation_stays_blocked_then_reconciles(self):
        bridge,delivery,view,_=self.transport_fixture();bridge.send_timeout=.001;gate=asyncio.Event();calls=[]
        async def send(text):
            calls.append(text)
            try:await gate.wait()
            except asyncio.CancelledError:await gate.wait()
            return {'message_id':'actual-late-response'}
        self.assertFalse(await bridge.deliver_stream(view,send))
        delivery.reconcile_unknown(time.time()+121);self.assertEqual(delivery.get(view['id'])['parts'][0]['state'],'uncertain')
        gate.set()
        for _ in range(100):
            if not bridge.pending_platform:break
            await asyncio.sleep(.01)
        await bridge.flush_receipts();self.assertEqual(len(calls),1)
        self.assertEqual(delivery.get(view['id'])['parts'][0]['state'],'sent')

    async def test_sent_reminder_reference_has_no_fabricated_user_turn(self):
        bridge,delivery,view,_=self.transport_fixture()
        count=0
        async def send(text):
            nonlocal count
            count+=1;return {'message_id':'ref-'+str(count)}
        await bridge.deliver_stream(view,send)
        from persona_runtime.archive import resolve,scope
        with self.store.connection() as db:
            result=resolve(db,scope(self.pid,'qq','456','123'),{'kind':'platform_message','message_id':'ref-1'})
            self.assertEqual(result['status'],'found');self.assertIsNone(result['turn_id']);self.assertIsNotNone(result['outbound_id'])
            self.assertEqual(result['members'],[]);self.assertEqual(db.execute('SELECT count(*) FROM turns').fetchone()[0],0)
            denied=resolve(db,scope(self.pid,'qq','456','999'),{'kind':'platform_message','message_id':'ref-1'})
            self.assertEqual(denied['status'],'not_found')


class ProcessJobTests(unittest.TestCase):
    def test_exit_metadata_storage_failure_still_releases_owned_jobs(self):
        import tempfile
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.config import ConfigStore
        from persona_runtime.lifecycle import ServiceController
        with tempfile.TemporaryDirectory(prefix='m8e-exit-write-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            paths=RuntimePaths(Path(folder));ConfigStore(paths).initialize();controller=ServiceController(paths);controller.start_console()
            with patch('persona_runtime.lifecycle.atomic_json',side_effect=OSError('injected storage failure')):
                result=controller.shutdown()
            self.assertTrue(result['processes_released']);self.assertIn('exit_report_write_failed',result['errors'])
            self.assertIsNone(controller.app_processes);self.assertIsNone(controller.console_lease)

    def test_stale_logout_receipt_cannot_confirm_a_different_console(self):
        import tempfile
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.config import ConfigStore
        from persona_runtime.lifecycle import ServiceController
        with tempfile.TemporaryDirectory(prefix='m8e-stale-logout-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            paths=RuntimePaths(Path(folder));cfg=ConfigStore(paths);value=cfg.initialize();value['bot_qq']='12345';atomic_json(cfg.file,value)
            atomic_json(paths.home/'runtime/console.json',{'state':'closed','token':'actual-prior-console','process':{'pid':999999999,'created':0}})
            atomic_json(paths.home/'runtime/last-exit.json',{'console_token':'stale-console','logout_confirmed':True,'qq_was_running':True})
            controller=ServiceController(paths)
            try:
                lease=controller.start_console();self.assertTrue(lease['login_confirmation_required'])
                from persona_runtime.runtime_control import current
                self.assertFalse(current(paths)['last_exit']['logout_confirmed'])
            finally:controller.end_console()

    def test_standalone_exit_deadline_survives_shutdown_exception(self):
        import tempfile,os
        with tempfile.TemporaryDirectory(prefix='m8e-exit-deadline-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            code='''
import sys
from pathlib import Path
from persona_runtime.paths import RuntimePaths
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.storage import Store
from persona_runtime import desktop
p=RuntimePaths(Path(sys.argv[1]));c=ConfigStore(p);cfg=c.initialize()
cfg.update(service_port=48765,napcat_port=46399,astrbot_port=46285,onebot_port=46299)
cfg['runtime']['components']={'service':False,'astrbot':False,'napcat':False}
atomic_json(c.file,cfg);Store(p).initialize();desktop.APP_EXIT_TIMEOUT=.5
base=desktop.Workbench
class Hidden(base):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**kwargs);self.withdraw();self.after(250,self.close_window)
  def fail():raise OSError('injected shutdown metadata failure')
  self.controller.shutdown=fail
desktop.Workbench=Hidden;desktop.run(p)
'''
            process=subprocess.Popen([sys.executable,'-B','-c',code,folder],cwd=Path.cwd(),env=dict(os.environ),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                stdout,stderr=process.communicate(timeout=10);self.assertEqual(process.returncode,1)
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=5)

    def test_component_prepare_reuses_own_assets_without_reading_another_project(self):
        import tempfile,os
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.deployment import prepare
        with tempfile.TemporaryDirectory(prefix='m8e-中文 路径-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            paths=RuntimePaths(Path(folder));paths.initialize()
            for name in ('native','node_modules','static','worker'):(paths.home/'components/napcat'/name).mkdir(parents=True)
            interpreter=paths.home/'components/astrbot/.venv/Scripts/python.exe';interpreter.parent.mkdir(parents=True);interpreter.touch()
            with patch.dict(os.environ,{'PCAR_NAPCAT_SOURCE':''}),patch('persona_runtime.deployment.shutil.copytree',side_effect=AssertionError('must reuse owned assets')):
                # Empty environment overrides are treated as absent.
                self.assertTrue(prepare(paths,install=False)['prepared'])

    def test_missing_component_source_requests_explicit_choice(self):
        import tempfile,os
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.deployment import prepare
        with tempfile.TemporaryDirectory(prefix='m8e-missing-source-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            with patch.dict(os.environ,{'PCAR_NAPCAT_SOURCE':''}):
                with self.assertRaisesRegex(RuntimeErrorBase,'PCAR_NAPCAT_SOURCE'):prepare(RuntimePaths(Path(folder)),install=False)

    def test_standalone_close_exits_even_if_extension_stalls_after_mainloop(self):
        import tempfile,os
        with tempfile.TemporaryDirectory(prefix='m8e-app-exit-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            code='''
import sys,time
from pathlib import Path
from persona_runtime.paths import RuntimePaths
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.storage import Store
from persona_runtime import desktop
p=RuntimePaths(Path(sys.argv[1]));c=ConfigStore(p);cfg=c.initialize()
cfg.update(service_port=38765,napcat_port=36399,astrbot_port=36285,onebot_port=36299)
cfg['runtime']['components']={'service':False,'astrbot':False,'napcat':False}
atomic_json(c.file,cfg);Store(p).initialize()
base=desktop.Workbench
class Hidden(base):
 def __init__(self,*args,**kwargs):
  super().__init__(*args,**kwargs);self.withdraw();self.after(250,self.close_window)
 def mainloop(self,*args,**kwargs):
  super().mainloop(*args,**kwargs);time.sleep(30)
desktop.Workbench=Hidden;desktop.run(p)
'''
            process=subprocess.Popen([sys.executable,'-B','-c',code,folder],cwd=Path.cwd(),env=dict(os.environ),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                stdout,stderr=process.communicate(timeout=12)
                self.assertEqual(process.returncode,0,stderr.decode('utf-8',errors='replace')[-500:])
                self.assertEqual(json.loads((Path(folder)/'runtime/console.json').read_text())['state'],'closed')
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=5)

    def test_background_descendants_cannot_promote_their_job_priority(self):
        import tempfile,os
        with tempfile.TemporaryDirectory(prefix='m8e-priority-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            result=Path(folder)/'priority.json'
            probe=('import ctypes,json;from ctypes import wintypes as w;'
                'k=ctypes.WinDLL("kernel32",use_last_error=True);k.GetCurrentProcess.restype=w.HANDLE;'
                'k.SetPriorityClass.argtypes=[w.HANDLE,w.DWORD];k.GetPriorityClass.argtypes=[w.HANDLE];'
                'h=k.GetCurrentProcess();k.SetPriorityClass(h,32768);print(json.dumps({"priority":k.GetPriorityClass(h)}))')
            parent=('import subprocess,sys,json;from pathlib import Path;'
                'r=subprocess.check_output([sys.executable,"-B","-c",sys.argv[2]],creationflags=subprocess.CREATE_NO_WINDOW);'
                'Path(sys.argv[1]).write_bytes(r)')
            jobs=AppProcesses()
            try:
                child=jobs.start('napcat',[sys.executable,'-B','-c',parent,str(result),probe],cwd=Path.cwd(),env=dict(os.environ))
                self.assertEqual(child.wait(timeout=10),0)
                self.assertEqual(json.loads(result.read_text())['priority'],16384)
            finally:jobs.close()

    def test_normal_start_does_not_consume_persisted_fault_restart_budget(self):
        import tempfile,threading
        from types import SimpleNamespace
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.app_runtime import AppRuntime
        with tempfile.TemporaryDirectory(prefix='m8e-cold-start-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            state={'state':'已停止'};now=[1000]
            controller=SimpleNamespace(paths=RuntimePaths(Path(folder)),ensure_binding=lambda:None,closing=threading.Event(),
                config=SimpleNamespace(load=lambda:{'runtime':{'components':{'service':True}}}),health=lambda _:dict(state))
            runtime=AppRuntime(controller,clock=lambda:now[0])
            with patch.object(runtime,'launch_operation',return_value=True) as launch:
                runtime.tick();self.assertEqual(runtime.restarts['service'],[]);self.assertEqual(launch.call_count,1)
                state['state']='运行';now[0]+=5;runtime.tick()
                state['state']='已停止'
                for _ in range(3):now[0]+=5;runtime.tick()
                self.assertEqual(launch.call_count,2);self.assertEqual(len(runtime.restarts['service']),1)
            now[0]+=10;reopened=AppRuntime(controller,clock=lambda:now[0])
            with patch.object(reopened,'launch_operation',return_value=True):reopened.tick()
            self.assertEqual(reopened.restarts,runtime.restarts)

    def test_closed_tray_does_not_retain_tcl_interpreter(self):
        import tkinter as tk,weakref
        from persona_runtime.tray import Tray
        root=tk.Tk();root.withdraw();tray=Tray(root);reference=weakref.ref(root)
        tray.close();root.destroy();del root
        self.assertIsNone(reference())

    def test_tray_native_callbacks_do_not_reenter_tk(self):
        import tkinter as tk,ctypes,threading
        from ctypes import wintypes as w
        from persona_runtime.tray import Tray
        root=tk.Tk();root.withdraw();received=[];tray=None
        root.show_from_tray=lambda:received.append(threading.get_ident())
        try:
            tray=Tray(root);self.assertNotEqual(tray.hwnd,root.winfo_id())
            user=ctypes.WinDLL('user32');user.SendMessageW.argtypes=[w.HWND,w.UINT,w.WPARAM,w.LPARAM]
            user.SendMessageW.restype=ctypes.c_ssize_t
            for _ in range(100):
                user.SendMessageW(tray.hwnd,tray.MESSAGE,0,0x202)
            self.assertFalse(received)
            root.after(200,root.quit);root.mainloop()
            self.assertEqual(len(received),100)
            self.assertTrue(all(thread==threading.get_ident() for thread in received))
            old=tray.hwnd;tray.close();user.IsWindow.argtypes=[w.HWND]
            self.assertFalse(user.IsWindow(old))
        finally:
            if tray:tray.close()
            root.destroy()

    def test_recovery_deadline_and_reason_survive_reopen(self):
        import tempfile
        from types import SimpleNamespace
        from persona_runtime.paths import RuntimePaths
        from persona_runtime.app_runtime import AppRuntime
        with tempfile.TemporaryDirectory(prefix='m8e-recovery-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            paths=RuntimePaths(Path(folder));controller=SimpleNamespace(paths=paths)
            first=AppRuntime(controller);first.restarts={'service':[100,105]};first.next={'service':110};first.last_errors={'service':'TransportUnknown'}
            first.persist({})
            second=AppRuntime(controller)
            self.assertEqual(second.restarts,first.restarts);self.assertEqual(second.next,first.next)
            self.assertEqual(second.last_errors,first.last_errors)

    def test_close_releases_only_owned_children(self):
        jobs=AppProcesses();other=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)'],creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            child=jobs.start('probe',[sys.executable,'-B','-c','import time;time.sleep(30)'],cwd=Path.cwd(),env=dict(__import__('os').environ))
            self.assertIsNone(child.poll());jobs.close();self.assertIsNone(other.poll())
        finally:
            jobs.close();other.terminate();other.wait(timeout=5)

    def test_assignment_failure_never_executes_child(self):
        import tempfile
        root=Path('D:/Persona-Centric Agent Runtime/tmp')
        with tempfile.TemporaryDirectory(prefix='m8e-unassigned-',dir=root) as folder:
            marker=Path(folder)/'should-not-exist'
            jobs=AppProcesses()
            try:
                with patch('persona_runtime.app_processes.Job.assign',side_effect=RuntimeErrorBase('injected assign failure')):
                    with self.assertRaises(RuntimeErrorBase):jobs.start('probe',[sys.executable,'-B','-c','from pathlib import Path;import sys;Path(sys.argv[1]).write_text("bad")',str(marker)],cwd=Path.cwd(),env=dict(__import__('os').environ))
                self.assertFalse(marker.exists())
            finally:jobs.close()

    def test_native_tray_uses_existing_window_and_cleans_icon(self):
        import tkinter as tk
        from persona_runtime.tray import Tray
        root=tk.Tk();root.withdraw();tray=None
        try:
            tray=Tray(root);self.assertTrue(tray.active);tray.close();self.assertFalse(tray.active)
        finally:
            if tray:tray.close()
            root.destroy()

    def test_owner_process_death_reclaims_its_job(self):
        import tempfile,os
        from persona_runtime.instance import process_identity_alive
        with tempfile.TemporaryDirectory(prefix='m8e-owner-death-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            record=Path(folder)/'child.json'
            code=('import os,sys,json,time;from pathlib import Path;from persona_runtime.app_processes import AppProcesses;'
                'from persona_runtime.instance import ProcessIdentityHandle;'
                'j=AppProcesses();c=j.start("probe",[sys.executable,"-B","-c","import time;time.sleep(60)"],cwd=Path.cwd(),env=dict(os.environ));'
                'h=ProcessIdentityHandle(c.pid);Path(sys.argv[1]).write_text(json.dumps(h.identity));time.sleep(60)')
            owner=subprocess.Popen([sys.executable,'-B','-c',code,str(record)],cwd=Path.cwd(),creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline=time.monotonic()+8
                while not record.exists() and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue(record.exists());identity=json.loads(record.read_text());self.assertTrue(process_identity_alive(identity))
                owner.terminate();owner.wait(timeout=5);deadline=time.monotonic()+3
                while process_identity_alive(identity) and time.monotonic()<deadline:time.sleep(.05)
                self.assertFalse(process_identity_alive(identity))
            finally:
                if owner.poll() is None:owner.terminate();owner.wait(timeout=5)
