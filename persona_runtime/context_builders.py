"""Task-specific context projections. They never create cognitive state."""
import json
import sqlite3
import subprocess
import time
from itertools import zip_longest

from .archive import scope,turn_scope,read_exact,binding,fingerprint
from .archive_corpus import capture_connection
from .archive_index import ArchiveIndex
from .archive_query import Budget,SearchFailure,lexical,normal
from .models import RuntimeErrorBase
from .turn_input import load_turn_input

BUILDER_VERSION='cognitive-context-v1'
TASK_TYPES=('dialogue','reflection','affect','director','planning')


def policy_for(turn):
    if not turn.get('context_policy_snapshot'):
        return {'context_mode':'legacy_recent','cross_session':False,'origin':'legacy_without_context_snapshot'}
    value=json.loads(turn['context_policy_snapshot'])
    if (not isinstance(value,dict) or value.get('schema_version')!=1 or value.get('context_mode') not in ('working_set','legacy_recent')
            or type(value.get('cross_session')) is not bool):raise RuntimeErrorBase('invalid_context_policy_snapshot')
    return value|{'origin':'accepted'}


def pool(conn,turn,budget,*,keys_only=False):
    selected=turn_scope(conn,turn)
    selected=scope(selected['persona_id'],selected['channel'],selected['bot'],selected['peer'])
    policy=policy_for(turn)
    cross=policy['cross_session'] and policy['context_mode']=='working_set'
    spec={'scope':selected,'session_id':None if cross else turn['session_id'],
          'window':'dialogue_cross_50' if cross else 'dialogue_session_50','query':''}
    ordinal=conn.execute('SELECT rowid FROM turns WHERE id=?',(turn['id'],)).fetchone()[0]
    corpus=capture_connection(conn,spec,budget,before_order=ordinal,prefer_session=turn['session_id'] if cross else None,keys_only=keys_only)
    return spec,corpus


def summary_usable(conn,turn,summary):
    # Old QQ summaries contain generated semantics, not a confirmed send
    # snapshot. Keep them in the archive without promoting them to sent facts.
    if turn['channel']=='qq':return False
    ids=json.loads(summary['sources_json'])
    if len(ids)>200:return False
    selected=turn_scope(conn,turn)
    return all(read_exact(conn,selected,{'kind':'event','id':i})['status']=='found' for i in ids)


def memory_usable(conn,turn,memory):
    """Human corrections remain authoritative; withdrawn evidence does not."""
    from .archive import find_turn
    from .turn_input import load_turn_sources
    ids={memory['source_event_id']}
    ids.update(r[0] for r in conn.execute('SELECT event_id FROM memory_sources WHERE memory_id=?',(memory['id'],)))
    selected=turn_scope(conn,turn)
    for ident in ids:
        event=conn.execute('SELECT * FROM events WHERE id=? AND persona_id=? AND user_id=?',
                           (ident,turn['persona_id'],selected['user_id'])).fetchone()
        if not event:return False
        source=find_turn(conn,'event',ident)
        if source:
            if source['status']!='completed' or turn_scope(conn,source)!=selected:return False
            if turn['channel']=='qq' and event['role']!='user':return False
            related=load_turn_sources(conn,source)
        else:
            # Legacy manual notes have no platform identity. They are only
            # available on the local desktop, never inferred to be QQ data.
            if turn['channel']!='desktop':return False
            raw=conn.execute('SELECT channel FROM raw_events WHERE event_id=?',(ident,)).fetchone()
            if raw and raw[0]!='desktop':return False
            if conn.execute("SELECT 1 FROM channel_sessions WHERE session_id=? AND channel!='desktop'",(event['session_id'],)).fetchone():return False
            related=[ident]
        for source_id in related:
            blocked=conn.execute('SELECT reason FROM source_exclusions WHERE event_id=?',(source_id,)).fetchone()
            if blocked and not (memory['revision']>1 and memory['previous_id'] and blocked[0]=='memory_revised'):
                return False
    return True


