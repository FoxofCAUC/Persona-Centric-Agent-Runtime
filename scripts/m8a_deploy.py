"""Explicit M8.A stopped migration and one formal candidates position; no QQ."""
import argparse
from contextlib import ExitStack,closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plugins.m8a_acceptance import runner as acceptance
from scripts.stage7_deploy import idle
from scripts.verify_m761_migration import snapshot
from persona_runtime.character_embedding import private_directory
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import InstanceLock
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import identifier,timestamp
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import DEFAULT_HOME,RuntimePaths
from persona_runtime.private_personas import PrivatePersonas,provider_identity,safe
from persona_runtime.qq_testing import QQTesting
from persona_runtime.storage import Store

read,write_new,require=acceptance.read,acceptance.write_new,acceptance.require


def preview(out):
    out=safe(Path(out).resolve(),DEFAULT_HOME/'exports/acceptance');paths=RuntimePaths(DEFAULT_HOME);store=Store(paths);cfg=ConfigStore(paths).load()
    baseline=read(out/'baseline.json')
    with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
        require(db.execute('PRAGMA user_version').fetchone()[0]==21,'schema_changed')
        pid=Store.current(db);require(pid==baseline['persona_id'],'target_changed');pending=idle(db)
        result={'at':timestamp(),'persona_id':pid,'schema_from':21,'schema_to':22,
            'model':provider_identity(cfg['organization']['model'] if not cfg['organization']['reuse_chat'] else cfg['model']),
            'background_hourly':6,'background_daily':24,'project_hourly_unchanged':cfg['model']['hourly_limit'],
            'daily_restore':cfg['organization']['daily_enabled'],'idle_setting':[list(r) for r in db.execute('SELECT * FROM automation WHERE persona_id=?',(pid,))],
            'policies':[list(r) for r in db.execute('SELECT * FROM interaction_heads ORDER BY persona_id,channel')],
            'old_task_states':[list(r) for r in db.execute('SELECT status,count(*) FROM organization_tasks GROUP BY status')],
            'pending':pending,'old_unknown_replay':False,'scope':'current_role_necessary_visible_only',
            'rollback':'disable_background; database restore only before new business writes'}
    write_new(out/'formal-preview.json',result)
    return {k:result[k] for k in ('schema_from','schema_to','background_hourly','background_daily','project_hourly_unchanged','daily_restore')}


def apply(out,real):
    out=safe(Path(out).resolve(),DEFAULT_HOME/'exports/acceptance');real=safe(Path(real).resolve(),out)
    owner,plan=acceptance.verify(real);proof=read(real/'summary.json')
    require(proof['passed'] and not proof['simulated'] and proof['code_hashes']==acceptance.code_hashes(),'real_gate')
    require(read(real/'openings/2/closed.json')['thread_stopped'],'test_host_running')
    require(read(out/'formal-copy-migration/report.json')['source_unchanged'],'migration_gate')
    require(not (out/'deployment-started.json').exists(),'deployment_already_started')
    p=read(out/'formal-preview.json');paths=RuntimePaths(DEFAULT_HOME);store=Store(paths);cfg=ConfigStore(paths)
    with ExitStack() as stack:
        QQTesting(paths)._stopped(stack)
        with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
            require(db.execute('PRAGMA user_version').fetchone()[0]==21 and Store.current(db)==p['persona_id'],'stale_preview')
            require([list(r) for r in db.execute('SELECT * FROM interaction_heads ORDER BY persona_id,channel')]==p['policies'],'policy_changed')
            idle(db);columns,before=snapshot(db)
            backup=paths.home/'backups'/('m8a-'+identifier());private_directory(paths.home,backup)
            with closing(sqlite3.connect(backup/'before-schema22.sqlite3')) as dest:db.backup(dest)
            shutil.copy2(cfg.file,backup/'config.json')
            grants=paths.home/'private/personas/model-grants.json'
            if grants.exists():shutil.copy2(grants,backup/'model-grants.json')
        write_new(out/'deployment-started.json',{'at':timestamp(),'backup':str(backup),'persona_id':p['persona_id'],'old_tables':before})
        cfg.update({'organization':{'daily_enabled':False,'hourly_limit':6,'daily_limit':24}})
        store.initialize()
        with store.connection() as db:
            require(snapshot(db,columns)[1]==before,'migration_business_changed')
            require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall(),'integrity')
        chat=ChatEngine(store,cfg);org=OrganizationEngine(store,cfg,chat.client,chat)
        require(provider_identity(org.model())==p['model'],'model_changed')
        grant=PrivatePersonas(paths).authorize_model(store,p['persona_id'],'organization',p['model'],'m8a-formal-organization-consent')
        current=org.managed.current_policy();data=dict(current['data'],enabled=True)
        policy=org.managed.update_policy(p['persona_id'],data,current['id'],p['persona_id'],'m8a-formal-policy')
        require(cfg.load()['model']['hourly_limit']==p['project_hourly_unchanged'],'main_quota_changed')
        with store.connection() as db:
            require([list(r) for r in db.execute('SELECT * FROM interaction_heads ORDER BY persona_id,channel')]==p['policies'],'v8_changed')
            require([list(r) for r in db.execute('SELECT * FROM automation WHERE persona_id=?',(p['persona_id'],))]==p['idle_setting'],'idle_changed')
        write_new(out/'deployment-result.json',{'schema':22,'persona_id':p['persona_id'],'policy_id':policy['id'],'grant_kind':grant['kind'],'background_limits':[6,24],'backup':str(backup),'front_policy_unchanged':True,'old_tables_unchanged_at_migration':True,'R6':'pending','services_started':False})
    return {'schema':22,'background_limits':[6,24],'R6':'pending'}


