"""Stopped M8.D rollout and finite, user-operated normal Workbench acceptance."""
import argparse
import asyncio
from contextlib import ExitStack,closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import web
from plugins.m8d_acceptance import runner as acceptance
from scripts.stage7_deploy import idle
from scripts.verify_m761_migration import snapshot
from persona_runtime.character_embedding import private_directory
from persona_runtime.chat import ChatEngine,ChatCompletionsClient,ModelError
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.instance import InstanceLock
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import identifier,timestamp
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import DEFAULT_HOME,RuntimePaths
from persona_runtime.private_personas import guard_cloud_dispatch,provider_identity,safe
from persona_runtime.qq_testing import QQTesting
from persona_runtime.storage import Store

read,write_new,require=acceptance.read,acceptance.write_new,acceptance.require


def preview(out):
    out=safe(Path(out).resolve(),DEFAULT_HOME/'exports/acceptance');paths=RuntimePaths(DEFAULT_HOME);store=Store(paths);cfg=ConfigStore(paths).load();baseline=read(out/'baseline.json')
    with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row;pid=Store.current(db)
        require(db.execute('PRAGMA user_version').fetchone()[0]==22 and pid==baseline['persona_id'],'target_or_schema_changed')
        require(all(Path(p).is_file() and acceptance.sha(p)==h for p,h in baseline['protected'].items()),'baseline_changed')
        pending=idle(db)
        policies=[dict(r) for r in db.execute('SELECT * FROM organization_policies WHERE persona_id=? ORDER BY revision DESC LIMIT 1',(pid,))]
        require(len(policies)==1,'organization_policy_missing')
        policy=json.loads(policies[0]['data_json']);model=cfg['organization']['model'] if not cfg['organization']['reuse_chat'] else cfg['model']
        require(policy['provider']==provider_identity(model),'provider_changed');guard_cloud_dispatch(store,pid,'organization',model)
        result={'at':timestamp(),'persona_id':pid,'from':22,'to':23,'export':18,'old_policy':policies[0],
            'scope':{'channels':policy['channels'],'qq':policy['qq']},'provider':provider_identity(model),'limits':[cfg['organization']['hourly_limit'],cfg['organization']['daily_limit']],
            'front_hourly':cfg['model']['hourly_limit'],'daily_restore':cfg['organization']['daily_enabled'],
            'interaction_heads':[list(r) for r in db.execute('SELECT * FROM interaction_heads ORDER BY persona_id,channel')],
            'old_task_states':[list(r) for r in db.execute('SELECT status,count(*) FROM organization_tasks GROUP BY status')],
            'history_turns':db.execute("SELECT count(*) FROM turns WHERE persona_id=? AND status='completed'",(pid,)).fetchone()[0],
            'max_turn_row':db.execute('SELECT coalesce(max(rowid),0) FROM turns').fetchone()[0],
            'main_before':db.execute('SELECT count(*) FROM attempts').fetchone()[0],'org_before':db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0],
            'pending':pending,'private_grant':'existing_role_specific_organization','initial_backfill':False,'other_roles_unchanged':True,
            'rollback':'pause auto memory; never restore an old database over new business writes'}
    require(result['limits']==[6,24],'quota_changed');write_new(out/'formal-preview.json',result)
    return {k:result[k] for k in ('from','to','limits','history_turns','daily_restore','initial_backfill')}


