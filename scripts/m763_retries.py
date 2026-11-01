"""User-authorized evaluation retries; retains failed requests and paid material.

No model dispatch occurs here. New input receipts are created only in the
existing artificial arm's database. The original frozen fixture is immutable.
"""
from copy import deepcopy
import json
from pathlib import Path
import time
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.ingress import IngressService
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input

VERSION='failed-slot-retry-v1'


def current_slot(ledger,package,case_id,arm):
    with ledger.connect(package) as db:
        row=db.execute('SELECT * FROM slots WHERE case_id=? AND arm=?',(case_id,arm)).fetchone()
    if not row:raise ValueError('retry_slot_missing')
    return dict(row)


def effective_case(output,canonical):
    """Only an authoritative receipt may replace a failed request's view."""
    result=deepcopy(canonical);path=output/'retry-views.json'
    if not path.exists():return result
    views=json.loads(path.read_text(encoding='utf-8'))
    for arm in 'ABC':
        view=views.get(canonical['case_id']+'/'+arm)
        if not view:continue
        store=Store(RuntimePaths(Path(canonical['runtime_roots'][arm])))
        with store.connection() as db:
            receipt=db.execute('SELECT * FROM ingress_receipts WHERE request_id=? AND channel=\'desktop\'',(view['request_id'],)).fetchone()
            batch=db.execute('SELECT id FROM turns WHERE input_batch_id=?',(receipt['batch_id'],)).fetchone() if receipt else None
            turn=db.execute('SELECT * FROM turns WHERE id=?',(view['turn_id'],)).fetchone()
            if (not receipt or not batch or batch[0]!=view['turn_id'] or not turn or turn['persona_id']!=canonical['persona_id'] or
                any(fingerprint(turn[k])!=v for k,v in canonical['frozen'][arm].items())):raise ValueError('retry_view_binding')
        result['turns'][arm]=view['turn_id']
    if result['turns']!=canonical['turns']:result['material_turns']=canonical['turns']
    return result


class NoDispatch:
    async def complete(self,*args,**kwargs):raise AssertionError('Retry preparation cannot call a model')


