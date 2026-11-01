"""Read-only native UI check on the saved artificial simulation. No service."""
import json
from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.expression_selection_r1 import OUT, read, NoDispatch
from persona_runtime.paths import RuntimePaths
from persona_runtime.config import ConfigStore
from persona_runtime.storage import Store
from persona_runtime.chat import ChatEngine
from persona_runtime.desktop import Workbench


def main():
    root=OUT/'simulation';item=next(i for i in read(root/'prepared.json')['items'] if i['key']=='R01-detail-experiment')
    paths=RuntimePaths(Path(item['runtime']));engine=ChatEngine(Store(paths),ConfigStore(paths),NoDispatch())
    engine.store.character_committed=lambda *_:[]
    history=engine.cognitive.inspect(item['persona_id'],'desktop',turn_id=item['turn_id'])
    history['manifests']=[m for m in history['manifests'] if m['task_type']=='dialogue']
    old={k:v for k,v in history['manifests'][0].items() if k!='prompt_assembly'};old['context_id']='artificial-old-record'
    fallback=deepcopy(history['manifests'][0]);fallback['context_id']='artificial-fallback-display'
    s=fallback['prompt_assembly']['material_selection'];s.update(effective_mode='lexical',applied_prompt=None,
        fallback_reason='character_preparation_invalidated',returned_example_count=2,included_example_count=0,
        returned_example_ids=['artificial-one','artificial-two'],included_example_ids=[])
    history['manifests'] += [old,fallback]
    preview=engine.cognitive.preview(item['persona_id'],'desktop',turn_id=item['turn_id'])
    engine.cognitive.shadow.close();engine.cognitive.characters.close()
    with patch.object(Workbench,'poll'),patch.object(Workbench,'refresh_personas'),patch.object(Workbench,'read_logs'):
        w=Workbench(paths);w.title('PCAR R1 人工隔离界面核查 · 只读 / 无模型调用')
        w.persona_id=item['persona_id'];w.ctx_turn.set(item['turn_id']);w.show_page('context')
        def manage(action,**kwargs):
            if action=='context.preview':return preview
            if action=='context.inspect':return history
            raise ValueError('人工隔离核查仅提供只读上下文')
        w.client.manage=manage
        def task(name,resources,action,success=None,**kwargs):
            if name!='读取任务上下文':w.notice.set('人工隔离核查仅提供只读上下文');return
            value=action(None)
            if success:success(value)
        w.run_task=task;w.context_request('preview');w.mainloop();w.hub.pool.shutdown(wait=True,cancel_futures=True)


if __name__=='__main__':main()
