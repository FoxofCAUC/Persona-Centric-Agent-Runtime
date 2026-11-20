"""Ordinary private-chat planning and receipt-driven publication."""
import json
import time

from .archive import encoded, fingerprint
from .call_limits import gate, turn_scope
from .expression_contract import text_hash
from .models import RuntimeErrorBase, identifier, timestamp
from .ordinary_materials import collect, selected_proof
from .private_reply_entry import ACTIONS, RESEARCH, stop
from .private_reply_output import ADVISORY_VERSIONS, FACT_REVIEW_VERSION, assess, limits, record_format
from .scene_style import short_frames
from .private_transport_recovery import PrivateTransportParked, park

SOURCE = 'maibot_private_reply'
# Historic output formats can request one local format repair.
REPAIR_GROUPS = {'target':{'J1','J2','J3','J6'}, 'length':{'J6'}}


def publication(db, tid):
    return db.execute("""SELECT p.* FROM expression_speech_units u JOIN expression_speech_publications p
        ON p.utterance_id=u.id WHERE u.turn_id=? AND u.source_kind=? AND u.phase='main'""",(tid,SOURCE)).fetchone()


def stage_node(base,node):
    return 'notice.'+node if base['phase']=='search_notice' else node


def unconfirmed_dispatch(db, tid):
    return bool(db.execute("""SELECT 1 FROM private_reply_attempts WHERE turn_id=? AND state='unknown'
        UNION ALL SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND state='unknown'
        AND node NOT IN ('J7','notice.J7') LIMIT 1""",(tid,tid)).fetchone())


def recover_turn(db, turn):
    tid = turn['id']
    from .desktop_recovery import contract, release_for_continuation
    ownership = contract(db, turn)
    if ownership:
        release_for_continuation(db, turn, ownership.get('owner'))
    if publication(db,tid):
        return True
    from .private_lookup import notice
    pending=notice(db,tid)
    if pending and pending['state'] in ('pending','partial','unknown'):
        return True
    db.execute("UPDATE private_lookup_calls SET state='unknown' WHERE turn_id=? AND state='sending'",(tid,))
    db.execute("UPDATE private_reply_nodes SET state='unknown' WHERE turn_id=? AND state='sending'",(tid,))
    db.execute("UPDATE private_reply_attempts SET state='unknown' WHERE turn_id=? AND state='sending'",(tid,))
    if unconfirmed_dispatch(db,tid):
        stop(db,tid,'reply_dispatch_unknown')
    elif not turn['cancelled_at']:
        db.execute("""UPDATE turns SET status='queued',error_code=CASE WHEN error_code IN
            ('desktop_network_wait','desktop_storage_wait') THEN error_code END WHERE id=?""",(tid,))
    return True


