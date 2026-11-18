"""Prepare and open the isolated, user-driven E.N5 desktop experience."""
import argparse
import asyncio
from contextlib import ExitStack, closing
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory

from aiohttp import web
from persona_runtime.archive import scope
from persona_runtime.character import CharacterService
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import fingerprint, outside_repository
from persona_runtime.instance import InstanceLock
from persona_runtime.knowledge_native_route import flash_model
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.models import LOCAL_USER_ID, PersonaSpec, RuntimeErrorBase, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import PrivatePersonas, guard_cloud_dispatch, provider_identity, safe
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import SessionService, register, select
from persona_runtime.storage import Store
from .corpus_reply_study import read_private, save_once


STUDY='en5-ling-desktop-v1'
VERSION='en5-experience-v1'
BASELINE='companion-runtime-v8/deepseek-v4-pro'
MANIFEST=Path('runtime/en5-experience.json')
DECISION=Path(__file__).resolve().parents[1]/'docs/expression/E.N3-发言风格基线决议.json'


def require(ok, reason):
    if not ok:
        raise RuntimeErrorBase('en5_'+reason)


def checksum(path):
    return digest(Path(path).read_bytes())


def bind_sessions(paths, persona_id):
    """Admit the first normal desktop session and reserve the second without changing the head."""
    dbstore=Store(paths)
    with dbstore.connection(write=True) as db:
        require(dbstore.current(db)==persona_id and not db.execute(
            'SELECT 1 FROM sessions WHERE persona_id=?',(persona_id,)).fetchone(),'empty_sessions_required')
        first=select(db,persona_id,'desktop')
        second=identifier()
        db.execute("INSERT INTO sessions VALUES(?,?,?,'private',?)",(second,persona_id,LOCAL_USER_ID,timestamp()))
        row=dict(db.execute('SELECT * FROM sessions WHERE id=?',(second,)).fetchone())
        register(db,row,scope(persona_id,'desktop'),managed=False)
        return [first,second]


def select_part(paths, binding, part):
    require(part in (1,2),'part_invalid')
    store=Store(paths); pid=binding['persona_id']; first,second=binding['sessions']
    with store.connection() as db:
        require(store.current(db)==pid,'persona_changed')
        completed=db.execute("SELECT count(*) FROM turns WHERE session_id=? AND status='completed' AND reply_event_id IS NOT NULL",(first,)).fetchone()[0]
    if part==2:
        require(completed>=15,'first_part_incomplete')
        from persona_runtime.en5_display import read_receipts
        receipts=read_receipts(paths,binding['study_id'],pid,first)
        require(sum('main' in phases for phases in receipts.values())>=15,'first_part_display_incomplete')
        manager=SessionService(store)
        current=manager.current(pid,'desktop')
        if current['session_id']==first:
            sealed=manager.seal(first,pid,current['revision'],STUDY+'-part-one-complete','desktop')
            require(sealed['state']=='sealed','first_part_still_active')
    with store.connection(write=True) as db:
        return select(db,pid,'desktop',session_id=(first if part==1 else second))


