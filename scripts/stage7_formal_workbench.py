"""Finite formal Workbench/channel check, with normal owned service hosts."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.desktop import Workbench
from persona_runtime.instance import InstanceLock
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import timestamp
from persona_runtime.paths import DEFAULT_HOME, RuntimePaths


def run(out,desktop_only=False):
    out=Path(out).resolve();paths=RuntimePaths(DEFAULT_HOME)
    from persona_runtime.private_personas import safe
    safe(out,DEFAULT_HOME/'exports/acceptance')
    report=json.loads((out/'deployment-result.json').read_text(encoding='utf-8'))
    if report['schema']!=21 or ConfigStore(paths).load()['organization']['daily_enabled']:raise ValueError('Formal deployment/pause required')
    controller=ServiceController(paths)
    if controller.has_resident_processes():raise ValueError('Existing hosts require inspection')
    # Restore the exact installed 4.29.1 dashboard from the verified local cache.
    proof=json.loads((out/'qq-real-v2/dashboard-resource-repair.json').read_text(encoding='utf-8'))
    source=Path(proof['source']);target=paths.home/'components/astrbot/bot/data/dist'
    if not all(hashlib.sha256((source/p).read_bytes()).hexdigest()==h for p,h in proof['files'].items()):raise ValueError('Cached dashboard changed')
    if not target.exists():shutil.copytree(source,target)
    if not all((target/p).exists() and hashlib.sha256((target/p).read_bytes()).hexdigest()==h for p,h in proof['files'].items()):raise ValueError('Installed dashboard differs')
    with closing(InstanceLock(paths.home/'runtime/desktop.lock')):
        controller.start_console();window=None;closing_window=False
        try:
            window=Workbench(paths,managed_lifecycle=False,controller=controller)
            def closed(_=None):window.destroy_console()
            def request_close():
                nonlocal closing_window
                if closing_window:return
                closing_window=True
                window.run_task('正常停止本轮正式验证',{'lifecycle','config'},lambda _:controller.stop_all(include_qq=True),closed)
            window.protocol('WM_DELETE_WINDOW',request_close)
            def ready(value):
                atomic_json(out/'formal-host-ready.json',{'at':timestamp(),'pid':os.getpid(),'workbench_constructed':True})
                window.show_page('chat')
                window.load_chat()
                window.refresh_conversation()
            def failed(error):atomic_json(out/'formal-host-error.json',{'at':timestamp(),'error':str(error)})
            start=(lambda _:controller.start('service')) if desktop_only else controller.start_all
            window.after(500,lambda:window.run_task('启动正式验证入口',{'lifecycle','config'},start,ready,failure=failed))
            def observe():
                result=out/'P01R-result.json'
                if result.exists() and not (out/'workbench-visible.json').exists():
                    turn=json.loads(result.read_text(encoding='utf-8'))
                    if turn['reply'] and turn['reply'] in window.transcript.get('1.0','end'):
                        atomic_json(out/'workbench-visible.json',{'at':timestamp(),'turn_id':turn['id'],
                            'reply_hash':hashlib.sha256(turn['reply'].encode()).hexdigest(),
                            'persona_id':window.persona_id,'policy_text':window.chat_mode_text.get(),'visible':bool(window.winfo_viewable())})
                    else:window.refresh_conversation()
                if (out/'formal-close.request').exists():request_close()
                if not window.closed:window.after(1000,observe)
            window.after(1000,observe);window.mainloop()
            window.hub.pool.shutdown(wait=True,cancel_futures=True)
        finally:
            controller.stop_all(include_qq=True);controller.end_console()
            original=json.loads((out/'formal-schedule-pause.json').read_text(encoding='utf-8'))
            ConfigStore(paths).update({'organization':{'daily_enabled':original['original_daily_enabled']}})
            atomic_json(out/'formal-host-closed.json',{'at':timestamp(),'resident':controller.has_resident_processes(),
                'daily_setting_restored':True,'workbench_closed':bool(window and window.closed)})


if __name__=='__main__':run(sys.argv[1],desktop_only='--desktop-only' in sys.argv[2:])
