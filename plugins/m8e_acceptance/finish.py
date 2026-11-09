"""Finite read-only N1 observation, then the explicitly planned normal app exit.

No model execution, system scheduling, task continuation, or quota manipulation.
The observer belongs to this acceptance run and never restarts the Workbench.
"""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import psutil

from .runtime import ROOT,EVIDENCE,read,core_hashes
from .processes import owned_processes
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.instance import InstanceLock,current_process_identity,process_identity_alive,ProcessIdentityHandle
from persona_runtime.lifecycle import ServiceController,read_console
from persona_runtime.models import timestamp,RuntimeErrorBase
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME

LOCAL=timezone(timedelta(hours=8))


def snapshot_processes(paths,identity):
    result=[]
    for p in owned_processes(paths,identity):
        try:
            with p.oneshot():
                handle=ProcessIdentityHandle(p.pid)
                try:identity=handle.identity
                finally:handle.close()
                result.append({'identity':identity,'rss_bytes':p.memory_info().rss,'name':p.name()})
        except psutil.NoSuchProcess:continue
        except RuntimeErrorBase:
            # A GUI-owned diagnostic can finish between enumeration and the
            # retained-handle query. Keep live/permission failures visible.
            if not p.is_running():continue
            raise
    return result


def business_audit(paths):
    with closing(sqlite3.connect((paths.home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        pid=read(EVIDENCE/'formal-owner.json')['persona_id']
        unknown=db.execute("SELECT count(*) FROM organization_tasks WHERE status='blocked_unknown'").fetchone()[0]
        unknown+=db.execute("SELECT count(*) FROM reply_delivery_parts WHERE state IN ('sending','uncertain')").fetchone()[0]
        queue=db.execute("SELECT count(*) FROM organization_tasks WHERE status IN ('queued','waiting_quota','running')").fetchone()[0]
        daily=[list(r) for r in db.execute('SELECT id,status,next_epoch,updated_at FROM daily_updates')]
        coverage=[list(r) for r in db.execute('SELECT disposition,count(*) FROM memory_coverage WHERE persona_id=? GROUP BY disposition',(pid,))]
        states=[list(r) for r in db.execute('SELECT status,count(*) FROM organization_tasks GROUP BY status')]
        active=db.execute('SELECT 1 FROM personas p LEFT JOIN persona_lifecycle l ON l.persona_id=p.id WHERE p.id=? AND l.deleted_at IS NULL',(pid,)).fetchone()
    cfg=ConfigStore(paths).load();target=read(EVIDENCE/'formal-preview.json')
    assert active and cfg['runtime']['qq_persona_id']==pid
    assert (cfg['bot_qq'],cfg['owner_qq'])==(target['bot'],target['peer'])
    return {'critical_unknown_count':unknown,'queue_count':queue,'task_states':states,'processing_coverage':coverage,'daily_state':daily,'formal_role_binding_valid':True}


def component_fingerprints(paths):
    import importlib.metadata
    record=read(paths.home/'runtime/napcat.json')
    files={'napcat_main':paths.home/'components/napcat/napcat.mjs',
        'napcat_package':paths.home/'components/napcat/package.json',
        'bridge_plugin':paths.home/'components/astrbot/bot/data/plugins/astrbot_plugin_persona_runtime/main.py',
        'qq_executable':Path(record['qq_listener']['ExecutablePath'])}
    return {'files':{name:hashlib.sha256(file.read_bytes()).hexdigest() for name,file in files.items()},
        'astrbot_version':importlib.metadata.version('astrbot')}


def backup_audit(paths):
    folder=paths.home/'backups/workbench-automatic';index=read(folder/'index.json')
    assert 1<=len(index['entries'])<=7
    latest=index['entries'][-1];file=(folder/latest['file']).resolve()
    assert file.parent==folder.resolve() and file.suffix=='.sqlite3' and time.time()-latest['at']<86400+120
    with closing(sqlite3.connect(file.as_uri()+'?mode=ro',uri=True)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    return {'integrity':'ok','indexed_automatic_backups':len(index['entries']),'latest_epoch':latest['at'],'period_seconds':86400,'manual_backups_excluded':True}


def native_totals():
    calls=[read(p) for p in (EVIDENCE/'calls').glob('*.json')]
    assert all(r['status'] in ('completed','failed_safe') for r in calls)
    counts={kind:sum(c['kind']==kind for c in calls) for kind in ('background','main')}
    assert counts['background']<=24 and counts['main']<=12
    return counts|{'native_tokens':sum((c.get('usage') or {}).get('total_tokens',0) for c in calls),
        'missing_native_usage':sum(c.get('usage') is None for c in calls),'rmb_cost':None}


def close_and_clean(paths,identity,processes):
    from persona_runtime.runtime_control import exit_application
    cfg=ConfigStore(paths).load();lease=read_console(paths)
    assert lease['process']==identity
    exit_application(paths,cfg['instance_id'],'m8e-N1-observation-complete')
    deadline=time.monotonic()+55
    while process_identity_alive(identity) and time.monotonic()<deadline:time.sleep(.5)
    report=read(paths.home/'runtime/last-exit.json')
    assert report.get('console_token')==lease['token']
    assert not process_identity_alive(identity)
    assert report['logout_confirmed'] and report['processes_released']
    assert not ServiceController(paths).has_resident_processes()
    assert all(not process_identity_alive(p['identity']) for p in processes)
    assert read(EVIDENCE/'formal-window/destroyed.json')['native_window_destroyed']
    # The separate read-only metric observer also stops with its original owner.
    launch=read(EVIDENCE/'formal-observation/launch.json')['identity'];deadline=time.monotonic()+12
    while process_identity_alive(launch) and time.monotonic()<deadline:time.sleep(.5)
    assert not process_identity_alive(launch)
    assert business_audit(paths)['critical_unknown_count']==0
    totals=native_totals()
    atomic_json(EVIDENCE/'formal-exit-result.json',{'at':timestamp(),'passed':True,'report':report,
        'owner_exited':True,'observed_owned_children_released':True,'read_only_observer_exited':True,'native_calls':totals})
    # One declared cleanup mechanism; errors are retained, never retried through
    # another deletion method. This only removes the listed M8.E private copies.
    result=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(ROOT/'scripts/cleanup_m8e.ps1')],
        cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        atomic_json(EVIDENCE/'cleanup-blocked.json',{'at':timestamp(),'returncode':result.returncode,
            'diagnostic':result.stderr.decode('utf-8',errors='replace')[:4000],'manual_review_required':True})
        raise RuntimeError('Acceptance cleanup requires review; no alternative deletion attempted')
    assert read(EVIDENCE/'cleanup-result.json')['complete']
    return totals


def run():
    paths=RuntimePaths(DEFAULT_HOME);lock=InstanceLock(EVIDENCE/'finish.lock')
    try:
        for slot in ('I1','I2','I3A','I3B','L1','P1'):assert read(EVIDENCE/(slot+'-result.json'))['passed']
        assert read(EVIDENCE/'tray-menu-user-confirmation.json')['passed']
        assert read(EVIDENCE/'post-verification-login-confirmed.json')['passed']
        expected=read(EVIDENCE/'formal-owner.json');assert expected['core_hashes']==core_hashes()
        from .report import document_hashes,finalize
        expected_documents=document_hashes()
        expected_components=component_fingerprints(paths)
        atomic_json(EVIDENCE/'component-fingerprints.json',expected_components)
        lease=read_console(paths);identity=lease['process'];assert process_identity_alive(identity)
        atomic_json(EVIDENCE/'finish-launch.json',{'identity':current_process_identity(),'owner':identity,'at':timestamp(),'model_calls':0})
        original_day=datetime.now(LOCAL).date();last_wall=time.time();last_monotonic=time.monotonic();active_seconds=0.;midnight=None;gaps=[];processes=[]
        status_file=EVIDENCE/'N1-progress.json';deadline=time.monotonic()+30*3600
        while process_identity_alive(identity) and time.monotonic()<deadline:
            now=time.time();monotonic=time.monotonic();local=datetime.now(LOCAL);gap=now-last_wall;last_wall=now
            if abs(gap-(monotonic-last_monotonic))>5:raise RuntimeError('Wall clock changed independently of elapsed time; natural-day evidence needs review')
            last_monotonic=monotonic
            if gap>20:gaps.append({'at':now,'seconds':gap,'kind':'sleep_or_observer_delay'})
            if local.date()!=original_day and midnight is None:
                midnight={'at':local.isoformat(),'same_owner_identity':True,'observation_gap_seconds':gap,'continuous_sampling':gap<=20,
                    'note':'Software does not work while Windows sleeps; resumed observations are identified separately.'}
            observation=read(EVIDENCE/'formal-observation/status.json');sample=observation['last_sample']
            application=read(paths.home/'runtime/application.json')
            component_states=application.get('states',{})
            if component_states.get('napcat',{}).get('login_required'):
                raise RuntimeError('Native QQ session requires explicit user login; N1 remains incomplete')
            if any(v.get('recovery_reason')=='restart_limit_requires_attention' for v in component_states.values()):
                raise RuntimeError('Component recovery limit reached; N1 needs review')
            if sample['ready'] and now-sample['epoch']<20 and gap<=20:active_seconds+=gap
            processes=snapshot_processes(paths,identity)
            detail={'at':timestamp(),'owner':identity,'ready_active_observed_seconds':active_seconds,'natural_midnight':midnight,
                'gaps':gaps,'processes':processes,'resource':observation,'model_calls_created':0}
            atomic_json(status_file,detail)
            baseline=observation.get('baseline');two=observation.get('two_hours')
            idle_file=EVIDENCE/'formal-observation/idle-10min.json'
            if baseline and two and midnight and active_seconds>=7200 and idle_file.exists():
                idle=read(idle_file);assert idle['passed']
                limit=max(100*1024**2,baseline['rss_bytes']*.1)
                if max(two['rss_bytes'],sample['rss_bytes'])-baseline['rss_bytes']>limit:
                    raise RuntimeError('Observed memory growth exceeds the agreed investigation threshold')
                business=business_audit(paths)
                if business['critical_unknown_count']:raise RuntimeError('Critical unknown execution or delivery remains')
                if business['queue_count'] or not sample['ready']:time.sleep(10);continue
                if sample['process_count']!=len(processes):time.sleep(10);continue
                assert expected['core_hashes']==core_hashes()
                assert expected_components==component_fingerprints(paths)
                result={'at':timestamp(),'passed':True,'real_local_midnight':midnight,'ready_active_observed_seconds':active_seconds,
                    'measurement_scope':'verified_component_branches_and_gui_diagnostics_excluding_external_browsers',
                    'official_device_verification_completed':True,
                    'idle_cpu':idle,'baseline':baseline,'two_hours':two,'last_resource_sample':sample,
                    'memory_growth_limit_bytes':limit,'midnight_or_resume_growth_bytes':sample['rss_bytes']-baseline['rss_bytes'],
                    'sleep_or_observation_gaps':gaps,'business':business,'backup':backup_audit(paths),'native_calls':native_totals(),
                    'simulated_clock_used':False,'extra_model_calls_for_observation':0}
                atomic_json(EVIDENCE/'N1-result.json',result)
                totals=close_and_clean(paths,identity,processes)
                result['native_calls']=totals
                documentation=finalize(result,expected_documents)
                atomic_json(EVIDENCE/'final-result.json',{'at':timestamp(),'passed':True,'software_exited':True,'cleanup_complete':True,
                    'stage':'M8.E','version':'0.8.0.dev3','schema':24,'config':3,'export':19,'bridge':'0.7.3',
                    'native_calls':result['native_calls'],'source_hashes':expected['core_hashes'],'documentation':documentation,'git_committed':False,'git_pushed':False,
                    'G_AUTO_passed':False,'complete_installer_delivered':False})
                print('M8.E observation, normal logout/exit and declared cleanup completed',flush=True)
                return
            time.sleep(10)
        raise RuntimeError('Workbench exited or observation time window ended before N1 completion')
    finally:lock.close()


if __name__=='__main__':
    try:run()
    except Exception as error:
        atomic_json(EVIDENCE/'finish-attention.json',{'at':timestamp(),'error_type':type(error).__name__,'reason':str(error),'stage_passed':False})
        raise
