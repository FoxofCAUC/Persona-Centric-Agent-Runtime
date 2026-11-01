"""Resumable independent three-arm execution. No hidden retries or score-model calls."""
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import CharacterEmbeddingProvider
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.character_policy import choose
from persona_runtime.chat import ChatEngine,ChatCompletionsClient
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.evaluation import EvaluationStore
from persona_runtime.evaluation_material import EvaluationSession
from persona_runtime.instance import InstanceLock
from persona_runtime.paths import RuntimePaths
from persona_runtime.quotas import QuotaService
from persona_runtime.storage import Store
from scripts.m763_r04 import model_identity,execution_hashes
from scripts.m763_provider_gate import DirectoryGate
from scripts.m763_retries import RetryQueue,effective_case
from scripts.m763_cohorts import register_run

PACKAGE='m763-r06-independent-v1'
TABLES={'main':'attempts','selector':'character_selection_attempts','understanding':'situation_observation_attempts'}


def validate_provider(path,approved,now=None):
    check=json.loads(path.read_text(encoding='utf-8'))
    bykind={r['kind']:r for r in check['checks']};now=time.time() if now is None else now
    if not check.get('normal') or set(bykind)!={'main','auxiliary'}:raise ValueError('provider_check_failed')
    for kind,key in (('main','model'),('auxiliary','character_selector')):
        row=bykind[kind]
        if not row.get('normal') or not 0<=now-row['checked_epoch']<=600 or row['model_fingerprint']!=model_identity(approved[key]):
            raise ValueError('provider_check_expired_or_changed')


def validate_model_cohort(output,approved):
    path=output/'execution-freeze.json'
    if path.exists():
        original=json.loads(path.read_text(encoding='utf-8'))['models']
        if original!={k:model_identity(approved[k]) for k in ('model','character_selector')}:
            raise ValueError('evaluation_model_cohort_changed_preserve_original_run')


class RunState:
    def __init__(self,out):
        self.out=out;self.path=out/'execution-state.json'
        self.value=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'processed':[],'consecutive_failures':0,'stop':None}

    def check(self):
        if self.value['stop']:raise ValueError('model_tests_stopped:'+self.value['stop'])

    def collect(self):
        for path in sorted((self.out/'dispatch').glob('*.json'),key=lambda p:p.stat().st_mtime_ns):
            trace=json.loads(path.read_text(encoding='utf-8'))
            if trace['attempt_id'] in self.value['processed'] or not trace.get('finished_epoch'):continue
            store=Store(RuntimePaths(Path(trace['runtime_home'])))
            with store.connection() as db:
                row=db.execute('SELECT status FROM '+TABLES[trace['purpose']]+' WHERE id=?',(trace['attempt_id'],)).fetchone()
            success=bool(row and row[0]=='completed')
            self.value['processed'].append(trace['attempt_id'])
            self.value['consecutive_failures']=0 if success else self.value['consecutive_failures']+1
            if not success and trace['elapsed_ms']>600000:self.value['stop']='single_failure_over_600_seconds'
            elif self.value['consecutive_failures']>=5:self.value['stop']='five_consecutive_model_failures'
        atomic_json(self.path,self.value)


def pending_slots(ledger):
    """A terminal failed slot remains in the denominator, never an implicit retry.

    Uncertain/in-progress slots require authoritative reconciliation first.
    Independent uncalled slots can continue without replacing any failed output.
    """
    for slot in ledger.schedule(PACKAGE):
        if slot['state'] in ('success','failed'):continue
        if slot['state']!='accepted':raise ValueError('Resolve original ledger slot before any re-dispatch')
        yield slot


def validate_repair(old,identity,tests):
    changed={k for k in set(old['source_hashes'])|set(identity['source_hashes']) if old['source_hashes'].get(k)!=identity['source_hashes'].get(k)}
    if (not tests.get('passed') or tests.get('source_hashes')!=identity['source_hashes'] or
        tests.get('runner_hash')!=identity['runner_hash'] or tests.get('provider_gate_hash')!=identity['provider_gate_hash'] or
        tests.get('retry_tool_hash')!=identity.get('retry_tool_hash') or tests.get('retry_policy_hash')!=identity.get('retry_policy_hash') or
        not changed<=set(('persona_runtime/character_selector.py','persona_runtime/evaluation_material.py')) or
        any(old[k]!=identity[k] for k in ('fixture_hash','prepared_hash','models'))):
        raise ValueError('Repair evidence does not cover frozen run')


