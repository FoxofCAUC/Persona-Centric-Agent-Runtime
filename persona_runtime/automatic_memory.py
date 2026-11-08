"""Source coverage and staged memory coordination; never invokes a model."""
import json
from datetime import datetime,timezone
from .archive import binding,read_exact,turn_scope
from .models import LOCAL_USER_ID,RuntimeErrorBase,identifier,timestamp
from .organization import digest,encoded,normalized
from .automatic_memory_output import messages,parse,VERSION

ACTIVE=('extracting','verifying','waiting')


def available(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_batches'").fetchone())


def facts(db,pid,selected):
    """Only same-Scope, currently supported memories may enter either model."""
    from .context_builders import memory_usable
    rows=[]
    representative={'persona_id':pid,'channel':selected['channel'],'origin_json':encoded({'bot':selected['bot'],'sender':selected['peer']}),'input_batch_id':None}
    for r in db.execute("SELECT * FROM memories WHERE persona_id=? AND status='active' ORDER BY rowid",(pid,)):
        m=dict(r)
        if not memory_usable(db,representative,m,for_review=True):continue
        f=db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(m['id'],)).fetchone()
        if f and json.loads(f['data_json']).get('import_review_required'):continue
        if f and json.loads(f['scope_json'])!=selected:continue
        rows.append({k:m[k] for k in ('id','text','category','revision')}|({'subject':f['subject'],'attribute':f['attribute'],'valid_from':f['valid_from'],'valid_until':f['valid_until'],'currently_usable':usable_fact(db,m)} if f else {}))
    return rows


def select_facts(rows,events,proposals=None):
    from .chat import terms
    query=terms(' '.join(e['text'] for e in events if e['role']=='user'))
    slots={(normalized(i['subject']),normalized(i['attribute'])) for i in (proposals or {}).get('candidates',[])}
    return sorted(rows,key=lambda m:(100 if (normalized(m.get('subject','')),normalized(m.get('attribute',''))) in slots else 0)+len(query & terms(m['text'])),reverse=True)[:30]


def dependency_hash(rows,payload):
    ids={m['id'] for m in payload['existing_memories']}
    slots={(normalized(p['subject']),normalized(p['attribute'])) for p in payload.get('proposals',{}).get('candidates',[])}
    return digest([m for m in rows if m['id'] in ids or (normalized(m.get('subject','')),normalized(m.get('attribute',''))) in slots])


def blocked_sources(db,pid):
    ids=set()
    for row in db.execute('''SELECT t.*,c.data_json AS contract_json FROM organization_tasks t
        LEFT JOIN organization_contracts c ON c.task_id=t.id WHERE t.persona_id=? AND t.mode!='test'
        AND t.status IN ('blocked_unknown','interrupted','cancelled')''',(pid,)):
        c=json.loads(row['contract_json']) if row['contract_json'] else {}
        attempted=db.execute('SELECT 1 FROM organization_attempts WHERE task_id=?',(row['id'],)).fetchone()
        if row['status']=='cancelled' and not attempted and c.get('phase')!='unknown':continue
        p=json.loads(row['payload_json']);ids.update(i['turn_id'] for i in p.get('bindings',[]))
        if p.get('rows') and p.get('session_id'):
            ids.update(r[0] for r in db.execute('SELECT id FROM turns WHERE session_id=? AND rowid IN ('+','.join('?' for _ in p['rows'])+')',(p['session_id'],*p['rows'])))
    return ids


def validate_sources(db,payload,check_memories=True):
    for item in payload['bindings']:
        r=read_exact(db,payload['scope'],{'kind':'turn','id':item['turn_id']})
        if r['status']!='found' or binding(r)!=item['binding']:raise RuntimeErrorBase('source_expired')
        row=db.execute('SELECT created_at FROM turns WHERE id=?',(item['turn_id'],)).fetchone()
        if not row or row[0]!=payload['recorded_times'][item['turn_id']]:raise RuntimeErrorBase('source_expired')
    if check_memories and dependency_hash(facts(db,payload['persona_id'],payload['scope']),payload)!=payload['memory_dependency_hash']:
        raise RuntimeErrorBase('automatic_memory_version_changed')


def invalidate(db,pid,event_ids):
    """In the caller's withdrawal transaction; no IO, dispatch, or new grants."""
    if not available(db):return
    ids=set(event_ids)
    for row in db.execute("SELECT m.* FROM memories m WHERE m.persona_id=? AND m.status='active'",(pid,)).fetchall():
        sources={row['source_event_id']}|{r[0] for r in db.execute('SELECT event_id FROM memory_sources WHERE memory_id=?',(row['id'],))}
        if sources&ids:db.execute("UPDATE memories SET status='retracted',updated_at=? WHERE id=?",(timestamp(),row['id']))
    for row in db.execute("SELECT * FROM memory_fragments WHERE persona_id=? AND state='active'",(pid,)).fetchall():
        data=json.loads(row['data_json'])
        if ids & {e['id'] for e in data['payload']['events']}:
            invalidate_fragment(db,row,'source_withdrawn')
    for row in db.execute('SELECT * FROM memory_coverage WHERE persona_id=?',(pid,)).fetchall():
        b=db.execute('SELECT data_json FROM memory_batches WHERE id=?',(row['batch_id'],)).fetchone()
        if not b:continue
        p=json.loads(b[0])['payload']
        if ids & {e['id'] for e in p['events'] if e['turn_id']==row['turn_id']}:
            db.execute("UPDATE memory_coverage SET disposition='revoked',summary_state='invalid',updated_at=? WHERE turn_id=?",(timestamp(),row['turn_id']))
    for row in db.execute("SELECT * FROM memory_decisions WHERE persona_id=? AND state='quarantined'",(pid,)).fetchall():
        d=json.loads(row['data_json'])
        if ids & {s['source_id'] for s in d['proposal']['sources']}:
            db.execute("UPDATE memory_decisions SET state='stale',reason='source_expired' WHERE id=?",(row['id'],))