def query_terms(members):
    lists=[];literals=[];omitted=0
    for member in members:
        # Chat input is a sentence, not the administrator's exact query DSL.
        # Extract supported lexical pieces through ordinary CJK punctuation;
        # keep the public all_terms/phrase contract unchanged.
        tokens,_=lexical(member['text'],document=True)
        literal=not tokens
        kept=[t for t in tokens if len(t)<=256]
        omitted+=len(tokens)-len(kept);lists.append(kept)
        if literal and member['text'].strip() and len(member['text'])<=256:literals.append(normal(member['text']))
    selected=[];served=set()
    for row in zip_longest(*lists):
        for index,term in enumerate(row):
            if term is None or term in selected:continue
            if len(selected)>=32:omitted+=1;continue
            selected.append(term);served.add(members[index]['id'])
    return selected,literals,{'term_count':len(selected),'terms_hash':fingerprint(selected),
        'omitted_terms':omitted,'member_ids':[m['id'] for m in members if m['id'] in served],'literal_member_count':len(literals)}


def choose(corpus,documents,terms,literals):
    groups=corpus['groups'];recent=groups[:2]
    recent_ids={g['turn_id'] for g in recent};orders=corpus['orders']
    termsets={d['document_id']:set(lexical(d['text'],document=True)[0]) for d in corpus['documents']}
    counts={term:sum(term in s for s in termsets.values()) for term in terms}
    scores={};targets={}
    for doc in documents:
        hits=set(terms)&termsets[doc['document_id']]
        literal=any(value in normal(doc['text']) for value in literals)
        if not hits and not literal:continue
        tid=doc['turn_id'];rank=(-len(hits)-int(literal),-sum(1+len(termsets)//(1+counts[t]) for t in hits),-orders[tid],doc['document_id'])
        if tid not in scores or rank<scores[tid]:scores[tid]=rank
        targets.setdefault(tid,[]).append(doc['target'])
    related=[tid for tid in sorted(scores,key=lambda t:scores[t]) if tid not in recent_ids][:3]
    by_id={g['turn_id']:g for g in groups}
    items=[{'group':g,'kind':'recent','rank':n} for n,g in enumerate(recent)]
    items += [{'group':by_id[tid],'kind':'retrieval','rank':n} for n,tid in enumerate(related)]
    return items,targets


class DialogueBuilder:
    def __init__(self,store,engine):
        self.store=store;self.engine=engine;self.index=ArchiveIndex(store)

    def render(self,conn,turn,evidence):
        from .context_m4 import build_context
        return build_context(self.engine,conn,turn,evidence)

    def evidence(self,turn,*,fallback=False):
        budget=Budget();started=time.perf_counter();mode=policy_for(turn)['context_mode'];reason=None
        corpus={'groups':[],'documents':[],'orders':{},'digest':None};terms=[];literals=[];query_info={}
        if turn['channel']=='test':return None
        from .references import prepare
        # Explicit references are mandatory and must not be converted into a
        # successful optional-retrieval fallback.
        with self.store.connection() as conn:prepare(conn,turn)
        try:
            with self.store.connection() as conn:
                spec,corpus=pool(conn,turn,budget)
                terms,literals,query_info=query_terms(load_turn_input(conn,turn))
            if mode=='legacy_recent' or fallback:
                items=[{'group':g,'kind':'recent','rank':n} for n,g in enumerate(corpus['groups'][:10 if mode=='legacy_recent' else 2])]
                reason='source_changed' if fallback else None
                targets={}
            else:
                with self.index.locked():
                    state=self.index.synchronize(corpus,spec,budget)
                    docs,path=self.index.candidates(spec,state,budget,any_terms=True,terms_override=terms,include_literals=bool(literals))
                with self.store.connection() as conn:_,fresh=pool(conn,turn,budget)
                canonical={d['document_id']:d for d in fresh['documents']}
                if corpus['digest']!=fresh['digest'] or any(d!=canonical.get(d.get('document_id')) for d in docs):
                    corpus=fresh;raise SearchFailure('index_stale')
                items,targets=choose(fresh,docs,terms,literals);corpus=fresh
                query_info|={'cache_instance_id':state['cache_instance_id'],'generation':state['generation'],
                             'tokenizer_version':state['tokenizer_version'],'path':path,'degraded_fts':not state['fts']}
        except (SearchFailure,RuntimeErrorBase,sqlite3.Error,OSError,ValueError,subprocess.SubprocessError) as error:
            allowed={'cache_busy','cache_corrupt','index_stale','query_budget_exceeded','unavailable'}
            reason=str(error) if str(error) in allowed else 'retrieval_unavailable'
            items=[{'group':g,'kind':'recent','rank':n} for n,g in enumerate(corpus['groups'][:2])];targets={}
        return {'items':items,'pool':corpus['groups'],'orders':corpus['orders'],'context_mode':mode,
                'retrieval':{'mode':mode,'policy':policy_for(turn),'window_turn_ids':[g['turn_id'] for g in corpus['groups']],
                    'window_count':len(corpus['groups']),'source_digest':corpus['digest'],'matched_targets':targets,
                    'degraded_reason':reason,'query':query_info,'retrieval_ms':round((time.perf_counter()-started)*1000,3)}}

    def basic_evidence(self,conn,turn):
        try:
            _,corpus=pool(conn,turn,Budget())
        except (RuntimeErrorBase,SearchFailure,sqlite3.Error):
            corpus={'groups':[],'orders':{},'digest':None}
        mode=policy_for(turn)['context_mode']
        return {'items':[{'group':g,'kind':'recent','rank':n} for n,g in enumerate(corpus['groups'][:10 if mode=='legacy_recent' else 2])],
                'pool':corpus['groups'],'orders':corpus['orders'],'context_mode':mode,
                'retrieval':{'mode':mode,'policy':policy_for(turn),'window_turn_ids':[g['turn_id'] for g in corpus['groups']],
                    'window_count':len(corpus['groups']),'source_digest':corpus['digest'],'degraded_reason':'source_changed',
                    'query':{},'matched_targets':{},'retrieval_ms':0}}


class ReflectionBuilder:
    """M4 organization adapter, not autonomous reflection or session sealing."""
    @staticmethod
    def build(task,payload,messages):
        evidence=[{'kind':'event','id':e['id'],'hash':fingerprint(e['text'])} for e in payload.get('events',[])]
        previous=payload.get('previous')
        refs=([{'kind':'summary','id':previous['id'],'revision':previous['revision']}] if previous else [])
        refs += [{'kind':'memory','id':m['id'],'revision':m['revision']} for m in payload.get('existing_memories',[])]
        return {'purpose':'provider_probe' if task['mode']=='test' else 'memory_organization','evidence_refs':evidence,
                'included_state_refs':refs,'missing_data':['session_sealing_not_implemented','belief_state_not_implemented'],
                'hard_constraints':['source_guard','exact_source_quotes','existing_M4_approval_policy'],
                'soft_tendencies':[],'input_snapshot_hash':fingerprint(messages),'builder_version':BUILDER_VERSION}


class AffectBuilder:
    @staticmethod
    def build():
        return {'ready':False,'missing_data':['trait_state','mood_state','affect_decay_parameters'],
                'hard_constraints':['manual_state_is_not_inferred_affect','no_state_mutation'],'soft_tendencies':[]}


class DirectorBuilder:
    @staticmethod
    def build():
        return {'ready':False,'missing_data':['governance_proposal','risk_policy','before_after_diff'],
                'hard_constraints':['no_automatic_submission','no_state_mutation'],'soft_tendencies':[]}


class PlanningBuilder:
    @staticmethod
    def build():
        return {'ready':False,'missing_data':['goal_state','proactive_authorization','cooldown_state','quiet_hours'],
                'hard_constraints':['no_goal_creation','no_proactive_actions'],'soft_tendencies':[]}