class AuditedClient:
    def __init__(self,home,out,arm,turn_id,ledger,case_id,state,provider_check):
        self.home=home;self.out=out;self.arm=arm;self.tid=turn_id;self.ledger=ledger;self.case_id=case_id;self.state=state;self.check=provider_check
        self.engine=None;self.client=ChatCompletionsClient()

    async def complete(self,model,messages):
        self.state.check()
        approved=ConfigStore(RuntimePaths(self.home)).load();validate_provider(self.check.path,approved)
        purpose='understanding' if model.get('_situation_observation') else 'main' if '_reply_mode' in model else 'selector'
        if purpose=='understanding' and self.arm!='C':raise AssertionError('Only C may dispatch understanding')
        if purpose=='selector' and self.arm!='B':raise AssertionError('Only B may dispatch selection')
        table=TABLES[purpose]
        with self.engine.store.connection() as db:
            row=db.execute('SELECT * FROM '+table+' WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',(self.tid,)).fetchone()
            if not row or row['status']!='running':raise ValueError('Committed running attempt required')
            snapshot=json.loads(row['model_snapshot'])
            approved_model=approved['model' if purpose=='main' else 'character_selector']
            comparison=bool(getattr(getattr(self.engine,'evaluation_session',None),'prompt_comparison',False))
            identity_snapshot=dict(snapshot)
            if comparison and purpose=='main':identity_snapshot.pop('_dialogue_profile',None)
            if model_identity(identity_snapshot)!=model_identity(approved_model):raise ValueError('Frozen approved model changed')
            forwarded={k:v for k,v in model.items() if not k.startswith('_')}
            expected=snapshot|{'timeout':240 if purpose=='understanding' else snapshot['timeout']}
            expected={k:v for k,v in expected.items() if not k.startswith('_')}
            if forwarded!=expected:raise ValueError('Dispatch model differs from frozen parameters')
            fk={'main':'attempt_id','selector':'selection_attempt_id','understanding':'observation_attempt_id'}[purpose]
            manifest=db.execute('SELECT * FROM context_manifests WHERE '+fk+'=?',(row['id'],)).fetchone()
            if not manifest or json.loads(manifest['manifest_json'])['input_snapshot_hash']!=fingerprint(messages):raise ValueError('Dispatch Manifest mismatch')
        if purpose=='main':self.ledger.transition(PACKAGE,self.case_id,self.arm,'accepted','running',attempt_id=row['id'])
        trace={'attempt_id':row['id'],'turn_id':self.tid,'case_id':self.case_id,'arm':self.arm,'purpose':purpose,
            'runtime_home':str(self.engine.store.paths.home),'dispatch_epoch':time.time(),'request_hash':fingerprint(messages),
            'manifest_id':manifest['id'],'model_fingerprint':model_identity(snapshot),'provider_check_hash':sha256(self.check.path.read_bytes()).hexdigest(),'state':'dispatch_started'}
        folder=self.out/'dispatch';folder.mkdir(exist_ok=True);path=folder/(purpose+'-'+row['id']+'.json')
        with path.open('x',encoding='utf-8') as f:json.dump(trace,f,indent=2)
        started=time.monotonic()
        try:
            answer=await self.client.complete(model,messages)
            trace.update(state='response_received',response_hash=fingerprint(answer[0]),usage=answer[1]);return answer
        except BaseException as exc:
            from persona_runtime.disposition_compile import safe_diagnostics
            trace.update(state='response_unknown',error_type=type(exc).__name__,error_code=str(exc) if str(exc).startswith('provider_') else 'model_call_failed',transport=safe_diagnostics(getattr(exc,'diagnostics',None)))
            raise
        finally:
            trace.update(finished_epoch=time.time(),elapsed_ms=round((time.monotonic()-started)*1000,3));atomic_json(path,trace)