def usable_fact(db,memory,now=None):
    if not available(db):return True
    f=db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(memory['id'],)).fetchone()
    if not f:return True
    if json.loads(f['data_json']).get('import_review_required'):return False
    now=now or datetime.now(timezone.utc)
    if f['valid_from'] and datetime.fromisoformat(f['valid_from'])>now:return False
    if f['valid_until'] and datetime.fromisoformat(f['valid_until'])<=now:return False
    for row in db.execute("SELECT data_json FROM memory_decisions WHERE persona_id=? AND state='quarantined'",(memory['persona_id'],)):
        if memory['id'] in json.loads(row[0]).get('blocked_memory_ids',[]):return False
    return True


def invalidate_fragment(db,fragment,reason='source_expired'):
    data=json.loads(fragment['data_json']);data['invalidation_reason']=reason
    db.execute("UPDATE memory_fragments SET state='invalid',data_json=? WHERE id=?",(encoded(data),fragment['id']))
    for b in json.loads(fragment['data_json'])['payload']['bindings']:
        db.execute("UPDATE memory_coverage SET summary_state='invalid',updated_at=? WHERE turn_id=?",(timestamp(),b['turn_id']))


def supersede_fact(db,old,reason='fact_revised'):
    """Shared by human and automatic revision; legacy policies stay compatible."""
    fact=db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(old['id'],)).fetchone() if available(db) else None
    if not fact:
        from .memory_policy import exclude_memory
        exclude_memory(db,old,'memory_revised')
        return None
    sources={old['source_event_id']}|{r[0] for r in db.execute('SELECT event_id FROM memory_sources WHERE memory_id=?',(old['id'],))}
    for f in db.execute("SELECT * FROM memory_fragments WHERE persona_id=? AND state='active'",(old['persona_id'],)).fetchall():
        if sources & {e['id'] for e in json.loads(f['data_json'])['payload']['events']}:invalidate_fragment(db,f,reason)
    for summary in db.execute("SELECT id,sources_json FROM summaries WHERE persona_id=? AND status='active'",(old['persona_id'],)).fetchall():
        if sources & set(json.loads(summary['sources_json'])):db.execute("UPDATE summaries SET status='stale',reason=? WHERE id=?",(reason,summary['id']))
    return dict(fact)


def human_source_check(db,old,event_ids):
    if not available(db):return
    f=db.execute('SELECT scope_json FROM memory_facts WHERE memory_id=?',(old['id'],)).fetchone()
    if not f:return
    selected=json.loads(f[0])
    for ident in event_ids:
        if read_exact(db,selected,{'kind':'event','id':ident})['status']!='found':raise RuntimeErrorBase('source_expired')


def copy_manual_fact(db,fact,new_id):
    if not fact:return
    data=json.loads(fact['data_json']);data.update(manual_authority=True,import_review_required=False)
    db.execute('INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,?,?)',(new_id,fact['persona_id'],fact['user_id'],fact['scope_json'],fact['subject'],fact['attribute'],fact['valid_from'],fact['valid_until'],fact['batch_id'],encoded(data)))