def host(out):
    out=Path(out);paths=RuntimePaths(DEFAULT_HOME);p=read(out/'deployment-result.json');cfg=ConfigStore(paths)
    require(p['schema']==22 and not cfg.load()['organization']['daily_enabled'],'formal_pause_required')
    controller=ServiceController(paths);require(not controller.has_resident_processes(),'resident_hosts')
    from persona_runtime.desktop import Workbench
    with closing(InstanceLock(paths.home/'runtime/desktop.lock')):
        controller.start_console();window=None;closing_window=False
        try:
            window=Workbench(paths,managed_lifecycle=False,controller=controller);window.withdraw()
            def close():
                nonlocal closing_window
                if closing_window:return
                closing_window=True
                window.run_task('停止M8.A验证',{'lifecycle','config'},lambda _:controller.stop_all(include_qq=False),lambda _:window.destroy_console())
            window.protocol('WM_DELETE_WINDOW',close)
            def ready(_):
                window.persona_id=p['persona_id'];window.show_page('organization');window.refresh_organization()
                write_new(out/'formal-host-ready.json',{'at':timestamp(),'pid':os.getpid(),'workbench_constructed':True,'qq_started':False})
            def failed(error):write_new(out/'formal-host-error.json',{'error':str(error)})
            window.after(500,lambda:window.run_task('启动正式整理验证',{'lifecycle','config'},lambda _:controller.start('service'),ready,failure=failed))
            def observe():
                result=out/'R6-result.json'
                if result.exists() and not (out/'workbench-organization.json').exists():
                    r=read(result);rows=window.org_lists['tasks'].get(0,'end')
                    if any(r['task_id'][:8] in row and '完成' in row for row in rows):
                        write_new(out/'workbench-organization.json',{'task_displayed':True,'task_completed':True,'actual_policy_text':window.org_state_text.get(),'hidden_window':True})
                    else:window.refresh_organization()
                if (out/'formal-close.request').exists():close()
                if not window.closed:window.after(500,observe)
            window.after(500,observe);window.mainloop();window.hub.pool.shutdown(wait=True,cancel_futures=True)
        finally:
            controller.stop_all(include_qq=False);controller.end_console()
            # Unknown or failed R6 keeps background closed for inspection.
            passed=(out/'R6-result.json').exists() and read(out/'R6-result.json')['status']=='completed'
            if passed:cfg.update({'organization':{'daily_enabled':read(out/'formal-preview.json')['daily_restore']}})
            write_new(out/'formal-host-closed.json',{'at':timestamp(),'resident':controller.has_resident_processes(),'daily_restored':passed,'workbench_closed':bool(window and window.closed)})


