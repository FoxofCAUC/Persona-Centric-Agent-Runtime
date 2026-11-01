"""Isolated visual acceptance harness; buttons read synthetic in-memory responses.

No HTTP service, model connection, QQ, or formal runtime is opened by this harness.
Close the window to remove its temporary synthetic runtime.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ['TEMP']=os.environ['TMP']='D:/Persona-Centric Agent Runtime/tmp'
from tests.test_m762 import M762
from persona_runtime.desktop import Workbench
from persona_runtime.profile import append_version


async def material():
    test=M762();await test.asyncSetUp()
    test.publish();test.mode('shadow');tid=await test.queued('人工界面核查：我想找人倾诉')
    await test.engine._run(tid)
    history=test.engine.cognitive.inspect(test.pid,'desktop',turn_id=tid)
    old={k:v for k,v in history['manifests'][0].items() if k!='prompt_assembly'}
    old['context_id']='artificial-old-record';history['manifests'].append(old)
    with test.store.connection(write=True) as db:
        append_version(db,test.pid,'state',{'text':'仅用于预览区别的人工状态','expires_at':'2099-01-01T00:00:00+00:00'})
    preview=test.engine.cognitive.preview(test.pid,'desktop',turn_id=tid)
    test.engine.cognitive.shadow.close();test.engine.cognitive.characters.close()
    return test,tid,preview,history


def main():
    test,tid,preview,history=asyncio.run(material())
    with patch.object(Workbench,'poll'),patch.object(Workbench,'refresh_personas'),patch.object(Workbench,'read_logs'):
        window=Workbench(test.paths)
        window.title('PCAR R0 人工隔离验收 · 无真实模型 / QQ')
        window.persona_id=test.pid;window.ctx_turn.set(tid);window.show_page('context')
        def manage(action,**kwargs):
            if action=='context.preview':return preview
            if action=='context.inspect':return history
            raise ValueError('人工核查仅支持上下文读取')
        window.client.manage=manage
        def task(name,resources,action,success=None,**kwargs):
            if name!='读取任务上下文':window.notice.set('隔离核查仅支持上下文读取');return
            try:
                result=action(None)
                if success:success(result)
            except Exception:window.notice.set('人工核查未执行该操作')
        window.run_task=task
        window.context_request('preview')
        window.mainloop()
        window.hub.pool.shutdown(wait=True,cancel_futures=True)
    test.tmp.cleanup()


if __name__=='__main__':main()
