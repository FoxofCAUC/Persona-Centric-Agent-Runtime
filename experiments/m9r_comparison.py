"""Finite public validation on real engines and existing attempt ledgers.

New positions use two-stage only after the author's original-output review.
Historical comparison wire helpers and read-only reports remain available;
completed or unknown calls are never rerun to improve presentation.
"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from decimal import Decimal

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine, ChatCompletionsClient, ModelCompletion, ModelError
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.deepseek_native_contract import VERSION, CONFLICTS, official_endpoint
from persona_runtime.generation_retry import audited_complete
from persona_runtime.knowledge_routing import configured_model
from persona_runtime.models import PersonaSpec, identifier, timestamp
from persona_runtime.network_proxy import outbound_proxy
from persona_runtime.organization import OrganizationEngine
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_compat import build_request, describe_request
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from .m9r_comparison_materials import SPEC,SCENARIOS,VERSION as MATERIALS_VERSION

REPO=Path(__file__).resolve().parents[1]
COMPARISON_VERSION='m9r4-public-comparison-v1'


def newly_acquired_material(record):
    retrieval=record.get('retrieval',{})
    if retrieval.get('executor')==VERSION:return retrieval.get('material_acquired') is True
    return bool(retrieval.get('needed') and record.get('sources'))


def root_path(value):
    path=Path(value).resolve()
    if path==REPO or REPO in path.parents:raise ValueError('comparison_data_outside_source_required')
    return path


def hashes():
    files=list((REPO/'persona_runtime').glob('*.py'))+[Path(__file__),Path(__file__).with_name('m9r_comparison_materials.py'),Path(__file__).with_name('m9r_fatigue_materials.py')]
    return {str(path.relative_to(REPO)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def protected(home):
    names=('config/workbench.json','private/knowledge-router/config.json','private/personas/registry.json')
    return {name:hashlib.sha256((home/name).read_bytes()).hexdigest() for name in names if (home/name).is_file()}


def prepare(root,source_home,*,simulated=False,scenario_ids=None):
    root=root_path(root)
    if root.exists() and any(root.iterdir()):raise ValueError('comparison_root_must_be_empty')
    source=RuntimePaths.from_environment(source_home);main=ConfigStore(source).load()['model']
    official_endpoint(main)
    jev=configured_model(source)
    if not simulated:
        outbound_proxy(official_endpoint(main))
        if not main.get('encrypted_key') or not jev.get('encrypted_key'):raise ValueError('comparison_credentials_required')
    root.mkdir(parents=True,exist_ok=True)
    base=Store(RuntimePaths(root/'base'));base.initialize();service=RuntimeService(base)
    persona=service.create_persona(PersonaSpec.from_dict(SPEC));service.use_persona(persona['id'])
    scenarios=[row for row in SCENARIOS if scenario_ids is None or row['id'] in scenario_ids]
    if scenario_ids is not None and {row['id'] for row in scenarios}!=set(scenario_ids):raise ValueError('comparison_scenario_unknown')
    positions=[]
    for scenario in scenarios:
        for index,_ in enumerate(scenario['turns']):
            for shape in ('two_stage',):
                positions.append({'id':scenario['id']+'-'+str(index+1)+'-'+shape,'scenario':scenario['id'],'turn':index,'shape':shape})
    plan={'version':COMPARISON_VERSION,'created_at':timestamp(),'kind':'simulation' if simulated else 'real',
        'source_home':str(source.home),'source_hashes':hashes(),'protected_before':protected(source.home),
        'materials_version':MATERIALS_VERSION,'spec':SPEC,'spec_hash':fingerprint(SPEC),'scenarios':scenarios,
        'positions':positions,'persona_id':persona['id'],'order':'two_stage only; single-stage closed by author after original review',
        'base_database_sha256':hashlib.sha256(base.path.read_bytes()).hexdigest(),
        'baseline':'retained v0.1 behavior under current transport/accounting fixes; no old paid replay',
        'max_primary_positions':len(positions),'observed_stop_tokens':2000000,'observed_stop_tool_units':300,
        'hard_money_ceiling':None,'stop_policy':'between requests; native internal work is not hard-capped',
        'retry_policy':'existing known-failure retries only; success and unknown never replayed',
        'formal_default_switch':False,'user_taste_assessment':'not_prefilled'}
    atomic_json(root/'plan.json',plan);atomic_json(root/'plan-hash.json',{'sha256':fingerprint(plan)})
    return {'prepared':True,'kind':plan['kind'],'primary_positions':len(positions),'paid_calls':0}


def load_plan(root,*,check_source=True):
    plan=json.loads((root/'plan.json').read_text(encoding='utf-8'))
    if fingerprint(plan)!=json.loads((root/'plan-hash.json').read_text(encoding='utf-8'))['sha256']:
        raise ValueError('comparison_plan_changed')
    catalog={row['id']:row for row in SCENARIOS}
    if (plan['version']!=COMPARISON_VERSION or plan['spec']!=SPEC
            or any(row!=catalog.get(row['id']) for row in plan['scenarios'])):
        raise ValueError('comparison_materials_changed')
    if check_source and plan['source_hashes']!=hashes():raise ValueError('comparison_source_changed')
    return plan


class SimulatedClient:
    async def complete(self,model,messages):
        text='我会先把节奏放慢，先试最省事的一步。'
        if '_native_web_profile' in model:
            from persona_runtime.deepseek_native_response import NativeResponse
            tool=model['_native_web_profile']['tools']!='none'
            native={'model':'deepseek-flash','choices':[{'message':{'role':'assistant','content':text,
                **({'server_tool_calls':[{'index':0,'calls':[{'index':0,'id':'simulated','type':'deepseek_read_url',
                    'kind':'read_link','status':'completed','action':{'type':'read_link','url':'https://example.org/'},
                    'page':{'url':'https://example.org/','title':'人工公开材料'},'signature':'simulated-signature'}]}]} if tool else {})},
                'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,
                    **({'server_tool_use':{'tool_search':0,'tool_fetch':1,'tool_image_search':0}} if tool else {})}}
            parser=NativeResponse();parser.consume(native);value=parser.complete()
            value.diagnostics.update(network_route='local_simulation',request_headers_sent=False,response_headers_received=True)
            return value
        return ModelCompletion(text,{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},
            {'network_route':'local_simulation','request_headers_sent':False,'finish_reason':'stop'})


class PrimaryExecution:
    def __init__(self,runner,shape,scenario):
        self.runner=runner;self.shape=shape;self.scenario=scenario;self.current=None;self.turn_id=None
        self.decision=None;self.last_packet=None;self.last_reply=None

    def get(self,engine,db,turn):
        # The existing evaluation hook also serves an older three-arm ablation
        # interface. This comparison keeps the original v8 persona rendering.
        return None

    def actual(self,model,messages):
        actual=deepcopy(model);wire_messages=deepcopy(messages)
        if self.shape!='single_stage':return actual,wire_messages
        need=bool((self.decision or {}).get('need_web'))
        if not need and self.last_packet is None:return actual,wire_messages
        # Single-stage is allowed only for this frozen public/artificial spec.
        if self.runner.plan['spec_hash']!=fingerprint(SPEC):raise ValueError('single_stage_public_fixture_required')
        actual={key:value for key,value in actual.items() if key not in CONFLICTS and key!='_reply_mode'}
        task=self.scenario['turns'][self.current['turn']].get('public_task',{})
        actual.update(max_tokens=8192,_reply_mode='legacy_full',_native_web_profile={
            'version':VERSION,'mode':task.get('depth','ordinary') if need else 'ordinary',
            'tools':('read_link' if task.get('intent')=='read_link' else 'search') if need else 'none','stream':True})
        if self.last_packet:
            for index,message in enumerate(wire_messages):
                if message.get('role')=='assistant' and message.get('content')==self.last_reply:
                    proposed=deepcopy(wire_messages);proposed[index]=deepcopy(self.last_packet['assistant'])
                    if len(json.dumps(proposed,ensure_ascii=False))<=actual['max_input_chars']:wire_messages=proposed
        return actual,wire_messages

    def before_dispatch(self,engine,turn,prepared):
        frozen=json.loads(turn['model_snapshot'])
        frozen.update(_reply_mode=turn.get('reply_mode_snapshot'),_expression_snapshot=turn.get('expression_snapshot'))
        actual,messages=self.actual(frozen,prepared['messages'])
        manifest=prepared['manifest'];manifest['comparison_base_model_hash']=manifest.get('model_snapshot_hash')
        manifest['provider_request_contract']=describe_request(actual)
        manifest['model_snapshot_hash']=fingerprint({key:value for key,value in actual.items() if key!='encrypted_key'})
        manifest['input_snapshot_hash']=fingerprint(messages)
        manifest['comparison']={'version':COMPARISON_VERSION,'shape':self.shape,'public_fixture':True,
            'decision_reference':deepcopy(self.decision),'actual_wire_sidecar':self.current['id']+'/primary-request.json'}

    async def __call__(self,client,model,messages,**kwargs):
        actual,wire_messages=self.actual(model,messages)
        kwargs['paths']=self.runner.engines[(self.scenario['id'],self.shape)]['paths']
        folder=self.runner.root/'positions'/self.current['id']
        payload,contract=build_request(actual,wire_messages)
        atomic_json(folder/'primary-request.json',{'payload':payload,'contract':contract,
            'base_model':{key:value for key,value in model.items() if key!='encrypted_key'},
            'actual_model':{key:value for key,value in actual.items() if key!='encrypted_key'},
            'decision_reference':self.decision})
        try:
            value=await audited_complete(client,actual,wire_messages,**kwargs)
        except BaseException as error:
            native=getattr(error,'native',None)
            if native is not None:atomic_json(folder/'primary-protocol.json',native)
            raise
        atomic_json(folder/'primary-response.json',{'text':value[0],'usage':value[1],
            'diagnostics':value.diagnostics})
        if value.native is not None:
            atomic_json(folder/'primary-protocol.json',value.native)
            self.last_packet=deepcopy(value.native);self.last_reply=value[0]
        return value


class Runner:
    def __init__(self,root,plan,*,resuming=False):
        self.root=root;self.plan=plan;self.engines={};self.shared_decisions={};self.resuming=resuming
        if resuming:
            for position in plan['positions']:
                file=root/'positions'/position['id']/'result.json'
                if position['shape']=='two_stage' and file.exists():
                    value=json.loads(file.read_text(encoding='utf-8'))
                    decision=deepcopy(value['record'].get('retrieval',{}).get('web_decision',{}))
                    decision.update(reference_turn_id=value['turn_id'],reference_position=position['id'])
                    self.shared_decisions[(position['scenario'],position['turn'])]=decision

    def make_engine(self,scenario,shape):
        key=(scenario['id'],shape)
        if key in self.engines:return self.engines[key]
        paths=RuntimePaths(self.root/'instances'/scenario['id']/shape)
        exists=paths.home.exists()
        if exists and not self.resuming:raise ValueError('comparison_instance_exists_reconcile_first')
        paths.initialize();store=Store(paths)
        base=Store(RuntimePaths(self.root/'base'))
        if hashlib.sha256(base.path.read_bytes()).hexdigest()!=self.plan['base_database_sha256']:
            raise ValueError('comparison_public_base_changed')
        if not exists:
            with Store(RuntimePaths(self.root/'base')).connection() as source:
                with sqlite3.connect(store.path) as target:source.backup(target)
        if fingerprint(RuntimeService(store).show_persona(self.plan['persona_id'])['spec'])!=self.plan['spec_hash']:
            raise ValueError('comparison_instance_persona_changed')
        cfg=ConfigStore(paths);cfg.initialize()
        source_paths=RuntimePaths(Path(self.plan['source_home']))
        main=ConfigStore(source_paths).load()['model'];jev=configured_model(source_paths)
        value=cfg.load();value['model']=deepcopy(main)
        if self.plan['kind']=='simulation':value['model']['encrypted_key']=''
        atomic_json(cfg.file,value)
        atomic_json(paths.home/'private/knowledge-router/config.json',{
            **{name:jev[name] for name in ('base_url','name','encrypted_key')},
            **({'encrypted_key':'local-simulation'} if self.plan['kind']=='simulation' else {})})
        client=SimulatedClient() if self.plan['kind']=='simulation' else ChatCompletionsClient()
        execution=PrimaryExecution(self,shape,scenario)
        if exists:
            for position in sorted(self.plan['positions'],key=lambda row:row['turn']):
                folder=self.root/'positions'/position['id']
                if position['scenario']==scenario['id'] and position['shape']==shape and (folder/'primary-protocol.json').exists() and (folder/'result.json').exists():
                    old=json.loads((folder/'result.json').read_text(encoding='utf-8'))
                    if old['status']=='completed':
                        execution.last_packet=json.loads((folder/'primary-protocol.json').read_text(encoding='utf-8'))
                        execution.last_reply=old['reply']
        chat=ChatEngine(store,cfg,client,generation_executor=execution)
        chat.evaluation_session=execution
        # Preserve local diagnostic traces for harness/setup errors; the core
        # intentionally exposes only body-free error codes to chat users.
        import traceback
        def observed(function):
            def call(*args,**kwargs):
                try:return function(*args,**kwargs)
                except Exception:
                    folder=self.root/'positions'/execution.current['id']
                    with (folder/'setup-error.log').open('a',encoding='utf-8') as output:output.write(traceback.format_exc())
                    raise
            return call
        chat.cognitive.prepare=observed(chat.cognitive.prepare)
        chat.cognitive.finalize=observed(chat.cognitive.finalize)
        execution.before_dispatch=observed(execution.before_dispatch)
        org=OrganizationEngine(store,cfg,client,chat)
        pid=self.plan['persona_id']
        reader=chat.knowledge.current_policy(pid);processing=org.processing.policy(pid);web=org.web.policy(pid)
        enabled=shape!='single_stage' and not scenario.get('network_disabled')
        org.processing.enable_for_persona(pid,enabled,reader['revision'],processing['revision'],web['revision'],identifier())
        if self.plan['kind']=='simulation':
            async def decision(model,body):
                index=execution.current['turn'];need=scenario['expected_new_material'] and index==0
                if scenario.get('fatigue_materials_version'):
                    need=scenario['turns'][index].get('observation','').startswith(('new_web','new_link','explicit_new_web'))
                return ModelCompletion(json.dumps({'answers':{'route':{'choice':'web' if need else 'none'}}}),
                    {'prompt_tokens':5,'completion_tokens':1,'total_tokens':6},{'network_route':'local_simulation'})
            async def tool(stage,args,model):
                data={'results':[{'title':'人工公开资料','url':'https://example.org/','content':'人工可读正文。'*50}]} if stage=='search' else {'content':'人工保留正文。'*100,'title':'人工网页'}
                return ModelCompletion(json.dumps(data),{'tool':stage,'billable':False,'amount_usd':'0'},
                    {'network_route':'local_simulation','request_headers_sent':False})
            org.routing.transport=decision;org.processing.tools.transport=tool
        worker=asyncio.create_task(org.worker())
        item={'paths':paths,'store':store,'cfg':cfg,'chat':chat,'org':org,'worker':worker,'execution':execution}
        self.engines[key]=item
        return item

    async def position(self,position):
        scenario=next(row for row in self.plan['scenarios'] if row['id']==position['scenario'])
        folder=self.root/'positions'/position['id'];folder.mkdir(parents=True,exist_ok=False)
        atomic_json(folder/'reserved.json',{'position':position,'at':timestamp(),'kind':self.plan['kind']})
        item=self.make_engine(scenario,position['shape']);execution=item['execution'];execution.current=position
        reference=self.shared_decisions.get((scenario['id'],position['turn']))
        if position['shape']=='single_stage':
            if reference is None:raise ValueError('comparison_jev_reference_required')
            execution.decision=reference
        task=scenario['turns'][position['turn']]
        started=time.monotonic()
        turn=await item['chat'].submit('desktop',task['text'],position['id'],
            public_task=task.get('public_task') if position['shape']=='two_stage' else None)
        execution.turn_id=turn['id'];await item['chat'].wait()
        finished=item['chat'].get(turn['id']);wall=round((time.monotonic()-started)*1000,3)
        with item['store'].connection() as db:
            raw=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.conversation.record.'+turn['id'],)).fetchone()
            record=json.loads(raw[0])['data'] if raw else {}
            attempts=[dict(row) for row in db.execute('SELECT id,status,error_code,usage_json,started_epoch FROM attempts WHERE turn_id=?',(turn['id'],))]
            tasks=[dict(row) for row in db.execute("SELECT * FROM organization_tasks WHERE json_extract(payload_json,'$.knowledge_processing.root_turn_id')=?",(turn['id'],))]
            org_attempts=[]
            for row in tasks:
                for attempt in db.execute('SELECT id,status,error_code,usage_json FROM organization_attempts WHERE task_id=?',(row['id'],)):
                    packet=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.native.receipt.'+attempt['id'],)).fetchone()
                    outcome=json.loads(packet[0])['data']['packet']['outcome'] if packet else None
                    org_attempts.append(dict(attempt)|{'stage':json.loads(row['payload_json'])['knowledge_processing']['stage'],'native_terminal':outcome})
            manifests=[json.loads(row[0]) for row in db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=?',(turn['id'],))]
        if position['shape']=='two_stage':
            decision=deepcopy(record.get('retrieval',{}).get('web_decision',{'need_web':False,'decision_source':'disabled'}))
            decision['reference_turn_id']=turn['id'];decision['reference_position']=position['id']
            self.shared_decisions[(scenario['id'],position['turn'])]=decision
        single_protocol=json.loads((folder/'primary-protocol.json').read_text(encoding='utf-8')) if (folder/'primary-protocol.json').exists() else {}
        single_calls=[call for step in single_protocol.get('assistant',{}).get('server_tool_calls',[]) for call in step.get('calls',[])]
        single_acquired=any(call.get('status')=='completed' and (call.get('sources') or call.get('page')) for call in single_calls)
        acquired=single_acquired if position['shape']=='single_stage' else newly_acquired_material(record)
        result={'position':position,'kind':self.plan['kind'],'turn_id':turn['id'],'status':finished['status'],
            'error_code':finished['error_code'],'reply':finished.get('reply'),'display_reply':finished.get('display_reply'),
            'usage':finished.get('usage'),'reply_available_ms':wall,'timings':finished.get('timings'),
            'record':record,'search_notice':finished.get('search_notice'),'attempts':attempts,'organization_attempts':org_attempts,'manifests':manifests,
            'single_protocol_outcome':single_protocol.get('outcome'),'decision_reference':execution.decision,
            'new_material_acquired':acquired,'single_tool_calls':len(single_calls),
            'user_taste':'not_assessed','physical_calls_replayed':False}
        atomic_json(folder/'result.json',result)
        print(json.dumps({'position':position['id'],'status':result['status'],'kind':self.plan['kind']}),flush=True)
        if result['status']!='completed':raise RuntimeError('comparison_position_not_completed')
        declared=json.loads((folder/'primary-request.json').read_text(encoding='utf-8'))
        if manifests and (manifests[-1]['provider_request_contract']!=declared['contract'] or
                manifests[-1]['model_snapshot_hash']!=fingerprint(declared['actual_model'])):
            raise RuntimeError('comparison_actual_wire_manifest_mismatch')
        if any(row['stage']=='conversation_native' and row['status']!='completed' for row in org_attempts):
            raise RuntimeError('comparison_native_auxiliary_not_completed')
        if position['shape']=='two_stage' and position['turn']==0 and scenario['expected_new_material'] and not acquired:
            raise RuntimeError('comparison_required_two_stage_material_missing')
        return result

    async def close(self):
        for item in self.engines.values():
            item['org'].draining=True;item['chat'].draining=True;item['chat'].knowledge.close()
            item['worker'].cancel();await asyncio.gather(item['worker'],return_exceptions=True)
            await item['chat'].wait()


async def run(root,*,resuming=False,repair_note=None):
    root=root_path(root);plan=load_plan(root,check_source=not resuming)
    if any(position['shape']!='two_stage' for position in plan['positions']):
        raise ValueError('single_stage_closed_by_author_use_historical_report_only')
    if protected(Path(plan['source_home']))!=plan['protected_before']:raise ValueError('comparison_source_configuration_changed')
    if (root/'positions').exists() and not resuming:raise ValueError('comparison_already_reserved_use_report')
    if resuming:
        if not repair_note:raise ValueError('comparison_repair_note_required')
        for folder in (root/'positions').iterdir():
            if not (folder/'result.json').is_file():raise ValueError('comparison_reserved_position_requires_reconciliation')
            value=json.loads((folder/'result.json').read_text(encoding='utf-8'))
            if value['status']!='completed':raise ValueError('comparison_failed_primary_requires_new_position')
        prior=report(root)
        if prior['usage_unknown_attempts'] or prior['unknown_tool_usage_attempts']:
            raise ValueError('comparison_unknown_effect_requires_independent_followup')
        atomic_json(root/('code-repair-'+identifier()+'.json'),{'at':timestamp(),'reason':repair_note,
            'original_source_hashes':plan['source_hashes'],'accepted_source_hashes':hashes(),
            'completed_positions_replayed':False,'authorization':'active M9R.1-.4 development objective'})
    with (root/'run.lock').open('x',encoding='utf-8') as lock:lock.write(timestamp())
    runner=Runner(root,plan,resuming=resuming);result=None
    try:
        for position in plan['positions']:
            if resuming and (root/'positions'/position['id']/'result.json').exists():continue
            observed=report(root)
            if (observed['known_total_tokens']>=plan['observed_stop_tokens']
                    or sum(observed['billable_tools_known'].values())>=plan['observed_stop_tool_units']):
                raise RuntimeError('comparison_observed_budget_stop')
            await runner.position(position)
        result=report(root);atomic_json(root/'report.json',result);return result
    except BaseException as error:
        atomic_json(root/'stop.json',{'at':timestamp(),'type':type(error).__name__,
            'reason':str(error) if str(error).startswith('comparison_') else 'execution_stopped','resume_by_replay':False})
        raise
    finally:
        await runner.close();cleanup_credentials(root);(root/'run.lock').unlink()
        if result is not None:
            result.update(report(root));atomic_json(root/'report.json',result)


def cleanup_credentials(root):
    """Only this comparison's owned temporary copies; never formal credentials."""
    root=root_path(root);owned=(root/'instances').resolve();changed=0
    for file in (root/'instances').glob('*/*/config/workbench.json'):
        if not file.resolve().is_relative_to(owned):raise ValueError('comparison_cleanup_scope_invalid')
        value=json.loads(file.read_text(encoding='utf-8'));value['model']['encrypted_key']='';atomic_json(file,value)
        paths=RuntimePaths(file.parent.parent)
        jev=paths.home/'private/knowledge-router/config.json'
        if jev.exists():
            if not jev.resolve().is_relative_to(owned):raise ValueError('comparison_cleanup_scope_invalid')
            jev.unlink()
        database=Store(paths).path
        with sqlite3.connect(database) as db,db:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                quoted='"'+table.replace('"','""')+'"'
                columns=[row[1] for row in db.execute('PRAGMA table_info('+quoted+')')]
                for column in columns:
                    if column!='model_snapshot' and not column.endswith('_model_snapshot'):continue
                    q='"'+column.replace('"','""')+'"'
                    for rowid,raw in db.execute('SELECT rowid,'+q+' FROM '+quoted+' WHERE '+q+' IS NOT NULL').fetchall():
                        try:snapshot=json.loads(raw)
                        except (ValueError,TypeError):continue
                        if isinstance(snapshot,dict) and snapshot.get('encrypted_key'):
                            snapshot['encrypted_key']='';db.execute('UPDATE '+quoted+' SET '+q+'=? WHERE rowid=?',(json.dumps(snapshot,ensure_ascii=False),rowid));changed+=1
            for key,raw in db.execute("SELECT key,value FROM knowledge_control WHERE key LIKE 'knowledge.native.policy.%'").fetchall():
                policy=json.loads(raw);policy['data']['enabled']=False
                db.execute('UPDATE knowledge_control SET value=? WHERE key=?',(json.dumps(policy,ensure_ascii=False),key))
        with sqlite3.connect(database) as db:db.execute('VACUUM')
    atomic_json(root/'credential-cleanup.json',{'at':timestamp(),'temporary_model_snapshot_fields_cleared':changed,
        'owned_config_credentials_cleared':True,'formal_credentials_modified':False})