class RetryQueue:
    def __init__(self,output,ledger,package):
        self.output=output;self.ledger=ledger;self.package=package;self.path=output/'retry-state.json'
        policy=json.loads((output/'retry-policy.json').read_text(encoding='utf-8'))
        if policy.get('version')!=VERSION or policy.get('immediate_retry_opportunities')!=3 or policy.get('package_id')!=package:
            raise ValueError('retry_policy_invalid')
        self.value=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'version':VERSION,'entries':[],'active':None}

    def save(self):atomic_json(self.path,self.value)

    def used(self,key,phase):
        return sum(r['key']==key and r['phase']==phase and r.get('model_dispatched',False) for r in self.value['entries'])

    def next(self):
        if any(r.get('requires_repair') and not r.get('repair_acknowledged') for r in self.value['entries']):
            raise ValueError('retry_failed_before_dispatch_requires_repair')
        active=self.value['active']
        if active:
            entry=next(r for r in self.value['entries'] if r['operation_id']==active)
            slot=current_slot(self.ledger,self.package,entry['case_id'],entry['arm'])
            if entry['status']=='accepted' and slot['state'] in ('success','failed'):
                self.finish(entry);return self.next()
            return slot,entry['phase']
        schedule=self.ledger.schedule(self.package)
        if any(r['state'] in ('running','fee_unknown','not_called') for r in schedule):raise ValueError('retry_reconciliation_required')
        for slot in schedule:
            if slot['state']=='failed' and self.used(slot['case_id']+'/'+slot['arm'],'immediate')<3:return slot,'immediate'
        for slot in schedule:
            if slot['state']=='accepted':return slot,'original'
        failed=[r for r in schedule if r['state']=='failed']
        if failed:
            # One tail pass visits every remaining failed slot before repeating;
            # the existing consecutive-failure/600s stop remains authoritative.
            return min(failed,key=lambda r:self.used(r['case_id']+'/'+r['arm'],'tail')),'tail'
        return None

    def start(self,slot,phase):
        if self.value['active']:return next(r for r in self.value['entries'] if r['operation_id']==self.value['active'])
        row=current_slot(self.ledger,self.package,slot['case_id'],slot['arm'])
        if row['state']!='failed' or row['output']:raise ValueError('retry_requires_failed_unusable_output')
        key=row['case_id']+'/'+row['arm'];ordinal=self.used(key,phase)+1
        reason=f'User policy {VERSION}; {phase} retry {ordinal}; previous terminal error {row["error"]}; retain old attempts and unknown fees'
        operation=fingerprint([VERSION,key,row['request_id'],phase,ordinal])
        entry={'key':key,'case_id':row['case_id'],'arm':row['arm'],'phase':phase,'ordinal':ordinal,
               'operation_id':operation,'reason':reason,'parent_request_id':row['request_id'],'previous_attempt_id':row['attempt_id'],
               'status':'preparing','created_epoch':time.time(),'model_dispatched':False}
        self.value['entries'].append(entry);self.value['active']=operation;self.save()
        return entry

    def prepare(self,home,canonical,entry):
        current=effective_case(self.output,canonical);arm=entry['arm']
        if not entry.get('previous_turn_id'):
            entry['previous_turn_id']=current['turns'][arm];self.save()
        # Idempotent operation lookup survives a crash after the ledger reset.
        result=self.ledger.supplement(self.package,entry['case_id'],arm,entry['reason'],entry['operation_id'])
        entry.update(request_id=result['request_id'],status='preparing');self.save()
        paths=RuntimePaths(Path(canonical['runtime_roots'][arm]));store=Store(paths);cfg=ConfigStore(paths)
        grant=AssistAdmission(paths.home,cfg.load()['instance_id'],frozenset((canonical['persona_id'],)),home)
        engine=ChatEngine(store,cfg,NoDispatch(),assist_admission=grant);ingress=IngressService(engine)
        try:
            with store.connection() as db:
                old=dict(db.execute('SELECT * FROM turns WHERE id=?',(entry['previous_turn_id'],)).fetchone())
                members=load_turn_input(db,old)
                if old['status']!='failed':raise ValueError('retry_original_not_failed')
                if any(db.execute('SELECT 1 FROM input_references WHERE event_id=?',(m['id'],)).fetchone() for m in members):
                    raise ValueError('retry_reference_binding_adapter_required')
            receipt=ingress.lookup('desktop',entry['request_id'])
            with patch.object(engine,'_schedule'):
                if receipt is None:
                    from persona_runtime.sessions import SessionService
                    engine.service.use_persona(canonical['persona_id'])
                    active=SessionService(store).current(canonical['persona_id'])
                    if not active['session_id'] or active['session_id']==old['session_id']:engine.service.create_session()
                    else:
                        with store.connection() as db:
                            if db.execute('SELECT 1 FROM events WHERE session_id=?',(active['session_id'],)).fetchone():
                                raise ValueError('retry_session_concurrent_change')
                rows=[ingress.accept({'persona_id':canonical['persona_id'],'text':member['text'],
                    'request_id':entry['request_id'] if i==0 else fingerprint([entry['request_id'],i])}) for i,member in enumerate(members)]
                batch=ingress.get(rows[0]['batch_id'])
                if batch['turn_id']:tid=batch['turn_id']
                else:tid=ingress.action(batch['id'],'seal',batch['revision'],fingerprint([entry['operation_id'],'seal']),canonical['persona_id'])['turn_id']
            with store.connection() as db:
                turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(tid,)).fetchone())
                if any(fingerprint(turn[k])!=v for k,v in canonical['frozen'][arm].items()):raise ValueError('retry_frozen_author_or_policy_changed')
            path=self.output/'retry-views.json';views=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            views[entry['key']]={'turn_id':tid,'request_id':entry['request_id'],'operation_id':entry['operation_id']}
            atomic_json(path,views)
            slot=current_slot(self.ledger,self.package,entry['case_id'],arm)
            if slot['state']=='not_called':self.ledger.transition(self.package,entry['case_id'],arm,'not_called','accepted',receipt_id=rows[0]['receipt_id'])
            elif slot['state']!='accepted':raise ValueError('retry_receipt_requires_reconciliation')
            entry.update(turn_id=tid,runtime_home=str(paths.home),status='accepted');self.save()
            return effective_case(self.output,canonical)
        finally:
            engine.cognitive.shadow.close();engine.cognitive.characters.close()

    def finish(self,entry):
        slot=current_slot(self.ledger,self.package,entry['case_id'],entry['arm'])
        if slot['state'] not in ('success','failed'):return
        aid=slot['attempt_id']
        # A pre-dispatch rejection may leave the ledger attempt_id empty. It
        # never consumes a model retry opportunity or silently loops.
        dispatched=bool(aid and (self.output/'dispatch'/('main-'+aid+'.json')).is_file())
        entry.update(status=slot['state'],attempt_id=aid,model_dispatched=dispatched,finished_epoch=time.time(),error=slot['error'],
                     requires_repair=slot['state']=='failed' and not dispatched)
        self.value['active']=None;self.save()
        if slot['state']=='failed' and not dispatched:raise ValueError('retry_failed_before_dispatch_requires_repair')
