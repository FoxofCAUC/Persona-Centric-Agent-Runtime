"""Actual authority-to-Shadow path with artificial data; no provider or QQ calls."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import atomic_json
from persona_runtime.disposition import DispositionService
from persona_runtime.disposition_compile import parse_output
from persona_runtime.disposition_shadow import ShadowService
from persona_runtime.models import generic_persona,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import append_version
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

CASES=(
    ('relationship_close','acknowledge','收到这次整理的清单',
     [{'field':'relationship','op':'eq','value':'close'}],'作者设定：两人是亲近、互相信任的朋友，收到确认时偏好简短回应。'),
    ('information_gap','ask','给些建议，支架尺寸该怎么选',
     [{'field':'category','op':'eq','value':'advice_request'}],'作者设定：建议任务缺少必要尺寸时，优先询问缺项。'),
    ('resource_unavailable','defer','给些建议，先说明当前能处理什么',[],
     '作者设定：确实无法处理任务时，说明限制而不承诺后台执行。'),
    ('boundary_source','set_boundary','收到这次整理的清单',[],
     '作者设定：受到授权边界限制时，说明边界，不扩大权限。'),
)


def run(output):
    output=output.resolve()
    if output.drive.upper()!='D:':raise ValueError('Explicit D-drive evidence required')
    output.mkdir(parents=True,exist_ok=True)
    if (output/'integration.json').exists():raise ValueError('Evidence exists; no replay')
    paths=RuntimePaths(output/'artificial-runtime');store=Store(paths);store.initialize()
    service=RuntimeService(store);ds=DispositionService(store);shadow=ShadowService(store)
    results=[]
    try:
        for key,action,text,triggers,author in CASES:
            pid=service.create_persona(replace(generic_persona(),name='M763人工适配核对',expression_style=author))['id']
            service.use_persona(pid)
            if key=='relationship_close':
                with store.connection(write=True) as db:append_version(db,pid,'relationship',{'description':author})
            source=next(s for s in ds.sources(pid)['sources'] if s['kind']==('relationship' if key=='relationship_close' else 'expression'))
            document=parse_output(json.dumps({'schema_version':1,'definitions':[{'id':key,'label':'人工作者配置',
                'source_refs':[{'source_id':source['source_id'],'quote':author}],
                'triggers':triggers,'preferences':{action:2},'priority':10,'enabled':True}]},ensure_ascii=False),[source])
            h=ds.draft_update(pid,document,0,identifier());h=ds.confirm(pid,h['draft_id'],None,h['revision'],identifier())
            observed=shadow.preview(pid,[text]);record=observed['record']
            results.append({'case':key,'author_projection_confirmed':True,'status':observed['status'],
                'chosen_action':record['chosen_action'],'candidate_actions':[r[0] for r in record['candidates']],
                'activated':record['activated'],'excluded':record['excluded'],
                'expected_capability_action':action,'capability_action_selected':record['chosen_action']==action,
                'persisted':observed['persisted'],'applied_to_prompt':observed['applied_to_prompt']})
        with store.connection() as db:
            counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                ('events','turns','attempts','context_manifests','disposition_decisions','disposition_compile_attempts')}
        if any(counts.values()):raise AssertionError('Unexpected call or conversation side effect')
    finally:shadow.close()
    report={'results':results,'counts':counts,'production_path':'author_source_to_confirmed_projection_to_Shadow_preview',
        'resource_boundary_cases':'supplemental diagnostic; no invented trusted runtime state',
        'main_calls':0,'selector_calls':0,'compiler_calls':0,'qq_sends':0,'threads_closed':True,'formal_touched':False}
    atomic_json(output/'integration.json',report)
    print(json.dumps({'cases':len(results),'capability_actions_selected':sum(r['capability_action_selected'] for r in results),
                      'main_calls':0,'formal_touched':False}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);run(p.parse_args().output)