async def close_engines(engines):
    for engine in engines.values():
        await engine.wait();closed=await engine.observer.drain()
        engine.selector.release(engine._evaluation_turn_id)
        engine.cognitive.shadow.close();engine.cognitive.characters.close()
        if not closed:raise ValueError('Understanding worker did not close; no further dispatch allowed')


async def execute_case_slot(home,out,prepared,arm,ledger,state,provider_check,provider,*,prompt_comparison=False):
    engines={};quotas=[];turns=prepared['turns'];case_id=prepared['case_id']
    try:
        for side in 'ABC':
            paths=RuntimePaths(Path(prepared['runtime_roots'][side]));store=Store(paths);cfg=ConfigStore(paths)
            client=AuditedClient(home,out,side,turns[side],ledger,case_id,state,provider_check)
            grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((prepared['persona_id'],)),home)
            engine=ChatEngine(store,cfg,client,assist_admission=grant);engine._evaluation_turn_id=turns[side];engines[side]=engine;client.engine=engine
            engine.cognitive.characters.index_factory=lambda config,s=store:CharacterSemanticIndex(s,provider)
            quota=QuotaService(store,cfg);quota.begin_acceptance();quotas.append(quota)
            with store.connection() as db:
                row=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns[side],)).fetchone())
                if any(fingerprint(row[k])!=v for k,v in prepared['frozen'][side].items()):raise ValueError('Frozen request changed')
        sessions=EvaluationSession.register_independent(engines,turns,material_turns=prepared.get('material_turns'),prompt_comparison=prompt_comparison)
        await provider_check.ensure()
        b=await engines['B'].selector.prepare(turns['B']);state.collect();state.check()
        c=await engines['C'].selector.prepare(turns['C'])
        await provider_check.ensure()
        observation=await engines['C'].observer.prepare(turns['C']);state.collect();state.check()
        contexts={side:await asyncio.to_thread(engines[side].cognitive.prepare,turns[side],selection,observation if side=='C' else None) for side,selection in (('B',b),('C',c))}
        sessions['C'].verify_independent(turns,contexts)
        await provider_check.ensure()
        await engines[arm]._run(turns[arm]);state.collect()
        value=engines[arm].get(turns[arm])
        with ledger.connect(PACKAGE) as db:slot=db.execute('SELECT * FROM slots WHERE case_id=? AND arm=?',(case_id,arm)).fetchone()
        if value['status']=='completed':
            ledger.transition(PACKAGE,case_id,arm,slot['state'],'success',attempt_id=slot['attempt_id'],output=value.get('display_reply') or value['reply'])
        else:
            ledger.transition(PACKAGE,case_id,arm,slot['state'],'failed',error=value.get('error_code'))
        state.check()
    finally:
        try:await close_engines(engines)
        finally:
            for quota in quotas:quota.end_acceptance()
            state.collect()


