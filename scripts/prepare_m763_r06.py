"""Freeze artificial authorities and 300 accepted requests; no external model calls."""
import argparse
import asyncio
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService,empty_character
from persona_runtime.character_embedding import CharacterEmbeddingProvider
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_policy import choose
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.disposition import DispositionService
from persona_runtime.disposition_compile import parse_output
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.evaluation_material import EvaluationSession
from persona_runtime.ingress import IngressService
from persona_runtime.models import PersonaSpec,identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import append_version
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class NoDispatch:
    async def complete(self,*_):raise AssertionError('Preparation never dispatches a model')


def configure(paths,approved):
    cfg=ConfigStore(paths);cfg.initialize();value=cfg.load()
    value['model']=deepcopy(approved['model']);value['character_selector']=deepcopy(approved['character_selector'])
    value['organization']['daily_enabled']=False;atomic_json(cfg.file,value)
    return cfg


def projection(ds,pid,author,relationship):
    sources=ds.sources(pid)['sources'];expr=next(s for s in sources if s['kind']=='expression')
    definitions=[]
    if author['preferred_action']=='adaptive':
        definitions=[{'id':'necessary-information','label':'作者确认的必要信息偏好','triggers':[{'field':'category','op':'eq','value':'advice_request'}],
            'preferences':{'ask':2,'explain':1},'priority':5,'enabled':True,'source_refs':[{'source_id':expr['source_id'],'quote':expr['text']}]},
            {'id':'close-relationship','label':'作者确认的熟悉关系表达','triggers':[{'field':'relationship','op':'eq','value':'close'}],
            'preferences':{'support':2,'acknowledge':1},'priority':10,'enabled':True,'source_refs':[{'source_id':expr['source_id'],'quote':expr['text']}]}]
    else:
        definitions=[{'id':'category-'+category,'label':'作者确认的'+category+'交流偏好','triggers':[{'field':'category','op':'eq','value':category}],
            'preferences':{author['preferred_action']:2},'priority':5,'enabled':True,'source_refs':[{'source_id':expr['source_id'],'quote':expr['text']}]} for category in author['categories']]
    doc=parse_output(json.dumps({'schema_version':1,'definitions':definitions},ensure_ascii=False),sources)
    relation=next(s for s in sources if s['kind']=='relationship')
    doc.update(schema_version=2,relationship_mapping={'value':relationship,'source_ref':{k:relation[k] for k in ('source_id','version_id','path','entry_id','content_hash')}|
        {'start':0,'end':len(relation['text']),'fragment_hash':fingerprint(relation['text'])}})
    doc['source_versions']['relationship']=relation['version_id']
    h=ds.current(pid);h=ds.draft_update(pid,doc,h['revision'],identifier())
    return ds.confirm(pid,h['draft_id'],h['active_id'],h['revision'],identifier())


async def close(engine):
    await engine.wait();await engine.observer.drain();engine.cognitive.shadow.close();engine.cognitive.characters.close()


def copy_database(source_store,paths):
    paths.initialize();store=Store(paths)
    with source_store.connection() as source:
        with closing(sqlite3.connect(store.path)) as dest:source.backup(dest)
    store.initialize()
    return store