def report(root):
    root=root_path(root);plan=load_plan(root,check_source=False);rows=[];tokens=0;unknown=0;calls=0
    tool_counts={'tool_search':0,'tool_fetch':0,'tool_image_search':0};tool_unknown=0;cny=Decimal(0);usd=Decimal(0)
    from persona_runtime.deepseek_native_contract import tool_accounting
    for position in plan['positions']:
        file=root/'positions'/position['id']/'result.json'
        if not file.exists():rows.append({'id':position['id'],'status':'not_executed_or_reserved'});continue
        value=json.loads(file.read_text(encoding='utf-8'))
        request=json.loads((file.parent/'primary-request.json').read_text(encoding='utf-8')) if (file.parent/'primary-request.json').exists() else {}
        native_primary='_native_web_profile' in request.get('actual_model',{})
        rows.append({'id':position['id'],'status':value['status'],'reply_available_ms':value['reply_available_ms']})
        for attempt in value['attempts']:
            usage=json.loads(attempt['usage_json'] or '{}') or {}
            physical=usage.get('generation_retry',{}).get('attempts',[])
            for item in physical:
                calls+=1;measured=item.get('usage') or {}
                if type(measured.get('total_tokens')) is int:tokens+=measured['total_tokens']
                else:unknown+=1
                if native_primary:
                    account=tool_accounting(measured,terminal=item.get('status')=='completed' or item.get('diagnostics',{}).get('native_terminal')=='failed')
                    for key,count in account['billable'].items():
                        if count is not None:tool_counts[key]+=count
                    if account['estimated_tool_amount'] is None:tool_unknown+=1
                    else:cny+=Decimal(account['estimated_tool_amount'])
        for attempt in value['organization_attempts']:
            calls+=1;usage=json.loads(attempt['usage_json'] or '{}') or {}
            if attempt['stage'] in ('conversation_search','conversation_fetch'):
                if usage.get('estimated_amount_cny') is not None:
                    cny+=Decimal(usage['estimated_amount_cny'])
                    if usage.get('billable'):tool_counts['tool_'+usage['tool']]+=1
                elif usage.get('amount_usd') is not None:usd+=Decimal(usage['amount_usd'])
                else:tool_unknown+=1
                continue
            if type(usage.get('total_tokens')) is int:tokens+=usage['total_tokens']
            else:unknown+=1
            if attempt['stage']=='conversation_native':
                account=tool_accounting(usage,terminal=attempt['status']=='completed' or attempt.get('native_terminal')=='failed')
                for key,count in account['billable'].items():
                    if count is not None:tool_counts[key]+=count
                if account['estimated_tool_amount'] is None:tool_unknown+=1
                else:cny+=Decimal(account['estimated_tool_amount'])
    return {'version':COMPARISON_VERSION,'kind':plan['kind'],'positions':rows,
        'all_positions_completed':all(row['status']=='completed' for row in rows),
        'model_and_independent_tool_attempts':calls,'known_total_tokens':tokens,'usage_unknown_attempts':unknown,
        'billable_tools_known':tool_counts,'unknown_tool_usage_attempts':tool_unknown,
        'known_estimated_tool_amount_cny':str(cny),'legacy_estimated_tool_amount_usd':str(usd),
        'price_version':'deepseek-cny-2026-11-15','temporary_credentials_removed':(root/'credential-cleanup.json').exists(),
        'actual_total_amount':None,'order':plan['order'],'user_taste':'not_assessed',
        'formal_default_unchanged':protected(Path(plan['source_home']))==plan['protected_before']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','run','resume','report'))
    parser.add_argument('--root',required=True);parser.add_argument('--source-home');parser.add_argument('--simulated',action='store_true')
    parser.add_argument('--scenario-ids');parser.add_argument('--repair-note')
    args=parser.parse_args();root=root_path(args.root)
    result=prepare(root,args.source_home,simulated=args.simulated,scenario_ids=args.scenario_ids.split(',') if args.scenario_ids else None) if args.action=='prepare' else asyncio.run(run(root,resuming=args.action=='resume',repair_note=args.repair_note)) if args.action in ('run','resume') else report(root)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
