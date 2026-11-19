"""Measure visible desktop acceptance under a full background queue and verify per-source coverage."""
import asyncio
import json
import math
from pathlib import Path
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import webview
from aiohttp import web
from persona_runtime.config import ConfigStore
from persona_runtime.models import PersonaSpec, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.workbench_webview import DesktopControls, workbench_html
from tests.test_desktop_recovery import Completion
from tests.test_import_history_memory import HistoryMemoryTests
from tests.test_m8d import Model
from tests.workbench_import_v2_smoke import Screen


def percentile(values):
    return round(sorted(values)[math.ceil(.95*len(values))-1],2)


def exercise(home):
    if home.exists():
        raise ValueError("请选择新的隔离实例")
    paths=RuntimePaths(home)
    config=ConfigStore(paths)
    config.initialize()
    config.update({"model":{"name":"791","base_url":"http://127.0.0.1:9","api_key":"791"},
                   "organization":{"model":{"name":"792","base_url":"http://127.0.0.1:9","api_key":"792"}}})
    store=Store(paths)
    store.initialize()
    service=RuntimeService(store)
    people=[service.create_persona(PersonaSpec(1,n,n,[n],n,[n],n))["id"] for n in ("791","792")]
    service.use_persona(people[0])
    class TimedCompletion(Completion):
        async def complete(self,model,messages):
            self.began=time.perf_counter()
            return await super().complete(model,messages)
    main=TimedCompletion()
    app=create_app(paths,client=main,foreground_only=True)
    org=app["organization"]
    chat=app["engine"]
    sessions=[chat.session(persona_id=pid)["session_id"] for pid in people]
    required={}
    for pid in people:
        service.use_persona(pid)
        fixture=SimpleNamespace(persona={"id":pid},cfg=config,store=store,org=org)
        imported=HistoryMemoryTests.restore(fixture)
        with store.connection() as db:
            rows={r[0] for r in db.execute("SELECT id FROM turns WHERE session_id=?",(imported,))}
        required[pid]={"imported":rows,"native":set()}
    service.use_persona(people[0])
    loop=asyncio.new_event_loop()
    worker=threading.Thread(target=loop.run_forever,daemon=True)
    worker.start()
    def dispatch(coro):
        return asyncio.run_coroutine_threadsafe(coro,loop).result(90)
    def first_status():
        with store.connection() as db:
            return db.execute("SELECT status FROM organization_tasks WHERE id=? AND persona_id=?",
                              (first,people[0])).fetchone()[0]
    entered=asyncio.Event()
    release=asyncio.Event()
    class Probe:
        async def complete(self,model,messages):
            entered.set()
            await release.wait()
            return "902",{"total_tokens":1}
    async def start():
        runner=web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner,"127.0.0.1",0).start()
        config.update({"service_port":runner.addresses[0][1]})
        # Drive the same scheduler explicitly at fault and clock boundaries.
        app["organization_worker"].cancel()
        await asyncio.gather(app["organization_worker"],return_exceptions=True)
        org.foreground_only=False
        org.client=Probe()
        first=org.test_model()["id"]
        executing=asyncio.create_task(org.managed.run_next("background"))
        await asyncio.wait_for(entered.wait(),10)
        for _ in range(8):
            org.test_model()
        try:org.test_model()
        except RuntimeErrorBase as error:
            assert str(error)=="organization_queue_full"
        else:raise AssertionError("background capacity was not reached")
        return runner,executing,first
    async def cover():
        front_queue=[]
        for i in range(6):
            j=i%2
            pid=people[j]
            service.use_persona(pid)
            submitted=time.perf_counter()
            turn=await chat.submit("desktop",str(800000+i),str(800000+i),sessions[j],persona_id=pid)
            await chat.wait()
            assert chat.get(turn["id"])["status"]=="completed"
            front_queue.append((main.began-submitted)*1000)
            required[pid]["native"].add(turn["id"])
            assert first_status()=="running"
        with store.connection() as db:
            count,oldest=db.execute("SELECT count(*),min(created_at) FROM organization_tasks WHERE status='queued'").fetchone()
        assert count==8
        oldest_wait=round(time.time()-datetime.fromisoformat(oldest).timestamp(),2)
        release.set()
        await executing
        for _ in range(8):
            assert await org.managed.run_next("background")
        assert first_status()=="completed"
        org.client=Model()
        now=time.time()
        org.managed.clock=lambda:now
        now+=601
        steps=0
        began=time.perf_counter()
        first_progress={}
        for _ in range(48):
            org.automatic.tick()
            await org.managed.run_next("background")
            steps+=1
            with store.connection() as db:
                done={r[0] for r in db.execute("SELECT turn_id FROM memory_coverage WHERE disposition='processed'")}
                backlog=db.execute("SELECT count(*) FROM organization_tasks WHERE status IN ('queued','running','waiting_quota')").fetchone()[0]
            for index,pid in enumerate(people):
                for source,ids in required[pid].items():
                    if ids & done:
                        first_progress.setdefault(f"{index+1}.{source}",round(time.perf_counter()-began,2))
            if not backlog and all(ids<=done for scopes in required.values() for ids in scopes.values()):
                return {"completed_by_person":[{source:len(ids) for source,ids in scopes.items()} for scopes in required.values()],
                        "remaining_tasks":backlog,"scheduler_steps":steps,"eligibility_clock_advance_seconds":601,
                        "memory_transport_calls":len(org.client.calls),"oldest_queued_seconds":oldest_wait,
                        "foreground_replies_while_saturated":len(front_queue),
                        "submit_to_model_p95_ms":percentile(front_queue),"first_coverage_seconds":first_progress}
        raise AssertionError([(len(ids),len(ids&done)) for scope in required.values() for ids in scope.values()])

    runner,executing,first=dispatch(start())
    controls=DesktopControls(lambda _:None,lambda:{"label":"791"},paths,lambda:window)
    window=webview.create_window("PCAR 工作台联合验收",html=workbench_html(),js_api=controls,width=1040,height=740)
    errors=[];report={};lock_times=[]
    connect=Store._connect
    def observed(db_store,*args,**kwargs):
        db=connect(db_store,*args,**kwargs)
        started=None;waiting=False
        def trace(statement):
            nonlocal waiting,started
            if statement=="BEGIN IMMEDIATE":
                waiting=True
            elif waiting:
                started=time.perf_counter();waiting=False
            elif started is not None and statement in ("COMMIT","ROLLBACK"):
                lock_times.append((time.perf_counter()-started)*1000);started=None
        db.set_trace_callback(trace)
        return db
    def interact():
        screen=Screen(window)
        times=[]
        try:
            screen.wait("window.pcarLive?.active && !pcarLive.offline && !message.disabled")
            for i in range(100):
                pid=people[i%2]
                screen.eval("pcarLive.select(state.people.find(p=>p.id==="+json.dumps(pid)+"));true")
                screen.wait("currentPerson()?.id==="+json.dumps(pid)+" && !pcarLive.selecting && !message.disabled")
                value=str(700000+i)
                screen.eval("message.value="+json.dumps(value)+";message.dispatchEvent(new Event('input',{bubbles:true}));window.acceptStart=performance.now();document.querySelector('#composer').requestSubmit();true")
                condition="Array.from(timeline.querySelectorAll('[data-submission-state=accepted]')).some(e=>e.textContent==="+json.dumps(value)+")"
                deadline=time.monotonic()+5
                while not screen.eval(condition):
                    assert time.monotonic()<deadline,(i,"acceptance")
                    time.sleep(.01)
                times.append(screen.eval("performance.now()-window.acceptStart"))
                with store.connection() as db:
                    row=db.execute("SELECT b.id,b.revision FROM ingress_receipts r JOIN input_batches b ON b.id=r.batch_id ORDER BY r.accepted_seq DESC LIMIT 1").fetchone()
                controls._client.call("POST",f"/v2/batches/{row[0]}/cancel",{"persona_id":pid,"expected_revision":row[1],"operation_id":value})
            assert first_status()=="running"
            report.update(visible_accept_p95_ms=percentile(times),samples=len(times),visible_accept_ms=times,
                          queue_waiting=8,background_inflight=1)
            report.update(dispatch(cover()))
            report.update(sqlite_write_section_p95_ms=percentile(lock_times),real_model_calls=0)
            (home/"pressure.json").write_text(json.dumps(report,indent=2),encoding="utf8")
            assert report["visible_accept_p95_ms"]<=500,report["visible_accept_p95_ms"]
        except BaseException as error:errors.append(error)
        finally:window.destroy()
    try:
        with patch.object(Store,"_connect",observed):
            webview.start(interact,gui="edgechromium",private_mode=True,storage_path=str(home/"cache/webview"))
    finally:
        loop.call_soon_threadsafe(release.set)
        dispatch(runner.cleanup())
        loop.call_soon_threadsafe(loop.stop);worker.join(5);loop.close()
    if errors:raise errors[0]
    print(json.dumps({k:v for k,v in report.items() if k!="visible_accept_ms"}))


if __name__=="__main__":
    exercise(Path(sys.argv[1]).resolve())