async def prepare(home,out,fixture_path,*,prompt_v2=False):
    home=home.resolve();out=out.resolve()
    if not out.is_relative_to(home/'exports/acceptance'):raise ValueError('Controlled evidence directory required')
    if (out/'prepared.json').exists():raise ValueError('Already prepared; use existing frozen run')
    fixture=json.loads(fixture_path.read_text(encoding='utf-8'))
    if len(fixture['cases'])!=100 or len(fixture['authors'])!=8:raise ValueError('Frozen coverage invalid')
    approved=ConfigStore(RuntimePaths(home)).load()
    ledger=EvaluationStore(RuntimePaths(out));package='m763-r06-independent-v1'
    if not ledger.path(package).exists():
        authors_by_key={a['key']:a for a in fixture['authors']}
        ledger.create(package,[{'id':c['id'],'scenario':'\n'.join(c['members']),'author_summary':authors_by_key[c['persona_key']]['author_text']} for c in fixture['cases']],
            {'fixture_sha256':sha256(fixture_path.read_bytes()).hexdigest(),'purpose':'independent_three_arm'},seed=fixture['seed'])
    seed_paths=RuntimePaths(out/'seed');seed=Store(seed_paths);seed.initialize();seed_cfg=configure(seed_paths,approved)
    service=RuntimeService(seed);ds=DispositionService(seed);characters=CharacterService(seed);people={}
    if (out/'authors.json').exists():people=json.loads((out/'authors.json').read_text(encoding='utf-8'))
    else:
        for author in fixture['authors']:
            spec=PersonaSpec(1,author['name'],'人工独立评测角色；'+author['author_text'],[author['author_text']],author['author_text'],
                ['如实区分事实与未知','不把作者设定当作共同经历','完成当前合法任务，策略不覆盖必要答案'],author.get('background','人工虚构角色，在社区档案室参与资料整理。'))
            pid=service.create_persona(spec)['id'];service.use_persona(pid)
            with seed.connection(write=True) as db:append_version(db,pid,'relationship',{'description':'作者设定：双方是普通合作关系，没有额外亲密设定。'},actor='test_author')
            data=empty_character()
            data['lorebook']=[{'id':'archive-room','title':'静弦档案室','category':'place','keywords':['档案室','档案'],
                'text':'静弦档案室是作者设定的社区资料整理空间，有一面手写索引墙；不是角色与用户实际共同到访的记录。','priority':3,'enabled':True,'visibility':'conversation','dependencies':[]}]
            data['examples']=[{'id':'listening','scenario':'倾诉','keywords':['倾诉','挫败'],'text':'听起来这件事让你挺难受的，我在听。','enabled':True},
                {'id':'celebration','scenario':'庆祝','keywords':['好消息','成功'],'text':'这一步值得高兴，你坚持的部分终于有了回音。','enabled':True}]
            if prompt_v2:data['examples']=deepcopy(author['examples'])
            char=characters.update(pid,data,characters.current(pid)['id'],identifier())
            projected=projection(ds,pid,author,'neutral')
            people[author['key']]={'persona_id':pid,'character_version':char['id'],'projection_id':projected['active_id']}
        atomic_json(out/'authors.json',people)
    policy=choose('hybrid_llm');model_dir=home/'models/character-embedding'/policy['config']['model_fingerprint']
    provider=CharacterEmbeddingProvider(RuntimePaths(home),model_dir)
    diagnostic=provider.diagnose()
    if diagnostic['status']!='ready':raise ValueError('Local embedding unavailable: '+diagnostic['reason'])
    index=CharacterSemanticIndex(seed,provider)
    for value in people.values():index.build(value['persona_id'],value['character_version'])
    atomic_json(out/'embedding.json',{'diagnostic':diagnostic,'prepared_personas':8,'cloud_calls':0})
    prepared=[]
    authors={a['key']:a for a in fixture['authors']}
    for case in fixture['cases']:
        case_dir=out/'cases'/case['id'];case_dir.mkdir(parents=True,exist_ok=True)
        saved=case_dir/'prepared.json'
        if saved.exists():prepared.append(json.loads(saved.read_text(encoding='utf-8')));continue
        base_paths=RuntimePaths(case_dir/'authority');base=copy_database(seed,base_paths);base_cfg=configure(base_paths,approved)
        pid=people[case['persona_key']]['persona_id'];base_service=RuntimeService(base);base_service.use_persona(pid)
        if case['relationship']!='neutral':
            with base.connection(write=True) as db:append_version(db,pid,'relationship',{'description':'作者设定：双方是互相信任的亲近朋友。'},actor='test_author')
            projection(DispositionService(base),pid,authors[case['persona_key']],case['relationship'])
        engines={};turns={};receipts={}
        try:
            for arm in 'ABC':
                paths=RuntimePaths(case_dir/arm);store=copy_database(base,paths);cfg=configure(paths,approved)
                if prompt_v2 and arm=='C':
                    from persona_runtime.dialogue_voice import freeze
                    value=cfg.load();value['model']['_dialogue_profile']=freeze(case['id'],case['members']);atomic_json(cfg.file,value)
                grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((pid,)),home)
                engine=ChatEngine(store,cfg,NoDispatch(),assist_admission=grant);engines[arm]=engine
                engine.store.character_committed=lambda *_:[]
                ingress=IngressService(engine)
                current=ingress.current_policy(pid,'desktop')
                with patch.object(engine,'_schedule'):
                    ingress.update_policy(pid,'desktop','natural_batch',current['id'],identifier(),reply_mode='natural_structured',character_retrieval_mode='hybrid_llm',disposition_mode='assist' if arm=='C' else 'baseline')
                    with ledger.connect(package) as db:request_id=db.execute('SELECT request_id FROM slots WHERE case_id=? AND arm=?',(case['id'],arm)).fetchone()[0]
                    rows=[ingress.accept({'persona_id':pid,'text':text,'request_id':request_id if i==0 else fingerprint([request_id,i])}) for i,text in enumerate(case['members'])]
                    batch=ingress.get(rows[0]['batch_id']);turns[arm]=ingress.action(batch['id'],'seal',batch['revision'],identifier(),pid)['turn_id'];receipts[arm]=rows
                    ledger.transition(package,case['id'],arm,'not_called','accepted',receipt_id=rows[0]['receipt_id'])
                cache=seed_paths.home/'cache/character-semantic/v1'
                if cache.exists():shutil.copytree(cache,paths.home/'cache/character-semantic/v1',dirs_exist_ok=True)
            EvaluationSession.register_independent(engines,turns,prompt_comparison=prompt_v2)
            frozen={}
            for arm,engine in engines.items():
                with engine.store.connection() as db:
                    row=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns[arm],)).fetchone())
                    frozen[arm]={k:fingerprint(row[k]) for k in ('profile_snapshot','character_snapshot','context_policy_snapshot','model_snapshot','selector_model_snapshot','observation_model_snapshot')}
            result={'case_id':case['id'],'persona_id':pid,'turns':turns,'receipts':receipts,'runtime_roots':{a:str(e.store.paths.home) for a,e in engines.items()},'frozen':frozen}
            atomic_json(saved,result);prepared.append(result)
        finally:
            for engine in engines.values():await close(engine)
        print(json.dumps({'prepared_case':case['id'],'cases':len(prepared),'model_calls':0}),flush=True)
    atomic_json(out/'prepared.json',{'fixture_sha256':sha256(fixture_path.read_bytes()).hexdigest(),'cases':prepared,'authors':people,'model_calls':0,'independent_frozen':True,'prompt_comparison':prompt_v2})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--fixture',required=True,type=Path);p.add_argument('--prompt-v2',action='store_true');args=p.parse_args()
    asyncio.run(prepare(args.home,args.output,args.fixture,prompt_v2=args.prompt_v2))
