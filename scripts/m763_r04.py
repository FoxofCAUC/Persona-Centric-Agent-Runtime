"""Controlled R04 development evidence; no main-model or platform dispatch."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import time
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine, ChatCompletionsClient
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.disposition import DispositionService
from persona_runtime.disposition_compile import parse_output
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store

REPO=Path(__file__).resolve().parents[1]
CONTRACT='m763-r04-execution-v1'
FAILURE_STOP_SECONDS=600
AUTHOR='作者设定：建议任务中确实缺少必需参数时，优先询问该缺项；不为可选偏好追问。'


def long_failure(out,state):
    """A single >600s failure latches the paid-test stop across resumes."""
    if state.get('model_tests_stopped'):return state['model_tests_stopped']
    for key,slot in state.get('slots',{}).items():
        if slot.get('state')=='failed' and slot.get('elapsed_ms',0)>FAILURE_STOP_SECONDS*1000:
            return {'reason':'failure_over_600_seconds','slot_id':key,'elapsed_ms':slot['elapsed_ms']}
    for path in sorted((out/'dispatch').glob('*.json')):
        trace=json.loads(path.read_text(encoding='utf-8'))
        if trace.get('state')=='response_unknown' and trace.get('elapsed_ms',0)>FAILURE_STOP_SECONDS*1000:
            return {'reason':'failure_over_600_seconds','attempt_id':trace['attempt_id'],'elapsed_ms':trace['elapsed_ms']}
    return None


def source_hashes():
    names=('disposition.py','disposition_shadow.py','situation_observation.py','cognitive.py',
           'context_policy.py','ingress.py','chat.py','schema_observation.py')
    return {n:sha256((REPO/'persona_runtime'/n).read_bytes()).hexdigest() for n in names}


def tool_hashes():
    names=('acceptance_m763_observation.py','m763_r04.py','check_observation_provider.py','inspect_m763_repair_evidence.py')
    return {n:sha256((REPO/'scripts'/n).read_bytes()).hexdigest() for n in names}


def execution_hashes():
    return {p.relative_to(REPO).as_posix():sha256(p.read_bytes()).hexdigest()
            for p in sorted((REPO/'persona_runtime').rglob('*')) if p.suffix in ('.py','.json')}


def model_identity(model):
    # Acceptance temporarily changes quota limits, not the model/connection.
    return fingerprint({k:v for k,v in model.items() if k not in ('hourly_limit','daily_limit')})


def controlled_path(home, path):
    home=home.resolve();path=path.resolve()
    if not path.is_relative_to(home/'exports/acceptance'):
        raise ValueError('Explicit controlled evidence path required')
    return path


def validate_check(home,path,model,now=None):
    path=controlled_path(home,path)
    check=json.loads(path.read_text(encoding='utf-8'))
    age=(time.time() if now is None else now)-check.get('checked_epoch',0)
    if (not check.get('normal') or not check.get('configured_model_listed') or not 0<=age<=600
            or check.get('connection_fingerprint')!=model_identity(model)):
        raise ValueError('Provider check invalid, expired or bound to another model')
    return check


def claim_registry(home,out,real):
    if not real:return
    registry=home/'exports/acceptance/m763-r04-real-run.json'
    value={'contract':CONTRACT,'output':str(out)}
    try:
        with registry.open('x',encoding='utf-8') as handle:json.dump(value,handle)
    except FileExistsError:
        if json.loads(registry.read_text(encoding='utf-8'))!=value:
            raise ValueError('Existing real run must be resumed; a new directory cannot bypass slots') from None


def persist_preflight(engine,tid,prepared):
    """Save real production audit objects without inventing a main attempt."""
    with engine.store.connection(write=True) as db:
        turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
        final=engine.cognitive.finalize(db,turn,prepared)
        manifest=deepcopy(final['manifest'])
        would_apply=bool(manifest.get('decision_capsule',{}).get('applied_to_prompt'))
        manifest.update(purpose='development_preflight',status='prepared_not_dispatched',
                        main_model_called=False,dispatch_state='not_dispatched')
        decision=manifest.get('_shadow_prepared')
        if decision:
            decision['would_apply_to_prompt']=would_apply;decision['applied_to_prompt']=False
            if 'capsule' in decision:
                decision['capsule']['would_apply_to_prompt']=would_apply
                decision['capsule']['applied_to_prompt']=False
        if 'decision_capsule' in manifest:
            manifest['decision_capsule']['would_apply_to_prompt']=would_apply
            manifest['decision_capsule']['applied_to_prompt']=False
        cid=engine.cognitive.persist(db,manifest,turn=turn,attempt_id=None)
        row=db.execute('SELECT * FROM disposition_decisions WHERE context_id=?',(cid,)).fetchone()
        if not row:raise ValueError('Preflight decision audit missing')
        stored=json.loads(db.execute('SELECT manifest_json FROM context_manifests WHERE id=?',(cid,)).fetchone()[0])
        if not stored.get('shadow',{}).get('persisted'):raise ValueError('Preflight audit gap')
        if stored['input_snapshot_hash']!=fingerprint(final['messages']):raise ValueError('Preflight hash mismatch')
        return final,{'context_id':cid,'decision_id':row['id'],
                      'final_input_hash':stored['input_snapshot_hash'],'would_apply_to_prompt':would_apply}


class OnlyObservation:
    def __init__(self,real,home=None,out=None,check=None,store=None,binding=None):
        self.real=real;self.client=ChatCompletionsClient();self.case=None;self.calls=0
        self.home=home;self.out=out;self.check=check;self.store=store;self.binding=binding;self.turn_id=None

    async def complete(self,model,messages):
        if not model.get('_situation_observation'):raise AssertionError('Only understanding dispatch is authorized')
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM situation_observation_attempts WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',(self.turn_id,)).fetchone()
            if row is None or row['status']!='running':raise ValueError('Committed observation attempt required')
            frozen=json.loads(row['model_snapshot'])
            if model_identity(frozen)!=self.binding:raise ValueError('Frozen model binding changed')
            from persona_runtime.situation_observation import timeout_for,policy_for
            owner=dict(db.execute('SELECT * FROM turns WHERE id=?',(self.turn_id,)).fetchone())
            allowed=frozen|{'timeout':timeout_for(frozen,policy_for(owner)),'_disposition_compile':True,
                            '_situation_observation':True,'_request_deadline':model.get('_request_deadline')}
            if model!=allowed or row['input_hash']!=fingerprint(messages):raise ValueError('Dispatch differs from frozen attempt')
            if self.real:
                current=ConfigStore(RuntimePaths(self.home)).load()['character_selector']
                if model_identity(current)!=self.binding:raise ValueError('Approved connection changed')
                validate_check(self.home,self.check,current)
            aid=row['id'];manifest=db.execute('SELECT id FROM context_manifests WHERE observation_attempt_id=?',(aid,)).fetchone()
            if manifest is None:raise ValueError('Observation manifest must precede dispatch')
        folder=self.out/'dispatch';folder.mkdir(exist_ok=True);path=folder/(aid+'.json')
        trace={'attempt_id':aid,'turn_id':self.turn_id,'mode':'real' if self.real else 'mock',
               'state':'dispatch_started','dispatch_epoch':time.time(),'request_hash':fingerprint(messages),
               'model_fingerprint':self.binding,'manifest_id':manifest[0],'contract':CONTRACT,
               'observation_contract':row['contract_version'],'timeout_seconds':model['timeout'],
               'provider_check_hash':sha256(self.check.read_bytes()).hexdigest() if self.real else None}
        with path.open('x',encoding='utf-8') as handle:json.dump(trace,handle,indent=2)
        self.calls+=1
        started=time.monotonic()
        try:
            if self.real:body,usage=await self.client.complete(model,messages)
            else:
                sources=json.loads(messages[1]['content'])['sources'];status=self.case['allowed'][0];items=[]
                if status in ('needed','sufficient'):
                    ref=lambda s:{'source_id':s['id'],'quote':s['text']}
                    items=[{'name':'人工参数','task_evidence':[ref(sources[0])],
                            'provided_evidence':[ref(sources[-1])] if status=='sufficient' else []}]
                body=json.dumps({'schema_version':1,'status':status,'items':items},ensure_ascii=False);usage={'total_tokens':0}
            trace.update(state='response_received',response_hash=fingerprint(body),usage=usage)
            return body,usage
        except BaseException as exc:
            trace.update(state='response_unknown',failure_type=type(exc).__name__)
            from persona_runtime.situation_observation import provider_error_code
            from persona_runtime.disposition_compile import safe_diagnostics
            trace.update(error_code=provider_error_code(exc),transport=safe_diagnostics(getattr(exc,'diagnostics',None)))
            raise
        finally:
            trace['finished_epoch']=time.time()
            trace['elapsed_ms']=round((time.monotonic()-started)*1000,3)
            atomic_json(path,trace)


def reconcile(engine,ingress,slot):
    """Resolve durable IDs first; interrupted slots are never implicitly replayed."""
    receipts=[ingress.lookup('desktop',op) for op in slot['request_ids']]
    receipts=[r for r in receipts if r]
    if not receipts:
        slot.update(state='not_accepted_interrupted',recovered=True);return
    if any(r['persona_id']!=slot['persona_id'] for r in receipts):raise ValueError('Recovery scope mismatch')
    tids={r['turn_id'] for r in receipts if r.get('turn_id')}
    if len(tids)>1:raise ValueError('Recovery member split')
    slot['receipt_ids']=[r['receipt_id'] for r in receipts]
    if tids:
        tid=next(iter(tids));slot['turn_id']=tid
        with engine.store.connection() as db:
            a=db.execute('SELECT * FROM situation_observation_attempts WHERE turn_id=?',(tid,)).fetchone()
            manifest=db.execute("SELECT * FROM context_manifests WHERE turn_id=? AND task_type='dialogue' ORDER BY rowid DESC LIMIT 1",(tid,)).fetchone()
            if a:
                slot.update(observed_attempt_id=a['id'],observation_status=(json.loads(a['result_json'] or '{}')).get('status'),
                            state='recovered_needs_review' if a['status']=='completed' else 'interrupted_fee_unknown',recovered=True)
                if manifest:
                    content=json.loads(manifest['manifest_json'])
                    decision=db.execute('SELECT * FROM disposition_decisions WHERE context_id=?',(manifest['id'],)).fetchone()
                    data=json.loads(decision['record_json']) if decision else {}
                    slot.update(context_id=manifest['id'],final_input_hash=content.get('input_snapshot_hash'),
                                decision_id=decision['id'] if decision else None,
                                chosen_action=(data.get('record') or {}).get('chosen_action'))
                elif a['status']=='completed':slot['state']='completed_observation_audit_pending'
    else:
        b=ingress.get(receipts[0]['batch_id'])
        if b['state'] in ('collecting','interrupted_buffer','sealed_waiting_capacity'):
            ingress.action(b['id'],'cancel',b['revision'],identifier(),slot['persona_id'])
        slot.update(state='acceptance_interrupted',recovered=True)


async def recover_audit(engine,slot,case):
    """Recover paid results with local work only, never re-enter the observer."""
    tid=slot['turn_id']
    if slot['state']=='completed_observation_audit_pending':
        from persona_runtime.situation_observation import receipt
        with engine.store.connection() as db:
            observed=receipt(db.execute('SELECT * FROM situation_observation_attempts WHERE id=?',(slot['observed_attempt_id'],)).fetchone())
        prepared=await asyncio.to_thread(engine.cognitive.prepare,tid,observation=observed)
        final,audit=persist_preflight(engine,tid,prepared)
        slot.update(**audit,chosen_action=((final['manifest'].get('_shadow_prepared') or {}).get('record') or {}).get('chosen_action'))
    if slot.get('decision_id'):
        slot.update(state='completed',semantic_review='pending',
                    contract_correct=slot.get('observation_status') in case['allowed'] and ((slot.get('chosen_action')=='ask')==case['ask_allowed']))


def new_slot(case,service,pid,parent=None,reason=None):
    return {'case_id':case['id'],'state':'reserved','persona_id':pid,'session_id':service.create_session()['id'],
            'request_ids':[identifier() for _ in case['members']],'input_hash':fingerprint(case['members']),
            'parent_slot':parent,'recheck_reason':reason}


async def run(home,out,real=False,provider_check=None,recheck=None,reason=None):
    home=home.resolve();out=controlled_path(home,out);out.mkdir(parents=True,exist_ok=True)
    approved=ConfigStore(RuntimePaths(home)).load()['character_selector'] if real else None
    if real:
        if provider_check is None:raise ValueError('Successful model-bound provider check required')
        provider_check=controlled_path(home,provider_check);validate_check(home,provider_check,approved)
        checks=out/'checks';checks.mkdir(exist_ok=True)
        content=provider_check.read_bytes();saved=checks/sha256(content).hexdigest()
        if not saved.exists():saved.write_bytes(content)
    claim_registry(home,out,real)
    raw=(REPO/'experiments/persona_evaluation/repair_cases.json').read_bytes();fixture=json.loads(raw)
    initial={'fixture_sha256':sha256(raw).hexdigest(),'source_hashes':source_hashes(),'tool_hashes':tool_hashes(),
             'execution_hashes':execution_hashes(),
             'mode':'real' if real else 'mock','dataset':'development_only','contract':CONTRACT}
    freeze=out/'freeze.json'
    if freeze.exists():
        previous=json.loads(freeze.read_text(encoding='utf-8'))
        if previous!=initial:
            if not reason:raise ValueError('Frozen implementation changed; explicit recovery reason required')
            archive=out/'revisions';archive.mkdir(exist_ok=True)
            name=fingerprint(initial)+'.json'
            if not (archive/name).exists():(archive/name).write_text(json.dumps(initial,indent=2),encoding='utf-8')
    else:
        with freeze.open('x',encoding='utf-8') as handle:json.dump(initial,handle,indent=2)
    path=out/'results.json'
    state=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'mode':initial['mode'],'contract':CONTRACT,
          'slots':{},'consecutive_failures':0,'main_calls':0,'qq_sends':0}
    if state['mode']!=initial['mode']:raise ValueError('Cannot relabel a simulated run')
    stop=long_failure(out,state)
    if stop:
        state['model_tests_stopped']=stop;atomic_json(path,state)
        raise ValueError('Model tests stopped after a failure over 600 seconds; only non-call closure allowed')
    if state['consecutive_failures']>=5:
        if not recheck or not reason:raise ValueError('Five failures checkpoint; explicit recovery recheck required')
        state.setdefault('failure_breaks',[]).append({'failures':state['consecutive_failures'],'reason':reason})
        state['consecutive_failures']=0
    paths=RuntimePaths(out/'runtime');store=Store(paths);store.initialize();cfg=ConfigStore(paths);cfg.initialize();service=RuntimeService(store)
    if 'persona_id' not in state:
        pid=service.create_persona(replace(generic_persona(),name='M763人工缺口开发资料',expression_style=AUTHOR))['id'];service.use_persona(pid)
        if real:
            local=cfg.load();local['character_selector']=deepcopy(approved);local['organization']['daily_enabled']=False;atomic_json(cfg.file,local)
        else:cfg.update({'character_selector':{'enabled':True,'base_url':'https://example.invalid','name':'mock'}})
        ds=DispositionService(store);source=next(s for s in ds.sources(pid)['sources'] if s['kind']=='expression')
        document=parse_output(json.dumps({'schema_version':1,'definitions':[{'id':'ask-needed','label':'人工测试作者确认的缺口偏好',
            'source_refs':[{'source_id':source['source_id'],'quote':AUTHOR}],
            'triggers':[{'field':'category','op':'eq','value':'advice_request'}],'preferences':{'ask':2},'priority':10,'enabled':True}]},ensure_ascii=False),[source])
        h=ds.draft_update(pid,document,0,identifier());ds.confirm(pid,h['draft_id'],None,h['revision'],identifier())
        state['persona_id']=pid;state['original_limits']=QuotaService(store,cfg)._limits();atomic_json(path,state)
    pid=state['persona_id'];service.use_persona(pid);binding=model_identity(cfg.load()['character_selector'])
    if real and binding!=model_identity(approved):raise ValueError('Isolated model differs from approved connection')
    state['model_fingerprint']=binding
    client=OnlyObservation(real,home,out,provider_check,store,binding)
    grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((pid,)),home)
    engine=ChatEngine(store,cfg,client,assist_admission=grant);ingress=IngressService(engine);quota=QuotaService(store,cfg)
    state['closed']=False;atomic_json(path,state)
    try:
        with patch.object(engine,'_schedule'):
            for slot in state['slots'].values():
                if slot['state'] in ('reserved','accepted'):reconcile(engine,ingress,slot)
        cases={c['id']:c for c in fixture['understanding_cases']}
        for slot in state['slots'].values():
            if slot['state'] in ('completed_observation_audit_pending','recovered_needs_review'):
                await recover_audit(engine,slot,cases[slot['case_id']])
        atomic_json(path,state)
        engine.recover();ingress.recover()
        for slot in state['slots'].values():
            if slot.get('turn_id'):engine.selector.release(slot['turn_id'])
        p=ingress.current_policy(pid,'desktop')
        if p['disposition_mode']!='assist':ingress.update_policy(pid,'desktop','natural_batch',p['id'],identifier(),reply_mode='legacy_full',character_retrieval_mode='lexical',disposition_mode='assist')
        selected=[recheck] if recheck else list(cases)
        if recheck:
            old=state['slots'].get(recheck)
            if not old or not reason:raise ValueError('Existing failed slot and reason required')
            reviews=json.loads((out/'reviews.json').read_text(encoding='utf-8')) if (out/'reviews.json').exists() else []
            rejected=any(r.get('slot_id')==recheck and r.get('attempt_id')==old.get('observed_attempt_id') and r.get('passed') is False for r in reviews)
            accepted=any(s.get('case_id')==recheck and s.get('contract_correct') and not any(
                r.get('slot_id')==k and r.get('attempt_id')==s.get('observed_attempt_id') and r.get('passed') is False
                for r in reviews) for k,s in state['slots'].items())
            if accepted:raise ValueError('Successful output cannot be replayed')
        with quota.acceptance():
            for cid in selected:
                if cid not in cases:raise ValueError('Unknown case ID')
                case=cases[cid]
                if not recheck and cid in state['slots']:continue
                if state['consecutive_failures']>=5:break
                if real:validate_check(home,provider_check,approved)
                key=cid if not recheck else f'{cid}-recheck-{len(state["slots"])}'
                slot=new_slot(case,service,pid,recheck,reason);slot['freeze_hash']=fingerprint(initial);state['slots'][key]=slot;atomic_json(path,state)
                client.case=case
                with patch.object(engine,'_schedule'):
                    receipts=[ingress.accept({'text':text,'request_id':op,'session_id':slot['session_id'],'persona_id':pid}) for text,op in zip(case['members'],slot['request_ids'])]
                    with store.connection(write=True) as db:
                        batch=dict(db.execute('SELECT * FROM input_batches WHERE id=?',(receipts[0]['batch_id'],)).fetchone());ingress._seal(db,batch,'development_observation')
                with store.connection() as db:tid=db.execute('SELECT id FROM turns WHERE input_batch_id=?',(receipts[0]['batch_id'],)).fetchone()[0]
                slot.update(turn_id=tid,state='accepted',receipt_ids=[r['receipt_id'] for r in receipts]);client.turn_id=tid;atomic_json(path,state)
                started=time.monotonic()
                try:
                    observed=await engine.observer.prepare(tid)
                    prepared=await asyncio.to_thread(engine.cognitive.prepare,tid,observation=observed)
                    final,audit=persist_preflight(engine,tid,prepared)
                    record=(final['manifest'].get('_shadow_prepared') or {}).get('record') or {}
                    status=(observed.get('result') or {}).get('status') if observed else None
                    correct=status in case['allowed'] and ((record.get('chosen_action')=='ask')==case['ask_allowed'])
                    slot.update(state='completed' if observed and observed.get('status')=='completed' else 'failed',
                                observation_status=status,error=observed.get('error') if observed else 'not_observed',
                                observed_attempt_id=observed.get('attempt_id') if observed else None,chosen_action=record.get('chosen_action'),
                                contract_correct=correct,elapsed_ms=round((time.monotonic()-started)*1000,3),
                                semantic_review='pending' if real else 'simulated_labels_not_model_quality',**audit)
                    state['consecutive_failures']=0 if slot['state']=='completed' else state['consecutive_failures']+1
                except Exception as exc:
                    slot.update(state='failed',error_type=type(exc).__name__,contract_correct=False)
                    with store.connection() as db:
                        a=db.execute('SELECT id,error_code FROM situation_observation_attempts WHERE turn_id=?',(tid,)).fetchone()
                        if a:slot.update(observed_attempt_id=a['id'],error=a['error_code'])
                    state['consecutive_failures']+=1
                finally:
                    if engine.get(tid)['status']=='queued':engine.cancel_queued(tid)
                    engine.selector.release(tid)
                    slot['elapsed_ms']=round((time.monotonic()-started)*1000,3)
                    stop=long_failure(out,state)
                    if stop:state['model_tests_stopped']=stop
                    atomic_json(path,state)
                print(json.dumps({'case':key,'state':slot['state'],'correct':slot.get('contract_correct')}),flush=True)
                if state.get('model_tests_stopped'):break
    finally:
        await engine.wait();closed=await engine.observer.drain();engine.cognitive.shadow.close();engine.cognitive.characters.close();quota.end_acceptance()
        with store.connection() as db:queued=[r[0] for r in db.execute("SELECT id FROM turns WHERE status='queued'")]
        for tid in queued:engine.cancel_queued(tid);engine.selector.release(tid)
        # A partial ingress batch also needs explicit disposition on interruption.
        for b in ingress.list(pid):
            if b['state'] in ('collecting','interrupted_buffer','sealed_waiting_capacity'):
                ingress.action(b['id'],'cancel',b['revision'],identifier(),pid)
        with store.connection() as db:
            state['understanding_attempts']=db.execute('SELECT count(*) FROM situation_observation_attempts').fetchone()[0]
            state['main_attempts']=db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            state['selector_attempts']=db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0]
            state['compile_attempts']=db.execute('SELECT count(*) FROM disposition_compile_attempts').fetchone()[0]
            state['unresolved_turns']=db.execute("SELECT count(*) FROM turns WHERE status IN ('queued','running')").fetchone()[0]
            state['active_reservations']=db.execute("SELECT count(*) FROM quota_reservations WHERE state='active'").fetchone()[0]
        state.update(closed=closed,limits_restored=state['original_limits']==quota._limits(),
                     all_contracts_passed=all(any(s.get('case_id')==c['id'] and s.get('contract_correct') for s in state['slots'].values()) for c in fixture['understanding_cases']))
        atomic_json(path,state)
    print(json.dumps({k:state[k] for k in ('mode','understanding_attempts','main_attempts','all_contracts_passed','limits_restored','closed')}))
