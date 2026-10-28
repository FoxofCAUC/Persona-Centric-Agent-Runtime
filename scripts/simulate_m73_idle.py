"""User-authorized, scoped monotonic deadline simulation; never changes OS time."""
import argparse
import asyncio
import json
import time
from pathlib import Path
from persona_runtime.config import atomic_json
from persona_runtime.sessions import SessionService,IDLE_SECONDS
from scripts.acceptance_m73 import Acceptance


async def simulate(driver):
    driver.online();out=driver.out/'R04-simulated-idle.json'
    if out.exists():raise RuntimeError('Simulation already recorded')
    if not (driver.out/'stop-real-idle-observation.json').exists():raise RuntimeError('Explicit real-wait replacement record required')
    sid=driver.state['calls']['R04']['session_id'];selected=driver.scope('qq');args=selected|{'session_id':sid}
    row=driver.client.manage('session.show',**args)
    if row['state']!='active' or row['blockers']:raise RuntimeError('Test session is not eligible for observation')
    with driver.store.connection() as db:
        others={r['session_id']:dict(r) for r in db.execute("SELECT * FROM session_lifecycle WHERE session_id!=?",(sid,))}
    calls=driver.count_calls();now=[0.];service=SessionService(driver.store,lambda:now[0]);normal_tick=service.tick
    marker={'mode':'simulated_deadline','authority':'explicit_user_acceptance_instruction','threshold_seconds':IDLE_SECONDS,'system_clock_changed':False,'production_threshold_changed':False}
    service.tick=lambda:normal_tick(_session_ids={sid},_clock_evidence=marker)
    started=time.monotonic();worker=asyncio.create_task(service.worker())
    try:
        deadline=time.monotonic()+10
        while sid not in service.clocks:
            if time.monotonic()>deadline:raise RuntimeError('Worker failed to observe test session')
            await asyncio.sleep(.05)
        now[0]=1799;await asyncio.sleep(5.4)
        assert service.show(**args)['state']=='active'
        now[0]=1800;deadline=time.monotonic()+10
        while service.show(**args)['state']!='sealed':
            if time.monotonic()>deadline:raise RuntimeError('Five-second worker did not seal due session')
            await asyncio.sleep(.1)
        package=service.package(**args);assert package['manifest']['clock_evidence']==marker
        atomic_json(driver.out/'R04-package.json',package)
    finally:
        worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    with driver.store.connection() as db:
        unchanged=all(dict(db.execute('SELECT * FROM session_lifecycle WHERE session_id=?',(sid2,)).fetchone())==value for sid2,value in others.items())
    assert unchanged and calls==driver.count_calls()
    report={'passed':True,'mode':'simulated_deadline','real_30minute_gate':'waived_by_user_not_claimed_passed','observed_real_seconds':time.monotonic()-started,'boundary_1799_active':True,'boundary_1800_sealed':True,'worker_poll_seconds':5,'package_id':package['id'],'simulation_exited':worker.done(),'other_sessions_unchanged':unchanged,'additional_model_calls':0,'system_clock_changed':False,'production_threshold_seconds':IDLE_SECONDS}
    atomic_json(out,report);print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();asyncio.run(simulate(Acceptance(a.output)))
