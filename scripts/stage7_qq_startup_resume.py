"""Retry only a zero-dispatch QQ startup; retain earlier failure evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plugins.release_acceptance import qq
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.deployment import child_environment
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import timestamp
from persona_runtime.storage import Store


def run(out):
    out=Path(out).resolve();owner,paths=qq.verify(out)
    with Store(paths).connection() as db:
        for table in ('attempts','organization_attempts','reply_delivery_attempts','turns'):
            qq.require(db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0,'startup_resume_must_have_zero_dispatch')
    qq.require(not list((out/'positions').glob('*.json')),'startup_resume_no_completed_position')
    qq.write_new(out/'startup-resume.json',{'at':timestamp(),'pid':os.getpid(),'reason':'missing dashboard repaired; bounded startup trace','model_calls_before':0})
    controller=ServiceController(paths);qq.require(not controller.has_resident_processes(),'resident_before_resume')
    runtime=qq.transport.Runtime(out,verifier=qq.verify,position=qq.position)
    controller.start_console();trace=(out/'host-startup-trace.log').open('wb')
    try:
        runtime.open()
        # Same normal host, with an additional stack dump; no SDK/model replacement.
        from persona_runtime.lifecycle import require_console
        from persona_runtime.deployment import render_components
        render_components(paths);cfg=ConfigStore(paths).load();lease=require_console(paths)
        code='import faulthandler; faulthandler.dump_traceback_later(20, repeat=True); from persona_runtime.host import main; main()'
        child=subprocess.Popen([str(paths.home/'components/astrbot/.venv/Scripts/python.exe'),'-B','-c',code,
            '--role','astrbot','--instance',cfg['instance_id'],'--console-token',lease['token']],
            cwd=qq.transport.ROOT,env=child_environment(paths),stdout=trace,stderr=trace,creationflags=0x08000000)
        controller.processes['astrbot']=child
        until=time.monotonic()+90
        while not controller._start_ready('astrbot',controller.health('astrbot')):
            if child.poll() is not None or time.monotonic()>until:raise RuntimeError('Traced startup not ready')
            time.sleep(.5)
        controller.start('napcat')
        while qq.position(out) is not None and not (out/'stop.request').exists():
            qq.verify(out)
            if runtime.recorder.blocked:raise RuntimeError('QQ acceptance stopped; inspect evidence')
            f=paths.home/'runtime/astrbot-health.json';h=qq.read(f) if f.exists() else {}
            atomic_json(out/'listening.json',{'online':bool(h.get('verified') and h.get('online') and time.time()-h.get('at',0)<15),
                'next':qq.position(out)['id'],'at':timestamp(),'home':str(paths.home)})
            time.sleep(1)
        if qq.position(out) is None:
            rows=[qq.read(out/'positions'/(c['id']+'.json')) for c in qq.cases()]
            qq.require(all(r['coverage_passed'] for r in rows),'qq_coverage')
            qq.write_new(out/'summary.json',{'positions':rows,'passed':True,'user_visible':'pending','code_hashes':qq.code_hashes()})
    finally:
        controller.stop_all(include_qq=True);runtime.close();controller.end_console();trace.close()
        qq.write_new(out/'completion-close.json',{'at':timestamp(),'resident':controller.has_resident_processes()})


if __name__=='__main__':run(sys.argv[1])