def apply(out,real):
    out=safe(Path(out).resolve(),DEFAULT_HOME/'exports/acceptance');real=safe(Path(real).resolve(),out)
    owner,plan=acceptance.verify(real);proof=read(real/'summary.json');require(proof['passed'] and not proof['simulated'] and proof['code_hashes']==acceptance.code_hashes(),'real_gate')
    last=max(int(p.name) for p in (real/'openings').iterdir() if p.is_dir())
    require(read(real/'openings'/str(last)/'closed.json')['thread_stopped'],'test_host_running');require(read(out/'formal-copy-migration-v3/report.json')['source_unchanged'],'migration_gate')
    p=read(out/'formal-preview.json');paths=RuntimePaths(DEFAULT_HOME);store=Store(paths);cfg=ConfigStore(paths)
    require(not (out/'deployment-started.json').exists(),'already_started')
    with ExitStack() as stack:
        QQTesting(paths)._stopped(stack)
        with closing(sqlite3.connect(store.path.as_uri()+'?mode=ro',uri=True)) as db:
            require(db.execute('PRAGMA user_version').fetchone()[0]==22 and Store.current(db)==p['persona_id'],'stale_preview')
            idle(db);columns,before=snapshot(db);backup=paths.home/'backups'/('m8d-'+identifier());private_directory(paths.home,backup)
            with closing(sqlite3.connect(backup/'before-schema23.sqlite3')) as dest:db.backup(dest)
            shutil.copy2(cfg.file,backup/'config.dpapi.json')
            grants=paths.home/'private/personas/model-grants.json'
            if grants.exists():shutil.copy2(grants,backup/'model-grants.json')
        # Verify that the actual backup is readable and complete, before upgrade.
        with closing(sqlite3.connect(backup/'before-schema23.sqlite3')) as db:require(snapshot(db,columns)[1]==before and db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','backup_invalid')
        write_new(out/'deployment-started.json',{'at':timestamp(),'backup':str(backup),'real_evidence':str(real),'runtime_hashes':acceptance.code_hashes(),'deployment_source_hash':acceptance.sha(Path(__file__))})
        cfg.update({'organization':{'daily_enabled':False}});store.initialize()
        with store.connection() as db:
            require(snapshot(db,columns)[1]==before and not db.execute('PRAGMA foreign_key_check').fetchall(),'migration_business_changed')
            require([list(r) for r in db.execute('SELECT * FROM interaction_heads ORDER BY persona_id,channel')]==p['interaction_heads'],'v8_changed')
        chat=ChatEngine(store,cfg);org=OrganizationEngine(store,cfg,chat.client,chat);pid=p['persona_id'];guard_cloud_dispatch(store,pid,'organization',org.model())
        current=org.managed.current_policy();require(current['id']==p['old_policy']['id'],'policy_changed')
        policy=org.managed.update_policy(pid,dict(current['data'],enabled=True,memory_automation=True,memory_backfill=False),current['id'],pid,'m8d-formal-enable')
        # Register all historical watermarks locally while backfill is paused.
        org.automatic.tick()
        require(cfg.load()['model']['hourly_limit']==p['front_hourly'],'front_quota_changed')
        write_new(out/'deployment-result.json',{'at':timestamp(),'schema':23,'policy_id':policy['id'],'persona_id':pid,'backfill':False,'daily':False,'coverage':org.automatic.coverage(),'private_grant_unchanged':True,'other_roles_unchanged':True})
    return {'schema':23,'backfill':False,'formal_user_acceptance':'pending'}


class FormalClient:
    def __init__(self,out):self.out=Path(out);self.inner=ChatCompletionsClient()
    async def complete(self,model,messages):
        out=self.out;p=read(out/'formal-preview.json');org=bool(model.get('_json_output'));paths=RuntimePaths(DEFAULT_HOME)
        require(acceptance.code_hashes()==read(out/'deployment-started.json')['runtime_hashes'],'runtime_changed')
        real=Path(read(out/'deployment-started.json')['real_evidence']);oldcalls=[read(f) for f in (real/'calls').glob('*.json')]
        with Store(paths).connection() as db:
            require(Store.current(db)==p['persona_id'],'selection_changed')
            if org:
                used=db.execute('SELECT count(*) FROM organization_attempts').fetchone()[0]-p['org_before'];rows=db.execute("SELECT * FROM organization_tasks WHERE status='running'").fetchall();require(len(rows)==1,'multiple_authorities')
                task=dict(rows[0]);payload=json.loads(task['payload_json']);require(task['persona_id']==p['persona_id'] and payload.get('automatic_memory'),'wrong_background_work')
                require(all(db.execute('SELECT rowid FROM turns WHERE id=?',(b['turn_id'],)).fetchone()[0]>p['max_turn_row'] for b in payload['bindings']),'unregistered_history_dispatch')
                require(used+sum(c['organization'] for c in oldcalls)<=24,'acceptance_daily_budget')
                require(db.execute('SELECT count(DISTINCT task_id) FROM organization_attempts WHERE rowid>?',(p['org_before'],)).fetchone()[0]<=6,'formal_logical_limit');logical=task['id']
            else:
                used=db.execute('SELECT count(*) FROM attempts').fetchone()[0]-p['main_before'];rows=db.execute("SELECT id,persona_id FROM turns WHERE status='running'").fetchall();require(len(rows)==1 and rows[0]['persona_id']==p['persona_id'],'wrong_main_role')
                require(used+sum(not c['organization'] for c in oldcalls)<=8,'acceptance_main_budget');logical=rows[0]['id']
        path=out/'formal-calls'/(identifier()+'.json');row={'at':timestamp(),'organization':org,'logical_id':logical,'status':'dispatching','messages_hash':acceptance.fingerprint(messages)}
        from plugins.m8d_acceptance.budget import record_dispatch
        record_dispatch(out,path,row)
        try:
            reply,usage=await self.inner.complete(model,messages);row.update(status='completed',usage=usage,reply_hash=acceptance.fingerprint(reply));atomic_json(path,row);return reply,usage
        except BaseException as error:
            row.update(status='failed_or_unknown',error=type(error).__name__,usage=getattr(error,'usage',None));atomic_json(path,row);raise


class FormalController:
    def __init__(self):self.notify=lambda text:None
    def owned(self,role):return role=='service'
    def snapshot(self):return {r:{'role':r,'owned':self.owned(r),'state':'运行' if self.owned(r) else '未启动','reason':'正式工作台受管验证；关闭窗口停止所属服务'} for r in ('service','astrbot','napcat')}
    def reject(self,*args,**kwargs):raise RuntimeError('本轮服务随工作台关闭；QQ未启动。')
    start=stop=start_all=stop_all=force_stop_all=force_stop_qq=management_url=reject


def host(out):
    out=Path(out);p=read(out/'deployment-result.json');paths=RuntimePaths(DEFAULT_HOME);cfg=ConfigStore(paths)
    require(p['schema']==23 and not cfg.load()['organization']['daily_enabled'],'formal_not_ready')
    from plugins.m8d_acceptance.budget import wait_for_formal_window
    wait_for_formal_window(out)
    controller=ServiceController(paths);require(not controller.has_resident_processes(),'resident_hosts')
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start();state={}
    from persona_runtime.server import create_app
    async def start():
        app=create_app(paths,FormalClient(out));state['app']=app;runner=web.AppRunner(app,access_log=None);state['runner']=runner;await runner.setup();site=web.TCPSite(runner,'127.0.0.1',cfg.load()['service_port']);await site.start()
    with ExitStack() as stack:
        for name in ('service','desktop'):stack.enter_context(closing(InstanceLock(paths.home/'runtime'/(name+'.lock'))))
        window=None
        try:
            controller.start_console();asyncio.run_coroutine_threadsafe(start(),loop).result(120)
            from persona_runtime.desktop import Workbench
            window=Workbench(paths,managed_lifecycle=False,controller=FormalController());window.persona_id=p['persona_id'];window.show_page('chat');window.refresh_organization()
            def control():
                atomic_json(out/'formal-live.json',{'at':timestamp(),'pid':os.getpid(),'coverage':state['app']['organization'].automatic.coverage(),'workbench_policy_text':window.org_state_text.get()})
                if (out/'formal-close.request').exists():window.close_window()
                elif not window.closed:window.after(1000,control)
            window.after(1000,control);write_new(out/'formal-host-ready.json',{'at':timestamp(),'pid':os.getpid(),'visible_workbench':True,'no_qq':True});window.mainloop();window.hub.pool.shutdown(wait=True,cancel_futures=True)
        finally:
            if state.get('runner'):asyncio.run_coroutine_threadsafe(state['runner'].cleanup(),loop).result(180)
            loop.call_soon_threadsafe(loop.stop);thread.join(15);loop.close();controller.end_console()
    write_new(out/'formal-host-closed.json',{'at':timestamp(),'workbench_closed':bool(window and window.closed),'thread_stopped':not thread.is_alive(),'no_resident_services':not controller.has_resident_processes()})


def finish(out):
    out=Path(out);paths=RuntimePaths(DEFAULT_HOME);p=read(out/'formal-preview.json');require(read(out/'formal-host-closed.json')['no_resident_services'],'host_running')
    require(read(out/'formal-summary.json')['passed'],'formal_not_passed');store=Store(paths);cfg=ConfigStore(paths);chat=ChatEngine(store,cfg);org=OrganizationEngine(store,cfg,chat.client,chat)
    with ExitStack() as stack:
        QQTesting(paths)._stopped(stack)
        current=org.managed.current_policy();org.managed.update_policy(p['persona_id'],dict(current['data'],enabled=True,memory_automation=True,memory_backfill=True),current['id'],p['persona_id'],'m8d-formal-backfill')
        cfg.update({'organization':{'daily_enabled':p['daily_restore']}})
        # Local coverage registration does not execute or charge a task.
        org.automatic.tick();coverage=org.automatic.coverage()
        write_new(out/'formal-finish.json',{'at':timestamp(),'coverage':coverage,'daily_restored':p['daily_restore'],'background_limits':[6,24],'front_unchanged':True,'backfill_registered_not_claimed_complete':True})
    return {'enabled':True,'unregistered_turns':coverage['unregistered_turns'],'backlog':coverage['eligible_turns']-coverage['processed_turns']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('preview','apply','host','finish'));parser.add_argument('--out',type=Path,required=True);parser.add_argument('--real',type=Path);a=parser.parse_args()
    result=apply(a.out,a.real) if a.action=='apply' else globals()[a.action](a.out)
    if result is not None:print(json.dumps(result,ensure_ascii=False))
