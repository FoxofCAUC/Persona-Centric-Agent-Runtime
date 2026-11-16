"""Finite .3 real/simulated validation on production recall hooks, public roles only."""
import argparse,asyncio,hashlib,json,time
from pathlib import Path
from . import m9r_comparison as comparison
from .recall_preparation import MEMORIES,CONVERSATION
from persona_runtime.archive import scope,fingerprint
from persona_runtime.config import atomic_json
from persona_runtime.models import identifier
from persona_runtime.recall_runtime import RecallRuntime
from persona_runtime.recall_support import RecallSupport
from persona_runtime.service import RuntimeService

SMOKE=[{'text':'今晚想喝点不甜的，你还记得我平时喜欢什么茶吗？'},
 {'text':'先换个话题，我今天一整天都犯困，想把晚上放慢点。'},
 {'text':'回到喝茶那件事，我前面说的甜度偏好是什么？'}]
TURNS=[{'text':text} for text in CONVERSATION]
TURNS[15]={'text':'我以前说喜欢晚上出门散步，这条先收回，不要再当作现在的偏好。'}
TURNS[22]={'text':'以前那件事你要是不确定就直接说，别现编。'}
SCENARIOS=[{'id':'RA','family':'recall_v8_baseline','recall_enabled':False,'expected_new_material':False,'turns':SMOKE},
 {'id':'RB','family':'recall_v8_candidate','recall_enabled':True,'expected_new_material':False,'turns':SMOKE},
 {'id':'RC','family':'recall_continuous_24','recall_enabled':True,'expected_new_material':False,'turns':TURNS}]
SCENARIOS.append({'id':'RD','family':'recall_query_repair_supplement','recall_enabled':True,'expected_new_material':False,'turns':[
 {'text':'想弄杯省心的咖啡。你记得我嫌手冲麻烦时常用什么杯子吗？'},
 {'text':'先换个话题，今天累得够呛，晚上想歇着发会儿呆。'},
 {'text':'回到咖啡那件事，我前面说的是图省心，不是完全不愿意动手，你还记得那个办法吗？'}]})
for row in SCENARIOS:
    if not any(s['id']==row['id'] for s in comparison.SCENARIOS):comparison.SCENARIOS.append(row)