class AutomaticMemory:
    def __init__(self,engine):self.engine=engine;self.store=engine.store

    def is_enabled(self,db,pid):
        p=self.engine.managed.policy(db,pid)['data']
        return p['enabled'] and p.get('memory_automation',False)

    def payload(self,db,pid,sid,selected,turns,purpose='extract',previous=None):
        p={'persona_id':pid,'user_id':LOCAL_USER_ID,'session_id':sid,'channel':selected['channel'],'scope':selected,
           'mode':'both','source_format':3,'rows':[],'events':[],'bindings':[],'previous':None,
           'existing_memories':facts(db,pid,selected),'outdated_claims':[],'recorded_times':{},
           'automatic_memory':{'version':VERSION,'stage':'extract','purpose':purpose,'previous_fragment':previous}}
        all_memories=p['existing_memories'];p['memory_state_hash']=digest(all_memories)
        for m in all_memories:
            if m.get('valid_until') and datetime.fromisoformat(m['valid_until'])<=datetime.now(timezone.utc):p['outdated_claims'].append({k:m[k] for k in ('id','text','subject','attribute')}|{'reason':'expired','valid_until':m['valid_until']})
        for r in db.execute("SELECT m.id,m.text,m.previous_id,f.subject,f.attribute FROM memories m JOIN memory_facts f ON f.memory_id=m.id WHERE m.persona_id=? AND m.status='superseded' AND f.scope_json=?",(pid,encoded(selected))):
            if not any(normalized(m['text'])==normalized(r['text']) and m.get('subject')==r['subject'] and m.get('attribute')==r['attribute'] for m in p['existing_memories']):p['outdated_claims'].append(dict(r))
        for turn in turns[:10]:
            exact=read_exact(db,selected,{'kind':'turn','id':turn['id']})
            if exact['status']!='found':continue
            candidate=json.loads(encoded(p));candidate['rows'].append(turn['ordinal']);candidate['bindings'].append({'turn_id':turn['id'],'binding':binding(exact)})
            candidate['recorded_times'][turn['id']]=turn['created_at']
            for e in exact['members']+exact['replies']:
                candidate['events'].append({'id':e['id'],'kind':'part' if e.get('kind')=='part' else 'event','role':e.get('role','assistant'),'text':e['text'],'turn':turn['ordinal'],'turn_id':turn['id']})
            candidate['existing_memories']=select_facts(all_memories,candidate['events'])
            limit=self.engine.model()['max_input_chars'];source_size=sum(len(e['text']) for e in candidate['events'])
            if source_size>limit//3:break
            while candidate['existing_memories'] and sum(len(m['content']) for m in messages(candidate))>limit:
                candidate['existing_memories'].pop()
            if sum(len(m['content']) for m in messages(candidate))>limit:break
            p=candidate
        return p

    def begin(self,db,payload,trigger):
        db.execute('SAVEPOINT memory_admission')
        try:result=self._begin(db,payload,trigger)
        except BaseException:
            db.execute('ROLLBACK TO memory_admission');db.execute('RELEASE memory_admission');raise
        db.execute('RELEASE memory_admission');return result

    def _begin(self,db,payload,trigger):
        if not payload['rows']:return None
        purpose=payload['automatic_memory']['purpose']
        key=digest([payload['scope'],payload['bindings'],purpose,payload['automatic_memory']['previous_fragment'],payload['outdated_claims'] if purpose=='repair' else []])
        existing=db.execute('SELECT * FROM memory_batches WHERE work_key=?',(key,)).fetchone()
        if existing:return dict(existing)
        # A different trigger/repair key cannot replay unknown work on its sources.
        wanted={b['turn_id'] for b in payload['bindings']}
        if wanted & blocked_sources(db,payload['persona_id']):return None
        bid=identifier();payload['automatic_memory']['batch_id']=bid;payload['fingerprint']=digest(payload)
        now=timestamp();data={'payload':payload,'proposals':None,'trigger':trigger}
        db.execute('INSERT INTO memory_batches VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)',
            (bid,payload['persona_id'],LOCAL_USER_ID,payload['session_id'],encoded(payload['scope']),key,purpose,'extracting',encoded(data),now,now))
        manual=trigger=='manual' or bool(payload['automatic_memory'].get('manual_operation'))
        task=self.engine.managed.register(db,payload,'memory-extract-'+key,automatic=0 if manual else 1,trigger=trigger)
        db.execute('UPDATE memory_batches SET extract_task_id=? WHERE id=?',(task['id'],bid))
        if purpose!='repair':
            for b in payload['bindings']:
                db.execute('INSERT INTO memory_coverage VALUES(?,?,?,?,?,?,?, ?,?) ON CONFLICT(turn_id) DO UPDATE SET source_hash=excluded.source_hash,batch_id=excluded.batch_id,disposition=excluded.disposition,summary_state=excluded.summary_state,updated_at=excluded.updated_at',
                    (payload['persona_id'],LOCAL_USER_ID,b['turn_id'],encoded(payload['scope']),b['binding']['hash'],bid,'pending','pending',now))
        return dict(db.execute('SELECT * FROM memory_batches WHERE id=?',(bid,)).fetchone())

    def enqueue_verification(self,db,batch):
        db.execute('SAVEPOINT memory_verification_admission')
        try:self._enqueue_verification(db,batch)
        except BaseException:
            db.execute('ROLLBACK TO memory_verification_admission');db.execute('RELEASE memory_verification_admission');raise
        db.execute('RELEASE memory_verification_admission')

    def _enqueue_verification(self,db,batch):
        data=json.loads(batch['data_json']);p=data['payload'];validate_sources(db,p,check_memories=False)
        p=json.loads(encoded(p));p['automatic_memory']['stage']='verify';p['proposals']=data['proposals']
        all_memories=facts(db,p['persona_id'],p['scope']);p['memory_state_hash']=digest(all_memories);p['existing_memories']=select_facts(all_memories,p['events'],p['proposals'])
        while p['existing_memories'] and sum(len(m['content']) for m in messages(p))>self.engine.model()['max_input_chars']:p['existing_memories'].pop()
        if sum(len(m['content']) for m in messages(p))>self.engine.model()['max_input_chars']:raise RuntimeErrorBase('automatic_memory_verification_input_too_long')
        p['memory_dependency_hash']=dependency_hash(all_memories,p)
        p['fingerprint']=digest(p);key=digest([batch['id'],p['proposals'],p['memory_dependency_hash']])
        original=db.execute('SELECT automatic FROM organization_tasks WHERE id=?',(batch['extract_task_id'],)).fetchone()[0]
        task=self.engine.managed.register(db,p,'memory-verify-'+key,automatic=original,trigger=data['trigger'])
        data.setdefault('verification_tasks',[]).append(task['id'])
        db.execute("UPDATE memory_batches SET verify_task_id=?,data_json=?,state='verifying',updated_at=? WHERE id=?",(task['id'],encoded(data),timestamp(),batch['id']))

    def published(self,db,task,payload,output):
        """Called only inside the existing owner's publication transaction."""
        bid=payload['automatic_memory']['batch_id'];batch=dict(db.execute('SELECT * FROM memory_batches WHERE id=?',(bid,)).fetchone());data=json.loads(batch['data_json'])
        if payload['automatic_memory']['stage']=='extract':
            data['proposals']=output
            db.execute("UPDATE memory_batches SET data_json=?,state='waiting',updated_at=? WHERE id=?",(encoded(data),timestamp(),bid))
            if not output['summary'] and not output['candidates']:self.commit(db,batch,payload,output,[])
        else:self.commit(db,batch,payload,payload['proposals'],output['decisions'])

    def commit(self,db,batch,payload,proposals,decisions):
        bid=batch['id'];pid=payload['persona_id'];selected=payload['scope'];now=timestamp();kept=[];quarantine=False
        items={i['id']:(kind,i) for kind in ('summary','candidates') for i in proposals[kind]}
        live={m['id']:m for m in facts(db,pid,selected)};published_targets=set()
        collisions={}
        for d in decisions:
            if d['action'] in ('publish','revise','temporal'):
                slot=(normalized(d['subject']),normalized(d['attribute']))
                collisions.setdefault(slot,set()).add(normalized(items[d['id']][1]['text']))
        for d in decisions:
            kind,item=items[d['id']];action=d['action'];state='omitted';reason=d['reason'];mid=None
            if d['target_memory_id'] in published_targets:action='publish'
            if action=='summary' and any(normalized(item['text'])==normalized(o['text']) for o in payload['outdated_claims']):
                action='omit';reason='superseded_literal_not_current_summary'
            matches=[]
            if kind=='candidates':
                matches=[m for m in live.values() if normalized(m.get('subject',''))==normalized(d['subject']) and normalized(m.get('attribute',''))==normalized(d['attribute'])]
                if action=='publish' and any(normalized(m['text'])!=normalized(item['text']) for m in matches):action='quarantine';reason='same_slot_requires_supported_revision'
                if len(collisions.get((normalized(d['subject']),normalized(d['attribute'])),()))>1:action='quarantine';reason='multiple_proposals_same_slot'
            target=None if d['target_memory_id'] in published_targets else live.get(d['target_memory_id'])
            if target and (target['category']!=item.get('category') or target.get('subject') and (normalized(target['subject'])!=normalized(d['subject']) or normalized(target['attribute'])!=normalized(d['attribute']))):
                action='quarantine';reason='revision_slot_mismatch'
            if action in ('revise','temporal') and not target:
                action='quarantine';reason='revision_target_missing'
            if action in ('revise','temporal'):
                start=datetime.fromisoformat(d['effective_at'] or now)
                end=datetime.fromisoformat(d['valid_until']) if d['valid_until'] else datetime.max.replace(tzinfo=timezone.utc)
                others=[m for m in matches if m['id']!=target['id'] and normalized(m['text'])!=normalized(item['text'])]
                if any(start<(datetime.fromisoformat(m['valid_until']) if m.get('valid_until') else datetime.max.replace(tzinfo=timezone.utc)) and end>(datetime.fromisoformat(m['valid_from']) if m.get('valid_from') else datetime.min.replace(tzinfo=timezone.utc)) for m in others):
                    action='quarantine';reason='overlapping_fact_versions'
                elif target.get('valid_from') and start<=datetime.fromisoformat(target['valid_from']):
                    action='quarantine';reason='ambiguous_temporal_order'
            if action=='summary':
                kept.append(dict(item,sources=d['sources']));state='summarized'
            elif action in ('publish','revise','temporal'):
                # Independent review must reaffirm the extraction's evidence,
                # never approve a candidate using only unrelated new quotations.
                verified_ids={s['source_id'] for s in d['sources']}
                if not verified_ids & {s['source_id'] for s in item['sources']}:raise RuntimeErrorBase('automatic_memory_evidence_mismatch')
                sources=d['sources'];old=None
                if target:
                    old=db.execute("SELECT * FROM memories WHERE id=? AND status='active' AND revision=?",(target['id'],target['revision'])).fetchone()
                    if not old:raise RuntimeErrorBase('automatic_memory_version_changed')
                    if not db.execute('SELECT 1 FROM memory_facts WHERE memory_id=?',(old['id'],)).fetchone():
                        db.execute('INSERT INTO memory_facts VALUES(?,?,?,?,?,?,NULL,NULL,?,?)',(old['id'],pid,LOCAL_USER_ID,encoded(selected),normalized(d['subject']),normalized(d['attribute']),bid,encoded({'legacy_approved':True,'upgraded_for_revision':True})))
                    supersede_fact(db,old)
                    future=action=='temporal' and d['effective_at'] and datetime.fromisoformat(d['effective_at'])>datetime.now(timezone.utc)
                    if not future:db.execute("UPDATE memories SET status='superseded',updated_at=? WHERE id=?",(now,old['id']))
                    published_targets.add(old['id']);live.pop(old['id'],None)
                    if action=='temporal' and d['effective_at']:db.execute('UPDATE memory_facts SET valid_until=? WHERE memory_id=?',(d['effective_at'],old['id']))
                duplicate=next((m for m in live.values() if normalized(m['text'])==normalized(item['text']) and m['category']==item['category'] and m.get('subject')==normalized(d['subject']) and m.get('attribute')==normalized(d['attribute']) and m.get('valid_from')==d['effective_at'] and m.get('valid_until')==d['valid_until']),None) if not old else None
                if duplicate:mid=duplicate['id'];state='deduplicated'
                else:
                    mid=identifier();root=old['root_id'] if old else mid;revision=db.execute('SELECT coalesce(max(revision),0)+1 FROM memories WHERE root_id=?',(root,)).fetchone()[0] if old else 1
                    db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,pid,LOCAL_USER_ID,item['category'],item['text'],sources[0]['source_id'],root,old['id'] if old else None,revision,'active',now,now))
                    db.execute('INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,?,?)',(mid,pid,LOCAL_USER_ID,encoded(selected),normalized(d['subject']),normalized(d['attribute']),d['effective_at'],d['valid_until'],bid,encoded({'decision':d,'proposal_id':item['id']})))
                    state='published'
                    live[mid]={'id':mid,'text':item['text'],'category':item['category'],'revision':revision,'subject':normalized(d['subject']),'attribute':normalized(d['attribute']),'valid_from':d['effective_at'],'valid_until':d['valid_until']}
                for s in sources:db.execute('INSERT OR IGNORE INTO memory_sources VALUES(?,?,?)',(mid,s['source_id'],s['quote']))
                # New evidence can resolve older quarantined versions of this slot.
                for prior in db.execute("SELECT * FROM memory_decisions WHERE persona_id=? AND state='quarantined'",(pid,)).fetchall():
                    pd=json.loads(prior['data_json'])['decision']
                    if normalized(pd['subject'])==normalized(d['subject']) and normalized(pd['attribute'])==normalized(d['attribute']):db.execute("UPDATE memory_decisions SET state='resolved',reason='new_supported_evidence' WHERE id=?",(prior['id'],))
            elif action=='quarantine':state='quarantined';quarantine=True
            detail={'proposal':item,'decision':d,'memory_id':mid,'blocked_memory_ids':[m['id'] for m in matches] if state=='quarantined' else []}
            db.execute('INSERT INTO memory_decisions VALUES(?,?,?,?,?,?,?,?,?,?)',(identifier(),pid,LOCAL_USER_ID,bid,item['id'],kind,state,reason,encoded(detail),now))
        previous=payload['automatic_memory']['previous_fragment']
        expired=[m for m in facts(db,pid,selected) if m.get('valid_until') and datetime.fromisoformat(m['valid_until'])<=datetime.now(timezone.utc)]
        kept=[i for i in kept if not any(normalized(i['text'])==normalized(m['text']) for m in expired)]
        if previous:db.execute("UPDATE memory_fragments SET state='superseded' WHERE id=?",(previous,))
        if kept:
            fragment=identifier();fp={k:v for k,v in payload.items() if k not in ('proposals',)}
            db.execute('INSERT INTO memory_fragments VALUES(?,?,?,?,?,?,?,?,?)',(fragment,pid,LOCAL_USER_ID,payload['session_id'],bid,previous,'active',encoded({'payload':fp,'items':kept}),now))
        db.execute("UPDATE memory_batches SET state='completed',updated_at=? WHERE id=?",(now,bid))
        quarantine=bool(db.execute("SELECT 1 FROM memory_decisions WHERE batch_id=? AND state='quarantined'",(bid,)).fetchone())
        disposition='processed_with_quarantine' if quarantine else ('processed' if decisions else 'no_memory')
        for b in payload['bindings']:
            db.execute("UPDATE memory_coverage SET disposition=CASE WHEN disposition='revoked' THEN disposition ELSE ? END,summary_state=?,updated_at=? WHERE turn_id=?",(disposition,'available' if kept else 'no_summary',now,b['turn_id']))
        self.compose_summary(db,payload['session_id'],selected,task_id=payload.get('_task_id') or (batch['verify_task_id'] or batch['extract_task_id']))

    def compose_summary(self,db,sid,selected,task_id):
        items=[];sources=set();refs={};rows=set()
        for f in db.execute("SELECT * FROM memory_fragments WHERE session_id=? AND state='active' ORDER BY rowid",(sid,)):
            data=json.loads(f['data_json']);p=data['payload']
            if p['scope']!=selected:continue
            try:validate_sources(db,p,check_memories=False)
            except RuntimeErrorBase:continue
            events={e['id']:e for e in p['events']}
            for item in data['items']:
                converted=[];item_refs={};item_sources=set();item_rows=set()
                for s in item['sources']:
                    e=events[s['source_id']];event=e['id']
                    if e['kind']=='part':
                        event=db.execute('SELECT t.reply_event_id FROM turns t JOIN reply_delivery_parts d ON d.turn_id=t.id WHERE d.id=?',(e['id'],)).fetchone()[0]
                    item_refs[e['id']]={'id':e['id'],'kind':e['kind'],'hash':digest(e['text'])};item_sources.add(event);item_rows.add(e['turn'])
                    converted.append({'event_id':event,'quote':s['quote']})
                items.append({'text':item['text'],'sources':converted,'_refs':item_refs,'_source_ids':item_sources,'_rows':item_rows})
        old=db.execute("SELECT coalesce(max(revision),0) FROM summaries WHERE session_id=?",(sid,)).fetchone()[0]
        db.execute("UPDATE summaries SET status='superseded' WHERE session_id=? AND status='active'",(sid,))
        if not items:return
        # Bounded, deterministic composition; it is explicitly a lossy view.
        bounded=[];size=0
        for item in reversed(items):
            if len(bounded)>=30 or size+len(item['text'])>3000:break
            bounded.insert(0,item);size+=len(item['text'])
        for item in bounded:
            refs.update(item.pop('_refs'));sources.update(item.pop('_source_ids'));rows.update(item.pop('_rows'))
        db.execute("UPDATE memory_coverage SET summary_state='no_summary' WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?) AND disposition IN ('processed','processed_with_quarantine','no_memory') AND summary_state!='invalid'",(sid,))
        for ordinal in rows:
            db.execute("UPDATE memory_coverage SET summary_state='available' WHERE turn_id IN (SELECT id FROM turns WHERE session_id=? AND rowid=?)",(sid,ordinal))
        db.execute('''INSERT INTO summaries(id,persona_id,user_id,session_id,channel,task_id,previous_id,revision,start_row,end_row,items_json,sources_json,covered_json,status,reason,created_at,source_refs_json)
            VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?,'active','automatic_memory',?,?)''',
            (identifier(),selected['persona_id'],LOCAL_USER_ID,sid,selected['channel'],task_id,old+1,min(rows),max(rows),encoded(bounded),encoded(sorted(sources)),encoded(sorted(rows)),timestamp(),encoded(list(refs.values()))))

    def tick(self,force=False):
        """Pure local coordination. All paid effects go through managed.register."""
        if self.engine.draining or self.engine.chat.draining:return
        with self.store.connection(write=True) as db:
            self.reconcile(db)
            policies=db.execute('SELECT p.* FROM organization_policies p WHERE revision=(SELECT max(revision) FROM organization_policies WHERE persona_id=p.persona_id)').fetchall()
            for policy in policies:
                pid=policy['persona_id'];settings=json.loads(policy['data_json'])
                if not settings['enabled'] or not settings.get('memory_automation',False):continue
                from .persona_management import active
                try:active(db,pid)
                except RuntimeErrorBase:continue
                for b in db.execute("SELECT * FROM memory_batches WHERE persona_id=? AND state IN ('extracting','verifying','waiting','blocked_input') ORDER BY rowid",(pid,)).fetchall():
                    batch=dict(b)
                    if batch['state'] in ('waiting','blocked_input'):
                        try:self.enqueue_verification(db,batch)
                        except RuntimeErrorBase as error:
                            if str(error)=='organization_queue_full':continue
                            state='blocked_input' if str(error)=='automatic_memory_verification_input_too_long' else 'stale'
                            db.execute('UPDATE memory_batches SET state=?,updated_at=? WHERE id=?',(state,timestamp(),batch['id']))
                            if state=='stale':db.execute("UPDATE memory_coverage SET disposition='stale' WHERE batch_id=? AND disposition='pending'",(batch['id'],))
                        continue
                    tid=batch['verify_task_id'] or batch['extract_task_id'];task=db.execute('SELECT * FROM organization_tasks WHERE id=?',(tid,)).fetchone()
                    if task['status'] not in ('queued','running','waiting_quota','completed'):
                        c=self.engine.managed.contract(db,tid);unknown=c['data']['phase']=='unknown' or task['status']=='blocked_unknown'
                        if task['error_code']=='automatic_memory_version_changed' and batch['verify_task_id']:
                            self.enqueue_verification(db,batch)
                            continue
                        state='cancelled_unknown' if unknown and task['status']=='cancelled' else 'blocked_unknown' if unknown else task['status']
                        db.execute('UPDATE memory_batches SET state=?,updated_at=? WHERE id=?',(state,timestamp(),batch['id']))
                        if state=='stale':db.execute("UPDATE memory_coverage SET disposition='stale' WHERE batch_id=? AND disposition='pending'",(batch['id'],))
                from .automatic_memory_schedule import schedule
                schedule(self,db,pid,settings,policy['created_at'],force=force)

    def reconcile(self,db):
        for row in db.execute("SELECT m.*,f.valid_until,f.data_json FROM memory_facts f JOIN memories m ON m.id=f.memory_id WHERE m.status='active' AND f.valid_until IS NOT NULL").fetchall():
            data=json.loads(row['data_json'])
            if datetime.fromisoformat(row['valid_until'])<=datetime.now(timezone.utc) and not data.get('expiry_applied'):
                supersede_fact(db,row,'fact_expired');data['expiry_applied']=True
                db.execute('UPDATE memory_facts SET data_json=? WHERE memory_id=?',(encoded(data),row['id']))
        # Always enforce withdrawal even when execution is paused.
        for f in db.execute("SELECT * FROM memory_fragments WHERE state='active'").fetchall():
            p=json.loads(f['data_json'])['payload']
            try:validate_sources(db,p,check_memories=False)
            except RuntimeErrorBase:invalidate_fragment(db,f)
        # An explicitly resumed policy may rebind only proved-undispatched work
        # or saved receipts. Cancelled, malformed and unknown effects stay put.
        for row in db.execute("SELECT * FROM organization_tasks WHERE status='blocked_authorization' AND error_code='organization_policy_changed'").fetchall():
            p=json.loads(row['payload_json'])
            if not p.get('automatic_memory') or not self.is_enabled(db,row['persona_id']):continue
            c=self.engine.managed.contract(db,row['id']);count=db.execute('SELECT count(*) FROM organization_attempts WHERE task_id=?',(row['id'],)).fetchone()[0]
            if c['data']['cancel_requested'] or count and c['data']['phase']!='received':continue
            c['policy_id']=self.engine.managed.policy(db,row['persona_id'])['id']
            try:self.engine.managed.check(db,dict(row),c,p)
            except RuntimeErrorBase:continue
            self.engine.managed.save(db,c);db.execute("UPDATE organization_tasks SET status='queued',error_code=NULL WHERE id=?",(row['id'],))
            db.execute("UPDATE memory_batches SET state=? WHERE id=?",('verifying' if p['automatic_memory']['stage']=='verify' else 'extracting',p['automatic_memory']['batch_id']))

    def coverage(self,persona_id=None):
        with self.store.connection() as db:
            pid=persona_id or self.store.current(db)
            from .persona_management import active
            active(db,pid)
            processing=[dict(r) for r in db.execute('SELECT disposition,count(*) AS count FROM memory_coverage WHERE persona_id=? GROUP BY disposition',(pid,))]
            summary=[dict(r) for r in db.execute('SELECT summary_state,count(*) AS count FROM memory_coverage WHERE persona_id=? GROUP BY summary_state',(pid,))]
            decisions=[dict(r) for r in db.execute('SELECT state,count(*) AS count FROM memory_decisions WHERE persona_id=? GROUP BY state',(pid,))]
            batches=[dict(r) for r in db.execute('SELECT id,purpose,state,extract_task_id,verify_task_id,created_at,updated_at FROM memory_batches WHERE persona_id=? ORDER BY rowid DESC LIMIT 200',(pid,))]
            total=db.execute("SELECT count(*) FROM turns WHERE persona_id=? AND status='completed'",(pid,)).fetchone()[0]
            registered=db.execute('SELECT count(*) FROM memory_coverage WHERE persona_id=?',(pid,)).fetchone()[0]
            policy=self.engine.managed.policy(db,pid)['data'];eligible=processed=summary_available=current_registered=0
            for row in db.execute("SELECT * FROM turns WHERE persona_id=? AND status='completed'",(pid,)):
                t=dict(row);selected=turn_scope(db,t)
                if selected['channel'] not in policy['channels'] or selected['channel']=='qq' and (selected['bot'],selected['peer'])!=(policy['qq']['bot'],policy['qq']['peer']):continue
                r=read_exact(db,selected,{'kind':'turn','id':t['id']})
                if r['status']!='found':continue
                eligible+=1;c=db.execute('SELECT * FROM memory_coverage WHERE turn_id=?',(t['id'],)).fetchone()
                if c and c['source_hash']==binding(r)['hash']:
                    current_registered+=1
                    if c['disposition'] in ('processed','processed_with_quarantine','no_memory'):processed+=1
                    if c['summary_state']=='available':summary_available+=1
            return {'enabled':self.is_enabled(db,pid),'processing':processing,'summary':summary,'decisions':decisions,'batches':batches,'completed_source_turns':total,'registered_source_turns':registered,'unregistered_turns':max(0,eligible-current_registered),'eligible_turns':eligible,'processed_turns':processed,'summary_available_turns':summary_available}

    def decisions(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM memory_decisions WHERE persona_id=? ORDER BY rowid DESC LIMIT 200',(self.store.current(db),))]

    def decision(self,decision_id):
        from .service import RuntimeService
        with self.store.connection() as db:return dict(RuntimeService._owned(db,'memory_decisions',decision_id,self.store.current(db)))

    def submit_range(self,db,preview,request_id,trigger):
        selected=preview.get('scope') or self.engine.managed.scope(db,preview)
        pid=preview['persona_id'];sid=preview['session_id'];requested=set(preview['rows'])
        if preview.get('rebuild'):raise RuntimeErrorBase('use_automatic_rebuild_preview')
        fp=digest([preview,trigger]);operation='organization-submission:'+request_id
        old=db.execute('SELECT * FROM memory_operations WHERE id=?',(operation,)).fetchone()
        if old:
            if old['fingerprint']!=fp:raise RuntimeErrorBase('submission_conflict')
            return json.loads(old['result_json'])
        turns=[dict(r) for r in db.execute('SELECT rowid AS ordinal,* FROM turns WHERE session_id=? ORDER BY rowid',(sid,)) if r['ordinal'] in requested]
        if len(turns)!=len(requested):raise RuntimeErrorBase('source_expired')
        known=[];missing=[]
        for t in turns:
            exact=read_exact(db,selected,{'kind':'turn','id':t['id']})
            if exact['status']!='found':raise RuntimeErrorBase('source_expired')
            c=db.execute('SELECT * FROM memory_coverage WHERE turn_id=?',(t['id'],)).fetchone()
            if c and c['source_hash']==binding(exact)['hash'] and c['batch_id'] and c['disposition'] not in ('unprocessed','stale'):known.append(c['batch_id'])
            else:missing.append(t)
        if missing:
            if db.execute("SELECT 1 FROM memory_batches WHERE scope_json=? AND state IN ('extracting','verifying','waiting')",(encoded(selected),)).fetchone():raise RuntimeErrorBase('organization_session_busy')
            payload=self.payload(db,pid,sid,selected,missing)
            if trigger in ('manual','sealed'):payload['automatic_memory']['manual_operation']=request_id
            b=self.begin(db,payload,trigger)
            if not b:raise RuntimeErrorBase('organization_effect_unknown')
        else:b=dict(db.execute('SELECT * FROM memory_batches WHERE id=?',(known[-1],)).fetchone())
        tid=b['verify_task_id'] or b['extract_task_id'];task=db.execute('SELECT * FROM organization_tasks WHERE id=?',(tid,)).fetchone()
        result=self.engine._view(task)|{'memory_batch_id':b['id'],'workflow_state':b['state'],'shared_coverage':True}
        db.execute('INSERT INTO memory_operations VALUES(?,?,?,?,?,?)',(operation,pid,LOCAL_USER_ID,fp,encoded(result),timestamp()))
        return result

    def override(self,decision_id,action,expected_selected,operation_id,text=None,target_memory_id=None,expected_revision=None):
        from .service import RuntimeService
        if action not in ('publish','revise','omit') or not isinstance(operation_id,str) or not operation_id:raise RuntimeErrorBase('invalid_memory_override')
        fp=digest([decision_id,action,expected_selected,text,target_memory_id,expected_revision])
        with self.store.connection(write=True) as db:
            old=db.execute('SELECT * FROM memory_operations WHERE id=?',(operation_id,)).fetchone()
            if old:
                if old['fingerprint']!=fp:raise RuntimeErrorBase('submission_conflict')
                return json.loads(old['result_json'])
            if self.store.current(db)!=expected_selected:raise RuntimeErrorBase('persona_selection_conflict')
            row=RuntimeService._owned(db,'memory_decisions',decision_id,expected_selected)
            if row['state']!='quarantined' or row['kind']!='candidates':raise RuntimeErrorBase('decision_not_overridable')
            batch=dict(db.execute('SELECT * FROM memory_batches WHERE id=?',(row['batch_id'],)).fetchone());p=json.loads(batch['data_json'])['payload']
            validate_sources(db,p,check_memories=False);p['existing_memories']=facts(db,expected_selected,p['scope'])
            detail=json.loads(row['data_json']);item=detail['proposal'];item=dict(item,id='manual-'+operation_id)
            if text is not None:
                if not isinstance(text,str) or not text.strip() or len(text)>500:raise RuntimeErrorBase('invalid_memory_override')
                item['text']=text
            d=dict(detail['decision'],id=item['id'],action=action,reason='explicit_human_override',sources=item['sources'],target_memory_id=target_memory_id)
            if action=='revise':
                target=next((m for m in p['existing_memories'] if m['id']==target_memory_id),None)
                if not target or target['revision']!=expected_revision:raise RuntimeErrorBase('memory_version_conflict')
                d.update(subject=target.get('subject',d['subject']),attribute=target.get('attribute',d['attribute']))
            elif target_memory_id is not None:raise RuntimeErrorBase('invalid_memory_override')
            db.execute("UPDATE memory_decisions SET state='overridden' WHERE id=?",(decision_id,))
            self.commit(db,batch,p,{'summary':[],'candidates':[item]},[d])
            result={'decision_id':decision_id,'action':action,'actor':'human','model_calls':0}
            db.execute('INSERT INTO memory_operations VALUES(?,?,?,?,?,?)',(operation_id,expected_selected,LOCAL_USER_ID,fp,encoded(result),timestamp()))
        return result

    def rebuild_preview(self,session_id,persona_id):
        with self.store.connection() as db:return self._rebuild_preview(db,session_id,persona_id)

    def _rebuild_preview(self,db,sid,pid):
        from .service import RuntimeService
        RuntimeService._owned(db,'sessions',sid,pid)
        rows=[dict(r) for r in db.execute("SELECT id,state,data_json FROM memory_fragments WHERE session_id=? AND state IN ('invalid','disabled') ORDER BY rowid",(sid,))]
        return {'persona_id':pid,'session_id':sid,'invalid_fragments':[r['id'] for r in rows],'fingerprint':digest(rows),'state':'repair_needed' if rows else 'no_action'}

    def rebuild_submit(self,preview,expected_selected,operation_id):
        if not isinstance(preview,dict) or not isinstance(operation_id,str) or not operation_id:raise RuntimeErrorBase('invalid_rebuild_submission')
        fp=digest([preview,expected_selected]);pid=preview['persona_id']
        with self.store.connection(write=True) as db:
            old=db.execute('SELECT * FROM memory_operations WHERE id=?',(operation_id,)).fetchone()
            if old:
                if old['fingerprint']!=fp:raise RuntimeErrorBase('submission_conflict')
                return json.loads(old['result_json'])
            if self.store.current(db)!=expected_selected or pid!=expected_selected:raise RuntimeErrorBase('persona_selection_conflict')
            if self._rebuild_preview(db,preview['session_id'],pid)!=preview:raise RuntimeErrorBase('source_expired')
            if not self.is_enabled(db,pid):raise RuntimeErrorBase('organization_policy_changed')
            for ident in preview['invalid_fragments']:
                row=db.execute('SELECT data_json FROM memory_fragments WHERE id=?',(ident,)).fetchone();data=json.loads(row[0]);data['manual_rebuild_operation']=operation_id
                db.execute("UPDATE memory_fragments SET state='invalid',data_json=? WHERE id=?",(encoded(data),ident))
            result={'state':preview['state'],'invalid_fragments':preview['invalid_fragments'],'automatic_continuation':True}
            db.execute('INSERT INTO memory_operations VALUES(?,?,?,?,?,?)',(operation_id,pid,LOCAL_USER_ID,fp,encoded(result),timestamp()))
        self.tick(force=True)
        return result