class OrdinaryReply:
    """Providers supply separately reviewed Choice and body-only transports."""
    def __init__(self,entry,providers):
        self.entry = entry
        self.store = entry.store
        self.providers = providers

    def _snapshot(self, db, tid, phase='main'):
        turn, base = self.entry._binding(db,tid,phase)
        check_scope = getattr(self.providers,'check_scope',None)
        if check_scope:
            check_scope(base)
        frozen = db.execute("SELECT * FROM private_reply_source_sets WHERE turn_id=?",(tid,)).fetchone()
        material = collect(self.entry,db,turn,base,json.loads(frozen['source_json']) if frozen else None)
        if frozen and fingerprint(material)!=frozen['material_hash']:
            raise RuntimeErrorBase('private_reply_source_changed')
        return turn,base,material

    def extend_contract(self, db, turn, base, proof):
        if not isinstance(proof,dict) or proof.get('draft') not in (0,1):
            raise RuntimeErrorBase('reply_decision_required')
        handoff=db.execute("""SELECT result_json FROM private_reply_nodes WHERE turn_id=? AND draft=?
            AND node=? AND state='received'""",(turn['id'],proof['draft'],stage_node(base,'handoff'))).fetchone()
        if not handoff or json.loads(handoff[0])!=proof:
            raise RuntimeErrorBase('reply_decision_required')
        _,_,material = self._snapshot(db,turn['id'],base['phase'])
        if proof.get('research'):
            from .private_lookup import validate_context
            validate_context(db,turn,base,proof['research'])
        if 'search_notice_event' in proof:
            from .private_lookup import presented_notice
            if base['phase']!='main' or presented_notice(db,turn['id'])!=proof['search_notice_event']:
                raise RuntimeErrorBase('private_lookup_scope_changed')
        if (proof['base']!=fingerprint(base) or proof['references']!=material['references']
                or selected_proof(material,proof['selected'])!=proof['selected']):
            raise RuntimeErrorBase('private_reply_source_changed')
        if proof['draft'] and not db.execute("""SELECT 1 FROM private_reply_reviews r JOIN private_reply_attempts a
                ON a.id=r.attempt_id WHERE a.turn_id=? AND a.phase=? AND a.draft=0 AND r.approved=0""",
                (turn['id'],base['phase'])).fetchone():
            raise RuntimeErrorBase('private_reply_repair_unavailable')
        return base | {'ordinary':proof}

    async def choice(self,tid,draft,node,state,options,base,material,*,receipt_node=None):
        choices = tuple(options)
        if not choices or len(set(choices))!=len(choices):
            raise RuntimeErrorBase('private_reply_invalid_choices')
        signature = fingerprint([base,material,self.providers.version,receipt_node or node,state,choices])
        key=stage_node(base,receipt_node or node)
        with self.store.connection(write=True) as db:
            turn,active,live = self._snapshot(db,tid,base['phase'])
            if active != base or live != material:
                raise RuntimeErrorBase('private_reply_source_changed')
            old = db.execute("SELECT * FROM private_reply_nodes WHERE turn_id=? AND draft=? AND node=?",
                             (tid,draft,key)).fetchone()
            if old:
                if old['request_hash']!=signature:
                    raise RuntimeErrorBase('private_reply_decision_stale')
                if old['state']!='received':
                    raise RuntimeErrorBase('reply_dispatch_unknown')
                answer = json.loads(old['result_json'])
            else:
                bind=getattr(self.providers,'bind_choose',None)
                choose=bind() if bind else self.providers.choose
                meter=gate(db,self.store,'private_jev',turn_scope(turn))
                if key=='J1' and draft==0:
                    selection={k:material[k] for k in ('history','memories')}
                    selection['admitted_ids']=list(material['catalog'])
                    db.execute("INSERT OR IGNORE INTO private_reply_source_sets VALUES(?,?,?)",
                               (tid,encoded(selection),fingerprint(material)))
                aid=identifier()
                db.execute("""INSERT INTO private_reply_nodes(turn_id,draft,node,attempt_id,request_hash,state)
                    VALUES(?,?,?,?,?,'sending')""",(tid,draft,key,aid,signature))
        if not old:
            try:
                answer,usage = await choose(node,aid,state,choices)
                with self.store.connection(write=True) as db:
                    db.execute("""UPDATE private_reply_nodes SET state='received',result_json=?,usage_json=?
                        WHERE attempt_id=? AND state IN ('sending','unknown')""",(encoded(answer),encoded(usage),aid))
            except (OSError,RuntimeErrorBase) as error:
                with self.store.connection(write=True) as db:
                    parked=park(db,turn,aid,'judge',signature,meter,error)
                if parked:
                    raise PrivateTransportParked('private_transport_parked') from error
                raise
            finally:
                with self.store.connection(write=True) as db:
                    db.execute("UPDATE private_reply_nodes SET state='unknown' WHERE attempt_id=? AND state='sending'",(aid,))
        if not isinstance(answer,str) or answer not in choices:
            raise RuntimeErrorBase('reply_decision_required')
        return answer

    def packet(self, material, decision):
        rows = material['catalog']
        core = material['persona']['core']
        return {'person':core['name'], 'relationship':material['persona']['relationship']['description'],
                'dialogue':[rows[x] for x in material['previous_exchange']],
                'current':[rows[x] for x in material['current']], 'targets':[rows[x] for x in decision['targets']],
                'evidence':[rows[x] for x in decision['facts']],
                'action':decision['action'],'stance':decision['stance'],'style':decision['style'],
                'mode':decision['mode'],'ending':decision['ending']}

    async def write(self,attempt,decision,contract):
        phase=contract['phase']
        proof=contract['ordinary']
        compact_notice=getattr(self.providers,'phase_scoped_search',False) and phase=='search_notice'
        with self.store.connection() as db:
            turn,base,material=self._snapshot(db,contract['turn'],phase)
            self.extend_contract(db,turn,base,proof)
            bind_phase=getattr(self.providers,'bind_phase_reply',None)
            bind=getattr(self.providers,'bind_reply',None)
            reply=bind_phase(phase) if bind_phase else bind() if bind else self.providers.reply
        if compact_notice:
            return await reply(attempt,{'phase':phase,'mode':decision['mode'],
                                        'output_constraints':proof['output_constraints']})
        packet=self.packet(material,decision)
        excluded=set(proof.get('superseded_outline_replies',[]))|set(proof.get('omitted_search_notices',[]))
        if excluded:
            packet['dialogue']=[row for row in packet['dialogue'] if row['id'] not in excluded]
        if 'search_notice_event' in proof:
            packet['search_notice_presented']=True
        packet['task_state']=proof['task']
        packet['repair_reason']=proof['repair_reason']
        for field in ('output_constraints','repair_observations'):
            if field in proof:
                packet[field]=proof[field]
        if proof.get('research'):
            packet['research']={k:v for k,v in proof['research'].items()
                                if k not in ('record_hash','j4_receipt','j5_receipt')}
        if 'research' in packet or 'output_constraints' in packet:
            packet['phase']=phase
        return await reply(attempt,packet)

    async def plan(self,tid,draft,base,material,reason,research=None,repair_observations=None):
        import asyncio
        rows=material['catalog']
        current=material['current']
        refs=material['references']
        excluded=set()
        scoped_search=getattr(self.providers,'phase_scoped_search',False)
        omitted_notices=set()
        search_notice_event=None
        if scoped_search and base['phase']=='main':
            from .private_lookup import presented_notice
            with self.store.connection() as db:
                search_notice_event=presented_notice(db,tid) if research else None
                omitted_notices={row[0] for row in db.execute("""SELECT p.canonical_event_id
                    FROM expression_speech_publications p JOIN expression_speech_units u ON p.utterance_id=u.id
                    WHERE u.source_kind=? AND u.phase='search_notice' AND p.canonical_event_id IS NOT NULL
                    AND u.turn_id IN (SELECT value FROM json_each(?))""",(SOURCE,encoded(material['history'])))}-set(refs)
        if (research and research['status']=='local_hit' and getattr(self.providers,'study_enabled',False)
                and not self._vector_turn(tid)):
            from .knowledge_study import superseded_outline_replies
            with self.store.connection() as db:
                excluded=superseded_outline_replies(db,tid,material['history'],research['facts'])-set(refs)
        recent=[i for i in material['recent'] if i not in excluded and i not in omitted_notices]
        initial={'current':[rows[i] for i in current],'references':[rows[i] for i in refs],
                 'recent':[rows[i] for i in recent]}
        iterations={}
        repair_groups=REPAIR_GROUPS[reason] if draft and not repair_observations else set()
        async def decide(name,state,options):
            iteration=draft if name.split('.')[0] in repair_groups else 0
            iterations[name]=iteration
            actual=state|{'repair':reason if iteration else None}
            return await self.choice(tid,iteration,name,actual,options,base,material)
        if research:
            target=research['target']
            iterations['J1']=0
        else:
            target = await decide('J1',initial,('wait','batch',*dict.fromkeys(current+refs)))
        if target == 'wait':
            if draft:
                raise RuntimeErrorBase('private_reply_review_rejected')
            return None
        targets = current if target=='batch' else [target]
        state={'target':[rows[i] for i in targets], 'current':initial['current'],
               'persona':material['persona'], 'recent':initial['recent'], 'repair':reason}
        if research and getattr(self.providers,'grounded_search',False):
            state['lookup']={'phase':base['phase'],'outcome':research.get('lookup_outcome',research['status']),
                             'selected_facts':len(research['facts'])}
        groups={'current':[], 'recent':recent, 'memory':material['memories']}
        options=list(dict.fromkeys(recent+material['memories']+material['character']))
        groups['character']=material['character']
        groups['combined']=options
        groups.update({i:[i] for i in options})
        styles={'profile':material['persona']['expression']['style']}
        variations=getattr(self.providers,'styles',{}).get(base['persona'],{})
        if 'profile' in variations or not all(isinstance(k,str) and isinstance(v,str) and v.strip() for k,v in variations.items()):
            raise RuntimeErrorBase('private_chat_review_incomplete')
        styles.update(variations)
        traits=material['persona']['core'].get('core_traits',[])
        stances={str(i):value for i,value in enumerate(traits)}
        stances['neutral']=None
        choices=await asyncio.gather(
            decide('J2.action',state,sorted(ACTIONS)),
            decide('J2.stance',state|{'stances':stances},stances),
            decide('J2.task',state,('none','open','closed')),
            decide('J3',{'current':initial['current'],'targets':targets,'candidates':[rows[i] for i in options],
                         'repair':reason},groups), return_exceptions=True)
        errors=[choice for choice in choices if isinstance(choice,BaseException)]
        if errors:
            raise next((exc for exc in errors if not isinstance(exc,PrivateTransportParked)),errors[0])
        action,stance,task,selection=choices
        style='profile' if not variations else await decide('J2.style',state|{'action':action,'styles':styles},styles)
        facts=list(dict.fromkeys(refs+groups[selection]))
        boundary=state|{'action':action,'task':task,'facts':[rows[i] for i in facts],'stance':stances[stance]}
        modes=['chat','short_answer']
        if research:
            boundary.update(research=research,phase=base['phase'])
            if base['phase']=='search_notice' or research['status']!='ordinary':
                modes=['short_answer']
            if base['phase']=='main' and research['explicit_long']:
                modes.append('long_detail')
        mode=await decide('J6.mode',boundary,tuple(modes))
        coverage=await decide('J6.coverage',boundary,('all',*facts))
        if coverage!='all':
            facts=list(dict.fromkeys(refs+[coverage]))
        ending=await decide('J6.ending',boundary,('stop','clarify') if action=='clarify' else ('stop',))
        if task=='closed' and ending=='clarify':
            raise RuntimeErrorBase('private_reply_decision_conflict')
        decision={'targets':targets,'action':action,'stance':stances[stance] or stance,'style':styles[style],
                  'facts':facts,'mode':mode,'ending':ending}
        proof={'base':fingerprint(base),'draft':draft,'references':refs,'task':task,'repair_reason':reason,
               'provider_version':self.providers.version,
               'selected':selected_proof(material,list(dict.fromkeys(targets+facts)))}
        if research:
            proof['research']=research
        if excluded:
            proof['superseded_outline_replies']=sorted(excluded)
        if omitted_notices:
            proof['omitted_search_notices']=sorted(omitted_notices)
        if search_notice_event:
            proof['search_notice_event']=search_notice_event
        output_version = getattr(self.providers,'output_contract_version',None)
        if output_version:
            proof['output_constraints']=limits(base['phase'],mode,compact_notice=scoped_search)
            if output_version in ADVISORY_VERSIONS:
                proof['output_contract_version']=output_version
            if repair_observations:
                proof['repair_observations']=repair_observations
        with self.store.connection(write=True) as db:
            _,now,live=self._snapshot(db,tid,base['phase'])
            if now!=base or material!=live:
                raise RuntimeErrorBase('private_reply_decision_stale')
            saved={(node,r):aid for node,r,aid in db.execute("""SELECT node,draft,attempt_id
                FROM private_reply_nodes WHERE turn_id=? AND state='received'""",(tid,))}
            receipts={node:saved.get(('J1' if node=='J1' and research else stage_node(base,node),r))
                      for node,r in iterations.items()}
            expected={'J1','J2.action','J2.task','J2.stance','J3','J6.mode','J6.coverage','J6.ending'}
            if variations:
                expected.add('J2.style')
            if set(receipts)!=expected or not all(receipts.values()):
                raise RuntimeErrorBase('reply_decision_required')
            proof.update(decision_hash=fingerprint(decision),node_receipts=receipts)
            db.execute("""INSERT OR IGNORE INTO private_reply_nodes
                VALUES(?,?,?,?,?,'received',?,NULL)""",
                (tid,draft,stage_node(base,'handoff'),identifier(),fingerprint(receipts),encoded(proof)))
        return decision,proof

    def validate_publication(self,db,turn,unit):
        record = db.execute("""SELECT a.body,a.body_sha256,d.contract_json,r.approved FROM private_reply_attempts a
            JOIN private_reply_reviews r ON r.attempt_id=a.id JOIN private_reply_decisions d ON d.id=a.decision_id
            WHERE a.id=? AND a.turn_id=? AND a.phase=? AND a.state='received'""",(unit['source_id'],turn['id'],unit['phase'])).fetchone()
        if (not record or not record['approved'] or record['body']!=unit['body']
                or text_hash(unit['body'])!=record['body_sha256'] or unit['source_hash']!=record['body_sha256']):
            raise RuntimeErrorBase('private_reply_unreviewed')
        bound=json.loads(record['contract_json'])
        if not self.providers or self.providers.version!=bound['ordinary']['provider_version']:
            raise RuntimeErrorBase('private_chat_review_version_changed')
        root,base=self.entry._binding(db,turn['id'],unit['phase'])
        actual=self.extend_contract(db,root,base,bound['ordinary'])
        if fingerprint(actual)!=fingerprint(bound):
            raise RuntimeErrorBase('private_reply_decision_stale')
        return root

    def publish(self,db,turn,attempt,decision,*,enforce_bubble_limit=True):
        outlet=self.entry.engine.expression.outlet
        unit=outlet._register(db,turn,attempt['phase'],SOURCE,attempt['id'],attempt['body_sha256'],attempt['body'],
                              {},{},{'logical_attempt_id':attempt['id'],'details':{}})
        from .expression_utterance_publication import SpeechPublication
        frames=short_frames(attempt['body'],decision['mode']=='chat')
        SpeechPublication(outlet).publish(db,unit,SOURCE,attempt['body'],frames,'private_review_accepted',
                                         enforce_bubble_limit=enforce_bubble_limit)
        db.execute("UPDATE turns SET status='running',error_code=NULL WHERE id=?",(turn['id'],))

    async def run(self,tid,phase):
        engine=self.entry.engine
        if phase!='main':
            raise RuntimeErrorBase('private_reply_phase_unavailable')
        async with engine.locks[('private_reply',tid)]:
            from . import desktop_recovery
            with self.store.connection(write=True) as db:
                turn=dict(db.execute("SELECT * FROM turns WHERE id=?",(tid,)).fetchone())
                if turn['cancelled_at'] or turn['status']=='completed' or publication(db,tid):
                    return
                if not desktop_recovery.claim(db,turn,self.entry.config.load(),engine.desktop_owner):
                    return
            try:
                await self._run(tid)
            except PrivateTransportParked:
                return
            except (RuntimeErrorBase,OSError,ValueError) as exc:
                from .call_limits import CallLimitPending
                from .local_repair import RepairInProgress
                if isinstance(exc,(CallLimitPending,RepairInProgress)):
                    raise
                if str(exc) in ('local_knowledge_storage_wait','local_knowledge_index_corrupt'):
                    from .local_lookup_route import recover_storage
                    recover_storage(self.store,turn,str(exc))
                    return
                with self.store.connection(write=True) as db:
                    unknown=unconfirmed_dispatch(db,tid)
                    stop(db,tid,str(exc) if isinstance(exc,RuntimeErrorBase) and not unknown else 'reply_dispatch_unknown')
            finally:
                with self.store.connection(write=True) as db:
                    desktop_recovery.release_for_continuation(db,turn,engine.desktop_owner)

    async def _run(self,tid):
        with self.store.connection(write=True) as db:
            if publication(db,tid):
                return
            if unconfirmed_dispatch(db,tid):
                raise RuntimeErrorBase('reply_dispatch_unknown')
            turn,base,material=self._snapshot(db,tid)
            if db.execute("""SELECT 1 FROM turns WHERE session_id=? AND rowid<(SELECT rowid FROM turns WHERE id=?)
                    AND status IN ('queued','running') AND cancelled_at IS NULL""",(turn['session_id'],tid)).fetchone():
                return
        if self.providers is None:
            raise RuntimeErrorBase('private_reply_transport_unavailable')
        research=None
        if base['route']==RESEARCH:
            if not getattr(self.providers,'research',False) or not callable(getattr(self.providers,'retrieve',None)):
                raise RuntimeErrorBase('private_reply_transport_unavailable')
            if getattr(self.providers,'study_enabled',False) and not callable(getattr(self.providers,'organize',None)):
                raise RuntimeErrorBase('private_reply_transport_unavailable')
            from .private_lookup import prepare
            research=await prepare(self,turn,base,material)
            if research is None:
                return
            if research['phase']!='main':
                with self.store.connection() as db:
                    turn,base,material=self._snapshot(db,tid,research['phase'])
        phase=base['phase']
        reason=None
        repair_observations=None
        for draft in (0,1):
            if draft:
                with self.store.connection() as db:
                    used=db.execute("SELECT phase FROM private_reply_attempts WHERE turn_id=? AND draft=1",(tid,)).fetchone()
                    if used and used[0]!=phase:
                        raise RuntimeErrorBase('private_reply_review_rejected')
                if research and research.get('origin') and reason=='target':
                    from .private_lookup import evidence,select_facts,validate_context
                    with self.store.connection() as db:
                        validate_context(db,turn,base,research)
                        data=evidence(db,turn['persona_id'],turn['session_id'],research['origin'],active_turn=tid)
                    research=await select_facts(self,turn,base,material,data,research,draft,reason)
            planned=await self.plan(tid,draft,base,material,reason,research,repair_observations)
            if planned is None:
                with self.store.connection(write=True) as db:
                    _,now,live=self._snapshot(db,tid)
                    if now!=base or live!=material:
                        raise RuntimeErrorBase('private_reply_source_changed')
                    db.execute("UPDATE turns SET status='completed',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),tid))
                    from .progress import record
                    record(db,tid,'private_reply_waited')
                return
            decision,proof=planned
            with self.store.connection() as db:
                contract=self.extend_contract(db,turn,base,proof)
                attempt=db.execute("SELECT * FROM private_reply_attempts WHERE turn_id=? AND draft=? AND phase=?",(tid,draft,phase)).fetchone()
            if not attempt:
                self.entry.accept_decision(tid,decision,fingerprint(contract),phase=phase,expires_at=time.time()+self.entry.config.load()['model']['timeout'],ordinary_proof=proof)
                await self.entry.dispatch(tid,phase,proof=proof)
            with self.store.connection() as db:
                attempt=db.execute("SELECT * FROM private_reply_attempts WHERE turn_id=? AND draft=? AND phase=?",(tid,draft,phase)).fetchone()
                if not attempt or attempt['state']!='received':
                    raise RuntimeErrorBase('reply_dispatch_unknown')
                saved=db.execute("SELECT contract_json,decision_json FROM private_reply_decisions WHERE id=?",(attempt['decision_id'],)).fetchone()
                if fingerprint(json.loads(saved[0]))!=fingerprint(contract) or json.loads(saved[1])!=decision:
                    raise RuntimeErrorBase('private_reply_decision_stale')
            constraints=proof.get('output_constraints')
            output_version=proof.get('output_contract_version')
            advisory=output_version in ADVISORY_VERSIONS
            issue=None
            receipt_id='publication/'+attempt['id']
            repair_observations=None
            if constraints:
                assessment=assess(attempt['body'],attempt['detail'],constraints,advisory=advisory)
                failures=assessment['failed_constraints']
                blocked=output_version!=FACT_REVIEW_VERSION and (
                    'detail_required' in failures if advisory else bool(failures))
                with self.store.connection(write=True) as db:
                    fmt_receipt=record_format(db,attempt,constraints,assessment,blocks_review=blocked)
                if blocked:
                    issue,receipt_id='length',fmt_receipt
                    repair_observations=assessment|{'required_fact_qualifiers':[
                        {k:fact[k] for k in ('id','date','record_source_dates','qualifications') if k in fact}
                        for fact in (research or {}).get('facts',[])]}
            else:
                issue=self._legacy_format_issue(attempt,decision,phase,research)
            if not issue and research and phase=='main' and output_version!=FACT_REVIEW_VERSION:
                # The frozen older format requires a distinct post-lookup body.
                from .private_lookup import notice
                with self.store.connection() as db:
                    spoken=notice(db,tid)
                    if spoken and spoken['state']=='complete' and spoken['body'].strip()==attempt['body'].strip():
                        issue='target'
            with self.store.connection(write=True) as db:
                root,now,live=self._snapshot(db,tid,phase)
                if now!=base or live!=material:
                    raise RuntimeErrorBase('private_reply_source_changed')
                previous=db.execute("SELECT approved,reason,receipt_id FROM private_reply_reviews WHERE attempt_id=?",
                                    (attempt['id'],)).fetchone()
                if previous:
                    if previous['approved']:
                        self.publish(db,root,attempt,decision,enforce_bubble_limit=not advisory)
                        return
                    if previous['reason']!=issue:
                        raise RuntimeErrorBase('private_reply_review_rejected')
                else:
                    db.execute("INSERT INTO private_reply_reviews VALUES(?,?,?,?)",
                               (attempt['id'],issue is None,issue or 'local_publication_ready',receipt_id))
                    if issue is None:
                        self.publish(db,root,attempt,decision,enforce_bubble_limit=not advisory)
                        return
            reason=issue
        raise RuntimeErrorBase('private_reply_review_rejected')

    def _vector_turn(self,tid):
        from .semantic_knowledge import active
        with self.store.connection() as db:
            return active(db,tid)

    @staticmethod
    def _legacy_format_issue(attempt,decision,phase,research):
        from .bubble_limits import BubbleLimitError,MAX_BUBBLE_CHARS,limit_ranges
        from .private_lookup import single_sentence
        body,mode=attempt['body'],decision['mode']
        if mode=='short_answer' and len(body)>MAX_BUBBLE_CHARS:
            return 'length'
        if research:
            if phase=='main' and mode in ('short_answer','long_detail') and not single_sentence(body):
                return 'length'
            if mode=='long_detail' and not attempt['detail']:
                return 'length'
        try:
            limit_ranges(body,short_frames(body,mode!='short_answer'))
        except BubbleLimitError:
            return 'length'
        return None