class Runner(comparison.Runner):
    def make_engine(self,scenario,shape):
        key=(scenario['id'],shape)
        exists=key in self.engines
        seed_file=self.root/'instances'/scenario['id']/shape/'public-recall-seed.json'
        restarting=not exists and seed_file.exists()
        previous_resuming=self.resuming
        if restarting:self.resuming=True
        item=super().make_engine(scenario,shape)
        self.resuming=previous_resuming
        if exists:return item
        if restarting:
            item['recall_seed']=json.loads(seed_file.read_text(encoding='utf-8'))['sources']
            return item
        service=RuntimeService(item['store']);pid=self.plan['persona_id'];sid=item['chat'].session()['session_id']
        now=time.time();clock=[now-200*86400];recall=RecallSupport(item['store'],clock=lambda:clock[0]);selected=scope(pid,'desktop');seed={}
        for label,text in MEMORIES:
            event=service.append_event(sid,'user',text);memory=service.add_memory('preference',text,event['id'])
            seed[label]={'memory_id':memory['id'],'event_id':event['id'],'unit_id':recall.register('memory',{'id':memory['id']},selected)}
            recall.register('dialogue',{'id':event['id']},selected)
        clock[0]=now;recall.confirm_time()
        event=service.append_event(sid,'user','今天晚饭准备做西葫芦。')
        memory=service.add_memory('preference',event['text'],event['id'])
        seed['fresh']={'memory_id':memory['id'],'event_id':event['id'],'unit_id':recall.register('memory',{'id':memory['id']},selected)}
        recall.register('dialogue',{'id':event['id']},selected)
        if scenario['recall_enabled']:
            runtime=RecallRuntime(item['store']);runtime.update(pid,True,0,identifier())
            for _ in range(20):
                runtime.maintain(pid,pages=3)
                if not runtime.status(pid)['dirty_jobs']:break
        item['recall_seed']=seed
        atomic_json(item['paths'].home/'public-recall-seed.json',{'scope':selected,'sources':seed,'lifecycle_time_simulated':True,'os_clock_changed':False})
        return item

    async def position(self,position):
        scenario=next(s for s in self.plan['scenarios'] if s['id']==position['scenario'])
        if scenario['id']=='RC' and position['turn']==21:
            old=self.engines.pop((scenario['id'],position['shape']))
            old['org'].draining=True;old['chat'].draining=True;old['chat'].knowledge.close()
            old['worker'].cancel();await asyncio.gather(old['worker'],return_exceptions=True);await old['chat'].wait()
            atomic_json(self.root/'runtime-restart.json',{'before_position':position['id'],'completed_old_positions_replayed':False,'same_database_reopened':True})
        item=self.make_engine(scenario,position['shape'])
        if scenario['id']=='RC':
            service=RuntimeService(item['store'])
            if position['turn']==5:
                revised=service.revise_memory(item['recall_seed']['tea']['memory_id'],'我现在更喜欢白茶，乌龙茶偶尔才喝。')
                item['recall_seed']['tea_current']=revised['id']
            if position['turn']==15:service.retract_memory(item['recall_seed']['travel']['memory_id'])
        result=await super().position(position)
        # Let the product worker perform local feedback; no admin continuation.
        if position['turn']==len(scenario['turns'])-1:await asyncio.sleep(2.2)
        with item['store'].connection() as db:
            use=db.execute('SELECT details_json FROM context_uses WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',(result['turn_id'],)).fetchone()
            meta=json.loads(use[0]).get('recall_support') if use else None
            events=[dict(r) for r in db.execute('SELECT unit_id,kind,status,alpha FROM recall_support_events WHERE root_id=?',(result['turn_id'],))]
            feedback=[dict(r) for r in db.execute('SELECT stage,state,error_code FROM recall_runtime_feedback WHERE turn_id=?',(result['turn_id'],))]
        atomic_json(self.root/'positions'/position['id']/'recall-observation.json',{'metadata':meta,'events':events,'feedback':feedback})
        return result

def prepare(root,source_home,ids,simulated=False):
    result=comparison.prepare(root,source_home,simulated=simulated,scenario_ids=ids)
    plan=json.loads((Path(root)/'plan.json').read_text(encoding='utf-8'))
    plan['recall_harness_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan['real_main_limit']=30;plan['independent_repair_main_limit']=6
    plan['call_scope']='RA/RB paired three-turn baseline/candidate, RC 24-turn candidate; separate new repair positions only'
    plan['recall_seed']='six synthetic old preferences and one current-day plan; fixture-local aging only'
    plan['restart']='close and reopen chat/worker on the same database before RC round 22; no old position dispatch'
    atomic_json(Path(root)/'plan.json',plan);atomic_json(Path(root)/'plan-hash.json',{'sha256':fingerprint(plan)})
    return result

async def run(root):
    root=Path(root);plan=comparison.load_plan(root)
    if plan['recall_harness_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():raise ValueError('recall_harness_changed')
    # Reuse the existing finite accounting/cleanup runner with this engine adapter.
    original=comparison.Runner;comparison.Runner=Runner
    try:return await comparison.run(root)
    finally:comparison.Runner=original

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','run','report'))
    parser.add_argument('--root',required=True);parser.add_argument('--source-home');parser.add_argument('--ids',default='RA,RB');parser.add_argument('--simulated',action='store_true')
    args=parser.parse_args();root=Path(args.root)
    result=prepare(root,args.source_home,args.ids.split(','),args.simulated) if args.action=='prepare' else asyncio.run(run(root)) if args.action=='run' else comparison.report(root)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
