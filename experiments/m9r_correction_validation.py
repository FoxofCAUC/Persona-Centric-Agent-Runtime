"""Two new correction turns on a COPY of an existing public trial, never a replay.

The source data is only the frozen public M9R.4 persona. This validates an
explicit readable-source correction operation and a cold/new-session followup;
it does not claim automatic NLP correction extraction or repair the old answer.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.models import identifier,timestamp
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .m9r_comparison import root_path,protected,cleanup_credentials
from .m9r_comparison_materials import SPEC

VERSION='m9r4-explicit-correction-v1'
CORRECTION=('人工更正：春1至春4矿洞尚未开放，不能安排下矿；矿洞春5才开放。'
    '春3和春4安排钓鱼、觅食、必要浇水等已可进行的活动；春5之后再考虑下矿。'
    '原混合底稿里春3到春4下矿的安排不再采用。其他建议未在这次更正中重新查证。')
TURNS=[
 {'id':'K01-correction','text':'前面那份七天安排有一处时间不对：矿洞春5才开放，春3和春4不能下矿。就按这个更正重新整理前七天，不用再联网。'},
 {'id':'K02-cold-new-session','text':'上次那份轻松的春季起步计划，春3到春6具体怎么排？只用本地资料，不要重新联网。'},
]


def prepare(root,source_comparison,source_home):
    root=root_path(root);source=root_path(source_comparison)
    if root.exists() and any(root.iterdir()):raise ValueError('correction_root_must_be_empty')
    original=json.loads((source/'plan.json').read_text(encoding='utf-8'))
    if original['kind']!='real' or original['spec_hash']!=fingerprint(SPEC):raise ValueError('public_trial_required')
    record=json.loads((source/'positions/C05-2-two_stage/result.json').read_text(encoding='utf-8'))
    if record['status']!='completed':raise ValueError('source_trial_not_completed')
    instance=RuntimePaths(source/'instances/C05/two_stage');database=Store(instance).path
    if fingerprint(RuntimeService(Store(instance)).show_persona(original['persona_id'])['spec'])!=fingerprint(SPEC):
        raise ValueError('public_persona_changed')
    root.mkdir(parents=True,exist_ok=True)
    home=RuntimePaths.from_environment(source_home).home
    value={'version':VERSION,'source_comparison':str(source),'source_database':str(database),
        'source_database_sha256':hashlib.sha256(database.read_bytes()).hexdigest(),'persona_id':original['persona_id'],
        'source_home':str(home),'protected_before':protected(home),'correction':CORRECTION,'turns':TURNS,
        'source_reference':'https://stardewvalleywiki.com/The_Mines','source_unchanged_required':True,
        'old_reply_replayed':False,'correction_operation':'explicit source replacement on trial copy only',
        'max_new_turns':2,'created_at':timestamp()}
    atomic_json(root/'plan.json',value);atomic_json(root/'plan-hash.json',{'sha256':fingerprint(value)})
    return {'prepared':True,'new_turns':2,'old_reply_replayed':False,'paid_calls':0}


async def run(root):
    root=root_path(root);plan=json.loads((root/'plan.json').read_text(encoding='utf-8'))
    if fingerprint(plan)!=json.loads((root/'plan-hash.json').read_text(encoding='utf-8'))['sha256']:raise ValueError('correction_plan_changed')
    if (root/'instances').exists():raise ValueError('correction_already_reserved_no_replay')
    source=Path(plan['source_database'])
    if hashlib.sha256(source.read_bytes()).hexdigest()!=plan['source_database_sha256']:raise ValueError('source_database_changed')
    paths=RuntimePaths(root/'instances/C11/two_stage');paths.initialize();store=Store(paths)
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
        with sqlite3.connect(store.path) as dst:src.backup(dst)
    cfg=ConfigStore(paths);cfg.initialize();formal=RuntimePaths(Path(plan['source_home']))
    config=cfg.load();config['model']=ConfigStore(formal).load()['model'];atomic_json(cfg.file,config)
    jev=configured_model(formal)
    atomic_json(paths.home/'private/knowledge-router/config.json',{key:jev[key] for key in ('base_url','name','encrypted_key')})
    results=[];chat=None;org=None;worker=None
    def open_engines():
        chat=ChatEngine(store,cfg);org=OrganizationEngine(store,cfg,None,chat)
        policy=org.processing.native.policy(plan['persona_id'])
        if not policy['data']['enabled']:org.processing.native.update(plan['persona_id'],True,policy['revision'],identifier())
        return chat,org,asyncio.create_task(org.worker())
    async def close_engines():
        org.draining=True;chat.draining=True;chat.knowledge.close()
        worker.cancel();await asyncio.gather(worker,return_exceptions=True);await chat.wait()
    try:
        chat,org,worker=open_engines()
        with store.connection() as db:
            row=db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.conversation.record.%' AND json_extract(value,'$.data.persona_id')=? ORDER BY rowid DESC LIMIT 1",(plan['persona_id'],)).fetchone()
            record=json.loads(row[0])['data'];source_id=record['sources'][0]['id']
        org.processing.native.update_source(plan['persona_id'],source_id,'corrected',0,identifier(),replacement=plan['correction'])
        first_session=None
        for index,case in enumerate(plan['turns']):
            if index:
                await close_engines();chat,org,worker=open_engines()
                opened=chat.session('desktop',fresh=True)
                if opened['session_id']==first_session:raise ValueError('cold_new_session_not_created')
            turn=await chat.submit('desktop',case['text'],case['id'])
            await chat.wait();value=chat.get(turn['id'])
            if index==0:first_session=value['session_id']
            with store.connection() as db:
                row=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.conversation.record.'+turn['id'],)).fetchone()
                materials=json.loads(row[0])['data']
                attempts=[dict(row) for row in db.execute('SELECT id,status,usage_json,error_code FROM attempts WHERE turn_id=?',(turn['id'],))]
                auxiliary=[dict(row) for row in db.execute("SELECT a.id,a.status,a.usage_json,json_extract(t.payload_json,'$.knowledge_processing.stage') AS stage FROM organization_attempts a JOIN organization_tasks t ON t.id=a.task_id WHERE json_extract(t.payload_json,'$.knowledge_processing.root_turn_id')=?",(turn['id'],))]
            result={'id':case['id'],'turn_id':turn['id'],'session_id':value['session_id'],'status':value['status'],
                'reply':value.get('reply'),'materials':materials,'main_attempts':attempts,'auxiliary_attempts':auxiliary,
                'original_reply_changed':False,'source_correction_explicit':True,'cold_engine':bool(index)}
            results.append(result);atomic_json(root/(case['id']+'.json'),result)
            if value['status']!='completed':raise ValueError('correction_turn_failed')
            if any(row['stage']!='conversation_web_decision' for row in auxiliary):raise ValueError('unexpected_new_retrieval')
            if materials['organized']:raise ValueError('old_blended_note_still_consumed')
            if not any(row.get('kind')=='user_correction' for row in materials['sources']):raise ValueError('corrected_readable_material_missing')
            print(json.dumps({'position':case['id'],'status':'completed','new_web_tools':0}),flush=True)
    finally:
        if worker and not worker.done():await close_engines()
        cleanup_credentials(root)
    known=0;calls=0;unknown=0
    for result in results:
        for row in result['main_attempts']+result['auxiliary_attempts']:
            usage=json.loads(row['usage_json'] or '{}') or {};calls+=1
            if type(usage.get('total_tokens')) is int:known+=usage['total_tokens']
            else:unknown+=1
    report={'version':VERSION,'new_turns_completed':len(results),'model_attempt_records':calls,'known_total_tokens':known,
        'unknown_usage_calls':unknown,'new_search_or_fetch_calls':0,'actual_total_amount':None,
        'source_database_unchanged':hashlib.sha256(source.read_bytes()).hexdigest()==plan['source_database_sha256'],
        'formal_configuration_unchanged':protected(formal.home)==plan['protected_before'],
        'temporary_credentials_removed':True,'source_correction_explicit':True,'automatic_nlp_correction_claimed':False,
        'first_original_guide_precision_error_retained':True,'user_taste':'not_assessed'}
    atomic_json(root/'report.json',report);return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','run'))
    parser.add_argument('--root',required=True);parser.add_argument('--source-comparison');parser.add_argument('--source-home')
    args=parser.parse_args();root=root_path(args.root)
    result=prepare(root,args.source_comparison,args.source_home) if args.action=='prepare' else asyncio.run(run(root))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