def material_source(state_path, source_home):
    state=read_private(outside_repository(state_path))
    decision=json.loads(DECISION.read_text(encoding='utf-8'))
    effective=read_private(outside_repository(state['en3_ling_repair_effective_decision']))
    contract_path=outside_repository(state['en3_ling_repair_input_contract'])
    require(decision==effective and decision['selected_branch']=='B' and decision['wire_settings']['model']=='deepseek-v4-pro'
            and checksum(contract_path)==decision['input_contract_sha256'],'baseline_decision_changed')
    contract=read_private(contract_path)
    source_home=safe(source_home)
    source_db=Store(RuntimePaths(source_home)).path
    with closing(sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute("""SELECT p.id,p.spec_json,json_set(c.data_json,'$.lorebook',
            json((SELECT json_group_array(json(j.value)) FROM json_each(c.data_json,'$.lorebook') j
                  WHERE json_extract(j.value,'$.enabled')=1 AND json_extract(j.value,'$.visibility')='conversation')),
            '$.examples',json('[]')) FROM personas p JOIN character_versions c ON c.persona_id=p.id
            JOIN character_heads h ON h.persona_id=p.id AND h.version_id=c.id WHERE c.id=?""",
            (contract['character_version'],)).fetchone()
    require(row and digest(row[1].encode('utf-8'))==contract['persona_spec_sha256'],'reviewed_character_changed')
    spec,character=json.loads(row[1]),json.loads(row[2])
    require(bool(contract['persona']) and spec['name']==contract['persona'],'wrong_reviewed_persona')
    record=next((item for item in PrivatePersonas(RuntimePaths(source_home)).load(True)['records']
                 if item.get('persona_id')==row[0]),None)
    require(record and record['state']=='ready','private_source_missing')
    base=read_private(outside_repository(state['body_repair_manifest']))['positions'][0]['model']
    endpoint=contract['opening_requests'][next(iter(contract['opening_requests']))][0]['endpoint']
    require(base['name']=='deepseek-v4-pro' and base['base_url']==endpoint,'source_model_changed')
    from persona_runtime.provider_policy import require_generation_provider
    require_generation_provider(base)
    return {'spec':spec,'character':character,'source_private_id':record['id'],'source_persona_id':row[0],
            'source_character_version':contract['character_version'],'contract_sha256':checksum(contract_path),
            'model':{k:base[k] for k in ('base_url','name','max_tokens','timeout','max_input_chars')}}


def binding_at(root):
    root=outside_repository(root); paths=RuntimePaths(safe(root/'home',root))
    data=read_private(paths.home/MANIFEST)
    require(data['version']==VERSION and data['study_id']==STUDY and data['baseline']==BASELINE
            and Path(data['home']).resolve()==paths.home and ConfigStore(paths).load()['instance_id']==data['instance_id'],
            'instance_mismatch')
    require(len(data['sessions'])==2 and len(set(data['sessions']))==2,'invalid_sessions')
    store=Store(paths)
    with store.connection() as db:
        pid=data['persona_id']
        require(store.current(db)==pid and db.execute(
                'SELECT count(*) FROM sessions WHERE id IN (?,?) AND persona_id=? AND user_id=?',
                (*data['sessions'],pid,LOCAL_USER_ID)).fetchone()[0]==2,'scope_changed')
        row=db.execute('SELECT spec_json FROM personas WHERE id=?',(pid,)).fetchone()
    require(row and fingerprint(json.loads(row[0]))==data['spec_hash'] and
            fingerprint(CharacterService(store).current(pid)['data'])==data['character_hash'],'visible_material_changed')
    registry=PrivatePersonas(paths).record_for_persona(pid)
    require(registry and registry.get('_copy_binding')==data['permission_binding'] and registry['approved_models']=={},
            'private_binding_changed')
    cfg=ConfigStore(paths).load()
    require(not cfg['owner_qq'] and not cfg['bot_qq'] and not cfg['organization']['daily_enabled']
            and not cfg['organization']['reuse_chat'] and not cfg['character_selector']['enabled'] and
            cfg['model']['name']=='deepseek-v4-pro','unexpected_runtime_configuration')
    return paths,data


def write_launcher(root):
    def arg(value):
        value=str(value)
        require(not any(c in value for c in ('"','\n','\r')),'launcher_path_invalid')
        return '"'+value.replace('%','%%')+'"'
    program=Path(__file__).resolve().parents[1]
    cmd='\r\n'.join(['@echo off','chcp 65001 >nul','cd /d '+arg(program),
        arg(sys.executable)+' -B -X utf8 -m experiments.en5_experience_setup launch --root '+arg(root),
        'if errorlevel 1 pause',''])
    (root/'启动连续体验.cmd').write_bytes(cmd.encode('utf-8'))


def prepare(root,state_path,source_home,*,mode='author_desktop'):
    require(mode in ('author_desktop','author_material_replay'),'mode_invalid')
    root=outside_repository(root); root.mkdir(parents=True,exist_ok=True)
    final=safe(root/'home',root); source_home=safe(source_home)
    require(final!=source_home and not source_home.is_relative_to(final),'overlap')
    source=material_source(state_path,source_home)
    with closing(InstanceLock(root/'prepare.lock')):
        if not final.exists():
            with TemporaryDirectory(dir=root,prefix='prepare-') as work:
                stage=Path(work)/'home'; paths=RuntimePaths(stage)
                cfg=ConfigStore(paths); cfg.initialize()
                cfg.update({'model':source['model'],'organization':{'daily_enabled':False,'reuse_chat':False}})
                store=Store(paths);store.initialize();service=RuntimeService(store)
                pid=service.create_persona(PersonaSpec.from_dict(source['spec']))['id']
                service.use_persona(pid)
                chars=CharacterService(store);chars.update(pid,source['character'],chars.current(pid)['id'],identifier())
                sessions=bind_sessions(paths,pid)
                instance=cfg.load()['instance_id']
                private_binding=fingerprint([STUDY,str(final),instance,pid,source['source_private_id']])
                private=PrivatePersonas(paths)
                # Derived copy has no inherited authorizations. Only explicit grants for its own purposes are usable.
                private.save_record({'id':identifier(),'persona_id':pid,'state':'en5_visible_copy',
                    'source_private_id':source['source_private_id'],'_copy_binding':private_binding,
                    'approved_models':{},'acceptance_on_original':False})
                with store.connection(write=True) as db:
                    db.execute('UPDATE persona_lifecycle SET privacy_root=? WHERE persona_id=?',(pid,pid))
                manifest={'version':VERSION,'study_id':STUDY,'purpose':'desktop-experience','home':str(final),
                    'instance_id':instance,'persona_id':pid,'sessions':sessions,'baseline':BASELINE,'mode':mode,
                    'source_home':str(source_home),'source_state':str(outside_repository(state_path)),
                    'source_persona_id':source['source_persona_id'],'source_character_version':source['source_character_version'],
                    'source_contract_sha256':source['contract_sha256'],'spec_hash':fingerprint(source['spec']),
                    'character_hash':fingerprint(source['character']),'permission_binding':private_binding}
                atomic_json(stage/MANIFEST,manifest)
                stage.rename(final)
        paths,binding=binding_at(root)
        require(binding['mode']==mode and binding['source_home']==str(source_home) and binding['source_contract_sha256']==source['contract_sha256']
                and binding['character_hash']==fingerprint(source['character']),'source_binding_changed')
        save_once(root/'provider-review.private.json',{'knowledge_router':_review_source(root,binding)})
        write_launcher(root)
        return binding


def _review_source(root, state):
    paths=RuntimePaths(Path(state['source_home']))
    # Only provider names and endpoint identities become part of the review scope.
    data=read_private(paths.home/'private/knowledge-router/config.json')
    from urllib.parse import urlsplit
    parsed=urlsplit(data['base_url'])
    require(parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and isinstance(data['name'],str) and data['name'],'invalid_jev')
    return {k:data[k] for k in ('name','base_url')}


def use_scope(root,mode='author_desktop',material_sha256=None):
    paths,binding=binding_at(root)
    return _scope(paths,binding,mode,material_sha256,read_private(outside_repository(root)/'provider-review.private.json')['knowledge_router'])


def _scope(paths,binding,mode,material_sha256,router):
    require(mode==binding['mode'] and mode in ('author_desktop','author_material_replay'),'wrong_input_mode')
    if mode=='author_material_replay':
        require(isinstance(material_sha256,str) and len(material_sha256)==64 and
                all(c in '0123456789abcdef' for c in material_sha256),'material_hash_required')
    else:require(material_sha256 is None,'unexpected_material')
    speech=ConfigStore(paths).load()['model']
    return {'version':'en5-author-use-v1','study_id':binding['study_id'],'home':str(paths.home),
            'instance_id':binding['instance_id'],'persona_id':binding['persona_id'],'sessions':binding['sessions'],
            'mode':mode,'material_sha256':material_sha256,'baseline':binding['baseline'],
            'scope_sha256':checksum(paths.home/MANIFEST),'character_sha256':binding['character_hash'],
            'providers':{'main':{k:speech[k] for k in ('name','base_url')},'knowledge_router':router,
                         'knowledge_native':{k:v for k,v in flash_model(speech).items() if k in ('name','base_url')}},
            'visible_character_only':True,'own_presented_history':True,'public_projection_only_for_native':True,
            'parts':2,'turns_per_part':15,'formal_deployment':False}


def validate_use(root, *, material_sha256=None, require_live=False):
    root=outside_repository(root)
    paths,binding=binding_at(root)
    receipt=root/'author-use.private.json'
    require(receipt.is_file(),'specific_use_approval_required')
    approved=read_private(receipt)
    if material_sha256 is None and binding['mode']=='author_material_replay':
        material_sha256=checksum(root/'materials.private.json')
    route=read_private(root/'provider-review.private.json')
    expected=_scope(paths,binding,binding['mode'],material_sha256,route['knowledge_router'])
    require(all(approved.get(k)==v and k in approved for k,v in expected.items()) and approved.get('approved') is True
            and isinstance(approved.get('author_message'),str) and approved['author_message'].strip(),'approval_scope_changed')
    if binding['mode']=='author_material_replay':
        require(checksum(root/'materials.private.json')==material_sha256 and
                checksum(root/'input-contract.private.json')==approved.get('input_contract_sha256'),'input_review_changed')
    if require_live:
        active=read_private(root/'activation.private.json')
        require(active.get('approval_sha256')==checksum(receipt) and active.get('instance_id')==binding['instance_id'],
                'activation_changed')
    return checksum(receipt)


def activate(root,approval_path=None):
    root=outside_repository(root)
    require(approval_path is None or outside_repository(approval_path)==root/'author-use.private.json','approval_location')
    grant_hash=validate_use(root)
    paths,binding=binding_at(root)
    source=RuntimePaths(Path(binding['source_home']))
    receipt=read_private(root/'author-use.private.json')
    with closing(InstanceLock(root/'prepare.lock')), closing(InstanceLock(paths.home/'runtime/service.lock')):
        store=Store(paths);cfg=ConfigStore(paths)
        def check_grants():
            model=cfg.load()['model']; router=configured_model(paths)
            for purpose,route in [('main',model),('knowledge_router',router),('knowledge_native',flash_model(model))]:
                require(provider_identity(route)==provider_identity(receipt['providers'][purpose]),'provider_changed')
                guard_cloud_dispatch(store,binding['persona_id'],purpose,route)
        if (root/'activation.private.json').exists():
            validate_use(root,require_live=True)
            check_grants()
            return read_private(root/'activation.private.json')
        material_source(Path(binding['source_state']),source.home)
        original=ConfigStore(source).load()['model']
        from persona_runtime.provider_policy import require_generation_provider
        require_generation_provider(original)
        require(original.get('encrypted_key') and original['base_url'].rstrip('/').removesuffix('/v1')==
                cfg.load()['model']['base_url'].rstrip('/').removesuffix('/v1'),'credential_source_changed')
        router=configured_model(source)
        require(provider_identity(router)==provider_identity(receipt['providers']['knowledge_router']),'router_changed')
        target=cfg.load()
        target['model']['encrypted_key']=original['encrypted_key']
        cfg.validate(target)
        atomic_json(cfg.file,target)
        atomic_json(paths.home/'private/knowledge-router/config.json',{k:router[k] for k in ('base_url','name','encrypted_key')})
        private=PrivatePersonas(paths);pid=binding['persona_id']
        slots_hash=None
        if binding['mode']=='author_material_replay':
            from .en5_experience_replay import plan_for
            from persona_runtime.knowledge_native import public_task
            material=read_private(root/'materials.private.json')
            slots={}
            for part,sid in enumerate(binding['sessions'],1):
                groups=plan_for(material,binding,part,receipt['material_sha256'])
                slots[sid]=[{'request_id':b['request_id'],'n':b['n'],'i':b['i'],'text_sha256':digest(b['text'].encode('utf-8')),
                             'public_task':public_task(b['public_task']) if b['public_task'] else None}
                            for members in groups for b in members]
            slots_hash=save_once(root/'approved-slots.private.json',{'approval_sha256':grant_hash,
                'instance_id':binding['instance_id'],'sessions':slots})
        for kind in ('main','knowledge_router','knowledge_native'):
            private.authorize_model(store,pid,kind,provider_identity(receipt['providers'][kind]),STUDY+'-'+kind)
        record=private.record_for_persona(pid)
        require(record.get('en5_approval_sha256') in (None,grant_hash),'previous_approval_changed')
        private.save_record(record | {'en5_approval_sha256':grant_hash,'en5_slots_sha256':slots_hash})
        check_grants()
        from persona_runtime.chat import ChatEngine
        from persona_runtime.organization import OrganizationEngine
        chat=ChatEngine(store,cfg)
        org=OrganizationEngine(store,cfg,chat.client,chat,foreground_only=True)
        try:
            current=chat.knowledge.current_policy(pid);processing=org.processing.policy(pid);web_policy=org.web.policy(pid)
            if not (current['data']['enabled'] and processing['data']['enabled'] and web_policy['data']['enabled']):
                org.processing.enable_for_persona(pid,True,current['revision'],processing['revision'],web_policy['revision'],STUDY+'-enable')
        finally:chat.knowledge.close()
        result={'version':'en5-activation-v1','instance_id':binding['instance_id'],'approval_sha256':grant_hash,
                'purposes':list(receipt['providers']),'new_model_requests':0}
        save_once(root/'activation.private.json',result)
        return result


async def run_desktop(root,part=None,driver=None,*,material_sha256=None):
    """Driver receives the mapped normal Tk Workbench and its real local application."""
    from persona_runtime.desktop import Workbench
    from persona_runtime.server import create_app
    root=outside_repository(root); paths,binding=binding_at(root)
    if binding['mode']=='author_material_replay':
        require(driver is not None and material_sha256 is not None,'reviewed_replay_driver_required')
    validate_use(root,material_sha256=material_sha256,require_live=True)
    current=material_source(Path(binding['source_state']),Path(binding['source_home']))
    require(current['contract_sha256']==binding['source_contract_sha256'] and
            fingerprint(current['character'])==binding['character_hash'],'visible_source_changed')
    cfg=ConfigStore(paths);store=Store(paths)
    permits={'main':cfg.load()['model'],'knowledge_router':configured_model(paths),
             'knowledge_native':flash_model(cfg.load()['model'])}
    approved=read_private(root/'author-use.private.json')
    for kind,model in permits.items():
        require(provider_identity(model)==provider_identity(approved['providers'][kind]),'provider_changed')
        guard_cloud_dispatch(store,binding['persona_id'],kind,model)
    with ExitStack() as stack:
        for name in ('desktop','service'):
            stack.enter_context(closing(InstanceLock(paths.home/'runtime'/(name+'.lock'))))
        if part is None:
            with store.connection() as db:
                count=db.execute("SELECT count(*) FROM turns WHERE session_id=? AND status='completed' AND reply_event_id IS NOT NULL",
                                 (binding['sessions'][0],)).fetchone()[0]
            part=2 if count>=15 else 1
        sid=select_part(paths,binding,part)
        sock=stack.enter_context(closing(socket.socket(socket.AF_INET,socket.SOCK_STREAM)))
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        sock.bind(('127.0.0.1',0))
        cfg.update({'service_port':sock.getsockname()[1]})
        app=create_app(paths,foreground_only=True)
        runner=web.AppRunner(app,access_log=None)
        window=task=None
        try:
            await runner.setup();await web.SockSite(runner,sock).start()
            window=Workbench(paths)
            window.title('角色 · 连续体验 · 第'+str(part)+'段')
            window.show_page('chat')
            window.load_chat()
            while not window.closed:
                window.update()
                if window.closed:break
                if driver and not task and window.session_id==sid and window.persona_id==binding['persona_id'] and window.chat_persona.current()>=0 and window.transcript.winfo_viewable():
                    task=asyncio.create_task(driver(window,app,binding,part))
                if task and task.done():return task.result()
                await asyncio.sleep(.015)
            if task and not task.done():raise RuntimeErrorBase('en5_window_closed_before_driver_finished')
            if task:return task.result()
        finally:
            if task and not task.done():
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            if window:
                if not window.closed:window.destroy_console()
                await asyncio.to_thread(window.hub.pool.shutdown,wait=True,cancel_futures=True)
            app['engine'].draining=True
            for task in list(app['engine'].tasks.values()):
                if not task.done():task.cancel('desktop_closed')
            await runner.cleanup()


def main(argv=None):
    cli=argparse.ArgumentParser(description=__doc__)
    actions=cli.add_subparsers(dest='command',required=True)
    prep=actions.add_parser('prepare');prep.add_argument('--root',type=Path,required=True)
    prep.add_argument('--state',type=Path,required=True);prep.add_argument('--source-home',type=Path,required=True)
    prep.add_argument('--mode',choices=('author_desktop','author_material_replay'),default='author_desktop')
    for op in ('status','activate','launch'):
        sub=actions.add_parser(op);sub.add_argument('--root',type=Path,required=True)
        if op=='launch':sub.add_argument('--part',type=int,choices=(1,2))
    args=cli.parse_args(argv)
    if args.command=='prepare': result=prepare(args.root,args.state,args.source_home,mode=args.mode)
    elif args.command=='activate':result=activate(args.root)
    elif args.command=='launch':
        _,binding=binding_at(args.root)
        if binding['mode']=='author_material_replay':
            from .en5_experience_replay import run
            asyncio.run(run(args.root,args.part))
        else:asyncio.run(run_desktop(args.root,args.part))
        return
    else:
        _,result=binding_at(args.root)
    print(json.dumps({k:result[k] for k in ('version','instance_id','mode','sessions','baseline','new_model_requests')
                      if k in result},ensure_ascii=False))


if __name__=='__main__':
    main()