def run(out,task_id=None):
    out=Path(out);paths=RuntimePaths(DEFAULT_HOME);store=Store(paths);cfg=ConfigStore(paths);p=read(out/'deployment-result.json')
    write_new(out/'R6-started.json',{'at':timestamp(),'position':'R6','kind':'candidates','no_automatic_review':True,'receipt_recovery':bool(task_id)})
    with store.connection() as db:
        require(Store.current(db)==p['persona_id'],'target_changed');idle(db)
        from persona_runtime.organization import eligible_turns
        sessions=db.execute("SELECT session_id FROM turns WHERE persona_id=? AND channel='desktop' AND status='completed' ORDER BY rowid DESC",(p['persona_id'],)).fetchall()
        chosen=None
        for (sid,) in sessions:
            turns=eligible_turns(db,sid)
            if turns:chosen=(sid,turns[-1]['ordinal']);break
        require(chosen,'no_valid_formal_source')
        before=db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]
    log=(out/'formal-host.log').open('wb')
    process=subprocess.Popen([sys.executable,'-B',str(Path(__file__)),'host','--out',str(out)],cwd=acceptance.ROOT,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0);log.close()
    try:
        def ready():
            require(process.poll() is None,'formal_host_exit')
            if (out/'formal-host-error.json').exists():raise RuntimeError('formal_host_start_failed')
            return True if (out/'formal-host-ready.json').exists() else None
        acceptance.host.wait_until(ready,180);client=LocalClient(cfg)
        if task_id:
            old=client.manage('organization.show',task_id=task_id)
            require(old['status']=='failed' and old['contract']['phase']=='received' and old['error_code']=='organization_invalid_output','receipt_repair_only')
            write_new(out/'R6-intent.json',{'task_id':task_id,'action':'organization.retry','dispatch_allowed':False,'reason':'validated_empty_result_compatibility'})
            task=client.manage('organization.retry',task_id=task_id)
        else:
            preview=client.manage('organization.preview',session_id=chosen[0],mode='candidates',start_row=chosen[1],end_row=chosen[1])
            # Keep private source in its normal database only, never in the evidence file.
            write_new(out/'R6-intent.json',{'request_id':'m8a-formal-R6','source_fingerprint':preview['fingerprint'],'source_count':len(preview['events']),'persona_id':p['persona_id']})
            task=client.manage('organization.submit',preview=preview,request_id='m8a-formal-R6')
        result=acceptance.wait_task(client,task['id'])
        with store.connection() as db:
            attempts=[dict(r) for r in db.execute('SELECT id,status,usage_json FROM organization_attempts WHERE task_id=?',(task['id'],))]
            require(1<=len(attempts)<=4 and db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]==before+(0 if task_id else len(attempts)),'unexpected_dispatch')
            require(db.execute('SELECT count(*) FROM context_manifests WHERE organization_task_id=?',(task['id'],)).fetchone()[0]==len(attempts),'manifest_count')
            require(db.execute("SELECT count(*) FROM candidates WHERE task_id=? AND status!='pending'",(task['id'],)).fetchone()[0]==0,'candidate_auto_approved')
            pending=db.execute('SELECT count(*) FROM candidates WHERE task_id=?',(task['id'],)).fetchone()[0]
        write_new(out/'R6-result.json',{'status':result['status'],'task_id':task['id'],'contract':result['contract'],'attempts':attempts,'pending_candidates':pending,'auto_approved':0,'source_body_exported':False,'new_calls_in_this_run':0 if task_id else len(attempts),'recovery':'explicit_operator_receipt_repair' if task_id else None})
        acceptance.host.wait_until(lambda:True if (out/'workbench-organization.json').exists() else None,90)
    finally:
        write_new(out/'formal-close.request',{'at':timestamp()});require(process.wait(timeout=180)==0,'formal_host_close')
    return read(out/'R6-result.json')|{'attempts':'recorded_locally','task_id':'recorded_locally'}


def repair_receipt(out):
    out=safe(Path(out).resolve(),DEFAULT_HOME/'exports/acceptance');failure=read(out/'R6-failure.json')
    require(failure['status']=='failed' and failure['error_code']=='organization_invalid_output' and len(failure['receipts'])==1,'known_receipt_only')
    require(not read(out/'formal-host-closed.json')['resident'],'formal_host_running')
    folder=out/'R6-recovery-v1';require(not folder.exists(),'repair_already_started');private_directory(DEFAULT_HOME,folder)
    for name in ('deployment-result.json','formal-preview.json'):shutil.copy2(out/name,folder/name)
    original=read(out/'real-v1/owner.json')['code_hashes'];current=acceptance.code_hashes()
    changed={name:{'executed':old,'repaired':current.get(name)} for name,old in original.items() if current.get(name)!=old}
    write_new(folder/'source-delta.json',{'reason':'narrow empty-result compatibility; original receipt only; no regeneration','changed':changed,'code_hashes':current})
    return run(folder,task_id=failure['task_id'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('preview','apply','host','run','repair_receipt'));p.add_argument('--out',type=Path,required=True);p.add_argument('--real',type=Path);a=p.parse_args()
    if a.action=='host':host(a.out)
    else:print(json.dumps(apply(a.out,a.real) if a.action=='apply' else globals()[a.action](a.out),ensure_ascii=False))