async def run(home,out,provider_check,repair_reason=None,repair_tests=None,supplier_change_from=None):
    home=home.resolve();out=out.resolve()
    if not out.is_relative_to(home/'exports/acceptance'):raise ValueError('Controlled evidence required')
    approved=ConfigStore(RuntimePaths(home)).load()
    validate_model_cohort(out,approved)
    frozen=json.loads((out/'prepared.json').read_text(encoding='utf-8'))
    if sha256((out/'independent-v1.json').read_bytes()).hexdigest()!=frozen['fixture_sha256']:raise ValueError('Independent fixture changed')
    if frozen.get('prompt_comparison'):
        from scripts.m763_cohorts import save_once
        fixture=json.loads((out/'independent-v1.json').read_text(encoding='utf-8'))
        if fixture.get('version')!='m763-natural-voice-independent-v2':raise ValueError('prompt_revision_fixture_invalid')
        registry=home/'exports/acceptance/m763-natural-voice-v2-run.json'
        save_once(registry,{'output':str(out),'fixture_sha256':frozen['fixture_sha256'],'prepared_hash':fingerprint(frozen),
                            'reason':'User authorized new independent v2 after naturalness failure; v1 remains immutable'})
    else:register_run(home,out,frozen,approved,supplier_change_from)
    provider_check=DirectoryGate(provider_check,approved,out)
    await provider_check.ensure();validate_provider(provider_check.path,approved)
    state=RunState(out);state.collect();state.check();ledger=EvaluationStore(RuntimePaths(out))
    identity={'source_hashes':execution_hashes(),'runner_hash':sha256(Path(__file__).read_bytes()).hexdigest(),
        'provider_gate_hash':sha256(Path(__file__).with_name('m763_provider_gate.py').read_bytes()).hexdigest(),
        'retry_tool_hash':sha256(Path(__file__).with_name('m763_retries.py').read_bytes()).hexdigest(),
        'cohort_tool_hash':sha256(Path(__file__).with_name('m763_cohorts.py').read_bytes()).hexdigest(),
        'retry_policy_hash':sha256((out/'retry-policy.json').read_bytes()).hexdigest() if (out/'retry-policy.json').exists() else None,
        'fixture_hash':frozen['fixture_sha256'],
        'prepared_hash':fingerprint(frozen),'models':{k:model_identity(approved[k]) for k in ('model','character_selector')}}
    path=out/'execution-freeze.json'
    if path.exists():
        old=json.loads(path.read_text(encoding='utf-8'))
        if old!=identity:
            if not repair_reason or not repair_tests:raise ValueError('Execution identity changed; explicit repair review required')
            tests=json.loads(repair_tests.read_text(encoding='utf-8'))
            validate_repair(old,identity,tests)
            folder=out/'execution-revisions';folder.mkdir(exist_ok=True)
            with (folder/(fingerprint(old)+'.json')).open('x',encoding='utf-8') as f:json.dump(old,f,indent=2)
            atomic_json(folder/(fingerprint(identity)+'-repair.json'),{'reason':repair_reason,'tests_hash':sha256(repair_tests.read_bytes()).hexdigest(),'previous':fingerprint(old),'next':fingerprint(identity),'successful_outputs_preserved':True})
            atomic_json(path,identity)
    else:
        with path.open('x',encoding='utf-8') as f:json.dump(identity,f,indent=2)
    provider=CharacterEmbeddingProvider(RuntimePaths(home),home/'models/character-embedding'/choose('hybrid_llm')['config']['model_fingerprint'])
    if provider.diagnose()['status']!='ready':raise ValueError('Frozen local model unavailable')
    cases={c['case_id']:c for c in frozen['cases']}
    queue=RetryQueue(out,ledger,PACKAGE) if (out/'retry-policy.json').exists() else None
    original=iter(pending_slots(ledger)) if queue is None else None
    while True:
        state.check()
        item=queue.next() if queue else ((slot,'original') if (slot:=next(original,None)) else None)
        if item is None:break
        slot,phase=item;entry=None
        await provider_check.ensure();validate_provider(provider_check.path,approved)
        case=cases[slot['case_id']]
        if phase!='original':
            entry=queue.start(slot,phase);prepared=queue.prepare(home,case,entry)
        else:prepared=effective_case(out,case)
        try:await execute_case_slot(home,out,prepared,slot['arm'],ledger,state,provider_check,provider,prompt_comparison=bool(frozen.get('prompt_comparison')))
        finally:
            if entry:queue.finish(entry)
        print(json.dumps({'case_id':slot['case_id'],'arm':slot['arm'],'phase':phase,'status':ledger.status(PACKAGE)}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--home',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--provider-check',type=Path,required=True)
    p.add_argument('--repair-reason');p.add_argument('--repair-tests',type=Path)
    p.add_argument('--supplier-change-from',type=Path,help='Explicitly preserve registered old run and freeze a changed supplier cohort')
    a=p.parse_args()
    lock=InstanceLock(a.output/'execution.lock')
    try:asyncio.run(run(a.home,a.output,a.provider_check,a.repair_reason,a.repair_tests,a.supplier_change_from))
    finally:lock.close()
