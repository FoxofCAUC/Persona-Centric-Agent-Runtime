"""Fixed fictional retrieval fixtures; evaluates context IDs, never a real provider."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import generic_persona, identifier
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class Stub:
    async def complete(self, model, messages):
        return 'offline', None


async def evaluate(home):
    root = home/'tmp'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='m4-evaluation-', dir=root) as directory:
        paths = RuntimePaths(Path(directory))
        cfg = ConfigStore(paths)
        cfg.initialize()
        cfg.update({'model': {'base_url':'http://127.0.0.1:1','name':'offline','hourly_limit':1000}})
        store = Store(paths)
        store.initialize()
        service = RuntimeService(store)
        service.use_persona(service.create_persona(generic_persona())['id'])
        source_session = service.create_session()
        memories = {}
        for label, text in (('tea','用户喜欢乌龙茶'),('journal','用户手账项目叫雨桥'),('clips','用户收集松绿色纸夹')):
            event=service.append_event(source_session['id'],'note',text)
            memories[label]=service.add_memory('preference' if label=='tea' else 'user_fact',text,event['id'])
        engine=ChatEngine(store,cfg,Stub())
        organization=OrganizationEngine(store,cfg,Stub(),engine)
        results=[]
        async def query(label, text, expected, setup=None):
            session=engine.session(fresh=True)
            if setup:
                await engine.submit('desktop',setup,identifier(),session['session_id'])
                await engine.wait()
            turn=await engine.submit('desktop',text,identifier(),session['session_id'])
            await engine.wait()
            details=organization.explain(turn['id'])[0]['details']
            actual={r['id'] for r in details['memories']}
            wanted={memories[k]['id'] for k in expected}
            results.append({'case':label,'expected':sorted(wanted),'actual':sorted(actual),
                            'hits':len(actual&wanted),'misses':len(wanted-actual),'false_recalls':len(actual-wanted),
                            'exact':actual==wanted,'source_event_ids':[service.show_memory(i)['source_event_id'] for i in actual]})
        await query('specific_tea','乌龙茶偏好',{'tea'})
        await query('specific_journal','雨桥项目',{'journal'})
        await query('specific_clips','松绿色纸夹',{'clips'})
        await query('unrelated','量子纠缠',set())
        await query('follow_up','刚才那种茶呢',{'tea'},setup='乌龙茶')
        memories['tea']=service.revise_memory(memories['tea']['id'],'用户改为喜欢咖啡')
        await query('corrected_version','咖啡',{'tea'})
        await query('old_version_absent','乌龙茶',set())
        service.retract_memory(memories['tea']['id'])
        await query('retracted_absent','咖啡',set())
        hits=sum(r['hits'] for r in results)
        misses=sum(r['misses'] for r in results)
        false=sum(r['false_recalls'] for r in results)
        return {'scope':'fictional context-ID retrieval, not semantic/generalization or natural response quality',
                'real_model_calls':0,'real_qq_messages':0,'cases':results,'hits':hits,'misses':misses,'false_recalls':false,
                'precision':hits/(hits+false) if hits+false else None,'recall':hits/(hits+misses) if hits+misses else None,
                'all_exact':all(r['exact'] for r in results)}


if __name__=='__main__':
    home=RuntimePaths.from_environment().home
    report=asyncio.run(evaluate(home))
    path=home/'exports/acceptance'/('M4-retrieval-evaluation-'+identifier()+'.json')
    atomic_json(path,report)
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False))
    print('Report:',path)
    raise SystemExit(0 if report['all_exact'] else 1)
