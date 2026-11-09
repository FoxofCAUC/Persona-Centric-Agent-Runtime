"""Read-only native process/resource observation; never starts or repairs work."""
from datetime import datetime
import json
import os
import sqlite3
import time

import psutil

from .runtime import EVIDENCE,owner,read
from .processes import owned_processes
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.desktop_client import LocalClient
from persona_runtime.instance import process_identity_alive


def run():
    paths,out,expected=owner();folder=out/('formal-observation' if paths.home.name=='Persona-Centric Agent Runtime' else 'isolated-observation')
    folder.mkdir(parents=True,exist_ok=True)
    lease=read(paths.home/'runtime/console.json');identity=lease['process'];root=psutil.Process(identity['pid'])
    previous={};previous_at=None;previous_local=None;idle=[];baseline=None;two_hours=None;midnight=None;gaps=[];warm_since=None
    client=LocalClient(ConfigStore(paths));start=time.time()
    with (folder/'samples.jsonl').open('a',encoding='utf-8') as log:
        while process_identity_alive(identity):
            now=time.time();local=datetime.now().astimezone();gap=now-previous_at if previous_at else None
            if gap and gap>15:gaps.append({'at':now,'gap_seconds':gap})
            try:processes=owned_processes(paths,identity)
            except Exception:
                if not process_identity_alive(identity):break
                raise
            cpu={};rss=0
            for process in processes:
                try:
                    with process.oneshot():
                        key=(process.pid,process.create_time());times=process.cpu_times();cpu[key]=times.user+times.system;rss+=process.memory_info().rss
                except (psutil.NoSuchProcess,psutil.AccessDenied):continue
            delta=sum(max(0,value-previous.get(key,0)) for key,value in cpu.items()) if previous_at else 0
            percent=100*delta/gap/(os.cpu_count() or 1) if gap and gap<15 else None
            ready=False;known_idle=False
            try:
                health=client.call('GET','/v1/health',timeout=2);bridge=read(paths.home/'runtime/astrbot-health.json')
                ready=(health['instance_id']==expected['instance_id'] and bridge['verified'] and bridge['online']
                    and now-bridge['at']<15 and bridge['pid'] in {key[0] for key in cpu})
                known_idle=ready and not (health['pending'] or health['background_running'] or health['buffer_pending'] or health['sending_parts'])
                if known_idle:
                    db=sqlite3.connect((paths.home/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)
                    try:known_idle=not db.execute("SELECT 1 FROM organization_tasks WHERE status IN ('queued','running','waiting_quota') LIMIT 1").fetchone()
                    finally:db.close()
            except Exception:pass
            row={'epoch':now,'local':local.isoformat(),'process_count':len(cpu),'rss_bytes':rss,'cpu_percent_whole_machine':percent,'ready':ready,'idle':known_idle}
            log.write(json.dumps(row)+'\n');log.flush()
            if not ready:warm_since=None
            elif warm_since is None:warm_since=now
            if ready and baseline is None and now-warm_since>=180:baseline=row
            if baseline and now-baseline['epoch']>=7200 and two_hours is None:two_hours=row
            if previous_local and local.date()!=previous_local.date() and gap and gap<15:midnight=row
            idle=idle+[row] if known_idle and percent is not None else []
            if idle and now-idle[0]['epoch']>=600 and not (folder/'idle-10min.json').exists():
                mean=sum(s['cpu_percent_whole_machine'] for s in idle)/len(idle)
                atomic_json(folder/'idle-10min.json',{'start':idle[0]['epoch'],'end':now,'samples':len(idle),'mean_cpu_percent_whole_machine':mean,'passed':mean<=3})
            threshold=max(100*1024**2,baseline['rss_bytes']*.1) if baseline else None
            result={'started':start,'last_sample':row,'baseline':baseline,'two_hours':two_hours,'natural_midnight':midnight,
                'sleep_or_observation_gaps':gaps,'memory_growth_limit_bytes':threshold,
                'two_hour_growth_bytes':two_hours['rss_bytes']-baseline['rss_bytes'] if two_hours and baseline else None,
                'midnight_growth_bytes':midnight['rss_bytes']-baseline['rss_bytes'] if midnight and baseline else None,
                'owner_alive':True,'read_only':True,'model_calls':0}
            atomic_json(folder/'status.json',result)
            previous=cpu;previous_at=now;previous_local=local;time.sleep(5)
    value=read(folder/'status.json') if (folder/'status.json').exists() else {'started':start}
    atomic_json(folder/'status.json',value|{'owner_alive':False,'stopped':time.time()})


if __name__=='__main__':run()
