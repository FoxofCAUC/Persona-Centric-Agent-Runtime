"""Bounded local preparation and transient dialogue character selections.

No network, authority writes, or persistent worker is owned by this module.
Only the service lifecycle starts discovery; read/preview never enqueue work.
"""
from collections import OrderedDict, deque
import json
import sqlite3
import threading
import time

from .character import CharacterService
from .character_context import select_context, render_parts
from .character_embedding import CharacterEmbeddingProvider, SemanticFailure, digest
from .character_policy import read, choose
from .character_retrieval import CharacterCandidateRetriever, FALLBACK_CODES
from .character_semantic_index import CharacterSemanticIndex, TEMPLATE_VERSION, sources
from .models import RuntimeErrorBase
from .turn_input import load_turn_input, input_text


def validate_snapshot(conn, turn):
    raw = turn.get('character_snapshot')
    if raw is None:return None
    try:
        value = json.loads(raw)
        version = CharacterService(None)._version(conn, turn['persona_id'], value['id'])
        if value != version:raise ValueError()
    except (RuntimeErrorBase, ValueError, TypeError, KeyError):
        raise RuntimeErrorBase('character_snapshot_invalid') from None
    return value


class PreparationCoordinator:
    """One executing job, 32 waiting jobs; terminal diagnostics bounded to 128."""
    def __init__(self, build):
        self.build = build
        self.condition = threading.Condition()
        self.pending = deque()
        self.states = OrderedDict()
        self.running = None
        self.closed = False
        self.worker = None

    def submit(self, key, payload, *, retry=False):
        with self.condition:
            if self.closed:return {'state':'stopped', 'task_key':key}
            old = self.states.get(key)
            if old and (old['state'] in ('queued','running') or not retry):return dict(old)
            if len(self.pending) >= 32:
                self.states[key]={'state':'queue_full','task_key':key};self._trim()
                return dict(self.states[key])
            self.states[key] = {'state':'queued','task_key':key}
            self.pending.append((key,payload))
            if self.worker is None:
                self.worker = threading.Thread(target=self._work, name='pcar-character-prepare', daemon=True)
                self.worker.start()
            self.condition.notify_all()
            return dict(self.states[key])

    def _trim(self):
        while len(self.states)>128:
            terminal=next((k for k,v in self.states.items() if v['state'] not in ('queued','running')),None)
            if terminal is None:break
            del self.states[terminal]

    def _work(self):
        while True:
            with self.condition:
                while not self.pending and not self.closed:self.condition.wait()
                if self.closed:return
                key,payload = self.pending.popleft();self.running=key
                self.states[key]={'task_key':key,'state':'running'}
            try:
                self.build(*payload)
                result={'state':'ready'}
            except (RuntimeErrorBase,OSError,MemoryError) as exc:
                # Readiness is independently inspectable; no exception bodies/paths in logs.
                result={'state':'failed','reason':str(exc) if isinstance(exc,SemanticFailure) else 'local_preparation_failed'}
            except Exception:
                result={'state':'failed','reason':'local_preparation_failed'}
            with self.condition:
                self.states[key]={'task_key':key,**result};self.states.move_to_end(key);self.running=None
                self._trim()
                self.condition.notify_all()

    def status(self,key):
        with self.condition:return dict(self.states.get(key,{'task_key':key,'state':'not_requested'}))

    def close(self):
        with self.condition:
            self.closed=True
            for key,_ in self.pending:self.states[key]={'task_key':key,'state':'stopped'}
            self.pending.clear();self.condition.notify_all()
        if self.worker:self.worker.join(timeout=1)
        # An executing local inference cannot be forcibly cancelled. Its daemon
        # owns only disposable cache work; it cannot keep the service alive.


class CharacterRuntime:
    def __init__(self,store,index_factory=None,wait_seconds=1.0):
        self.store=store;self.wait_seconds=wait_seconds
        self.index_factory=index_factory or self._make_index
        self.indexes={};self.index_lock=threading.Lock()
        self.query_gate=threading.Lock();self.closed=False;self.discovery_error=None
        self.coordinator=PreparationCoordinator(self._build)

    def _make_index(self,config):
        provider=CharacterEmbeddingProvider(self.store.paths,self.store.paths.home/'models/character-embedding'/config['model_fingerprint'])
        return CharacterSemanticIndex(self.store,provider)

    def index(self,config):
        key=config['model_fingerprint']
        with self.index_lock:
            if key not in self.indexes:self.indexes[key]=self.index_factory(config)
            return self.indexes[key]

    def _build(self,pid,version,policy,rebuild):
        index=self.index(policy['config'])
        diagnostic=index.provider.diagnose()
        if diagnostic['status']!='ready':raise SemanticFailure(diagnostic['reason'])
        return index.build(pid,version,rebuild=rebuild)

    def _key(self,pid,version,policy):
        return digest([pid,version,policy['config']['model_fingerprint'],TEMPLATE_VERSION])

    def enqueue(self,pid,version,policy,*,retry=False):
        return self.coordinator.submit(self._key(pid,version,policy),(pid,version,policy,retry),retry=retry)

    def discover(self,persona_id=None):
        """Post-commit/startup only. Discovery failure never undoes a save."""
        needs=[]
        with self.store.connection() as conn:
            for row in conn.execute('SELECT p.persona_id,p.data_json,c.version_id FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id JOIN character_heads c ON c.persona_id=p.persona_id WHERE (? IS NULL OR p.persona_id=?)',(persona_id,persona_id)):
                policy=read(json.loads(row['data_json']).get('context',{}))
                if policy['mode'] in ('hybrid','hybrid_llm'):needs.append((row['persona_id'],row['version_id'],policy))
            for row in conn.execute("SELECT persona_id,character_snapshot,context_policy_snapshot FROM turns WHERE status IN ('queued','running','interrupted') AND (? IS NULL OR persona_id=?)",(persona_id,persona_id)):
                if not row['character_snapshot']:continue
                policy=read(json.loads(row['context_policy_snapshot'] or '{}'))
                if policy['mode'] in ('hybrid','hybrid_llm'):needs.append((row['persona_id'],json.loads(row['character_snapshot'])['id'],policy))
            for row in conn.execute("SELECT persona_id,character_snapshot,policy_json FROM input_batches WHERE state IN ('collecting','sealed_waiting_capacity','interrupted_buffer') AND (? IS NULL OR persona_id=?)",(persona_id,persona_id)):
                if not row['character_snapshot']:continue
                data=json.loads(row['policy_json'])
                policy=read(data.get('context',{}))
                if policy['mode'] in ('hybrid','hybrid_llm'):needs.append((row['persona_id'],json.loads(row['character_snapshot'])['id'],policy))
        return [self.enqueue(*n) for n in needs]

    def notify(self,persona_id):
        try:
            result=self.discover(persona_id);self.discovery_error=None;return result
        except (RuntimeErrorBase,ValueError,TypeError,KeyError,OSError,sqlite3.Error):
            self.discovery_error='discovery_failed'
            return [{'state':'failed','reason':self.discovery_error}]

    def _scope(self,persona_id,version_id,channel):
        if channel not in ('desktop','qq'):raise RuntimeErrorBase('unsupported_channel')
        with self.store.connection() as conn:
            version=CharacterService(self.store)._version(conn,persona_id,version_id)
            from .context_policy import current
            policy=read(current(conn,persona_id,channel))
        return version,policy

    def status(self,persona_id,version_id=None,channel='desktop'):
        version,policy=self._scope(persona_id,version_id,channel)
        if policy['mode'] not in ('hybrid','hybrid_llm'):policy=choose('hybrid')
        try:
            index=self.index(policy['config']);spec=index._spec(version)
            index._read(spec,sources(version))
            cache={'state':'ready','index_fingerprint':digest(spec)}
        except (SemanticFailure,OSError,MemoryError) as exc:
            cache={'state':'unavailable','reason':str(exc) if isinstance(exc,SemanticFailure) else 'local_resource_failure'}
        return {'persona_id':persona_id,'version_id':version['id'],'model_fingerprint':policy['config']['model_fingerprint'],
                'cache':cache,'discovery_error':self.discovery_error,'preparation':self.coordinator.status(self._key(persona_id,version['id'],policy))}

    def request_prepare(self,persona_id,version_id=None,channel='desktop'):
        version,policy=self._scope(persona_id,version_id,channel)
        with self.store.connection() as conn:
            if self.store.current(conn)!=persona_id:raise RuntimeErrorBase('character_selection_conflict')
        if policy['mode'] not in ('hybrid','hybrid_llm'):policy=choose('hybrid')
        return self.enqueue(persona_id,version['id'],policy,retry=True)

    def local_pool(self,version,policy,members,scenario=()):
        from .character_selector_protocol import candidates
        if not self.query_gate.acquire(blocking=False):raise SemanticFailure('embedding_busy')
        try:
            index=self.index(policy['config'])
            raw=index.query(version['persona_id'],version['id'],members,per_kind_limit=10)
            if raw['version_id']!=version['id']:raise SemanticFailure('character_source_changed')
            if raw['model_fingerprint']!=policy['config']['model_fingerprint']:raise SemanticFailure('embedding_dependency_mismatch')
            result=candidates(version,members,raw['by_kind'],policy['config'],scenario)
            result.update(model_fingerprint=raw['model_fingerprint'],index_fingerprint=raw['index_fingerprint'])
            return result
        finally:self.query_gate.release()

    def _hybrid(self,version,policy,members,scenario,limit):
        return CharacterCandidateRetriever(self.store,self.index(policy['config']),policy['config']).retrieve(
            version['persona_id'],version['id'],members,scenario=scenario,example_limit=limit)

    def selection(self,version,policy,members,scenario=(),limit=2):
        if policy['mode']=='hybrid_llm':
            value=select_context(version,'\n\n'.join(members),members=members,scenario=scenario,example_limit=limit)
            value.update(requested_mode='hybrid_llm',effective_mode='lexical',degrade_reason='preview_cloud_not_called')
            try:value['candidate_preview']=self.local_pool(version,policy,members,scenario)['payload']
            except SemanticFailure as exc:value['degrade_reason']=str(exc)
            return value
        started=time.monotonic();result=None;reason=None
        if policy['mode']=='hybrid':
            if self.closed:reason='character_runtime_stopped'
            elif not self.query_gate.acquire(blocking=False):reason='embedding_busy'
            else:
                done=threading.Event();box={}
                def query():
                    try:box['value']=self._hybrid(version,policy,members,scenario,limit)
                    except Exception as exc:box['error']=exc
                    finally:self.query_gate.release();done.set()
                threading.Thread(target=query,name='pcar-character-query',daemon=True).start()
                if not done.wait(max(0,self.wait_seconds-(time.monotonic()-started))):reason='character_semantic_timeout'
                elif 'error' in box:
                    error=box['error']
                    if isinstance(error,SemanticFailure) and str(error) in FALLBACK_CODES:reason=str(error)
                    elif isinstance(error,(OSError,MemoryError)):reason='character_semantic_resource_failure'
                    else:raise error
                else:result=box['value']
        if result is None:
            result=select_context(version,'\n\n'.join(members),members=members,scenario=scenario,example_limit=limit)
            result.update(requested_mode=policy['mode'],effective_mode='lexical',degrade_reason=reason)
        result['preparation_ms']=round((time.monotonic()-started)*1000,3)
        result['config_fingerprint']=policy.get('config_fingerprint')
        result['policy_fingerprint']=digest(policy)
        if policy['mode']=='hybrid':result.setdefault('model_fingerprint',policy['config']['model_fingerprint'])
        return result

    def prepare(self,turn,selected=None):
        from .context_builders import policy_for
        frozen_policy=policy_for(turn);policy=read(frozen_policy)
        if turn['channel']=='test':return None
        with self.store.connection() as conn:
            version=validate_snapshot(conn,turn)
            if version is None:return None
            members=[m['text'] for m in load_turn_input(conn,turn)];text=input_text(conn,turn)
        from .behavior import resolve_behavior
        contextual=(turn.get('reply_mode_snapshot') or 'legacy_full')!='legacy_full'
        scenario=[s['act'] for s in resolve_behavior(text)['signals']] if contextual else []
        if policy['mode']=='hybrid_llm':
            result=selected or select_context(version,'\n\n'.join(members),members=members,scenario=scenario,example_limit=4 if contextual else 2)
            result.setdefault('requested_mode','hybrid_llm');result.setdefault('effective_mode','lexical');result.setdefault('degrade_reason','selector_not_run')
            result['config_fingerprint']=policy.get('config_fingerprint');result['policy_fingerprint']=digest(policy)
        else:result=self.selection(version,policy,members,scenario,4 if contextual else 2)
        result['policy_id']=frozen_policy.get('policy_id');result['policy_revision']=frozen_policy.get('revision')
        result['algorithm']=policy.get('config',{}).get('algorithm')
        result['_guard']=digest([turn.get('character_snapshot'),turn.get('context_policy_snapshot'),members,turn.get('reply_mode_snapshot'),turn.get('selector_model_snapshot'),turn.get('execution_revision')])
        result['_selection_hash']=digest(result)
        return result

    def recheck(self,conn,turn,result):
        from .context_builders import policy_for
        frozen_policy=policy_for(turn);policy=read(frozen_policy)
        if turn['channel']=='test':return None
        version=validate_snapshot(conn,turn)
        if version is None:return None
        members=[m['text'] for m in load_turn_input(conn,turn)]
        guard=digest([turn.get('character_snapshot'),turn.get('context_policy_snapshot'),members,turn.get('reply_mode_snapshot'),turn.get('selector_model_snapshot'),turn.get('execution_revision')])
        if result and result.get('_guard')==guard and result.get('_selection_hash')==digest({k:v for k,v in result.items() if k!='_selection_hash'}):return result
        # No index access, waits or embedding are permitted on this path.
        from .behavior import resolve_behavior
        contextual=(turn.get('reply_mode_snapshot') or 'legacy_full')!='legacy_full'
        scenario=[s['act'] for s in resolve_behavior(input_text(conn,turn))['signals']] if contextual else []
        fallback=select_context(version,'\n\n'.join(members),members=members,scenario=scenario,example_limit=4 if contextual else 2)
        fallback.update(_guard=guard,requested_mode=policy['mode'],effective_mode='lexical',
                        degrade_reason='character_preparation_invalidated',preparation_ms=0,
                        config_fingerprint=policy.get('config_fingerprint'),policy_fingerprint=digest(policy))
        fallback.update(policy_id=frozen_policy.get('policy_id'),policy_revision=frozen_policy.get('revision'),algorithm=policy.get('config',{}).get('algorithm'))
        fallback['model_fingerprint']=policy.get('config',{}).get('model_fingerprint')
        if result:fallback['selection_attempt_id']=result.get('selection_attempt_id')
        fallback['_selection_hash']=digest(fallback)
        return fallback

    def preview(self,persona_id,text='',version_id=None,channel=None,members=None,mode=None):
        if channel is None and members is None and mode is None:
            return CharacterService(self.store).preview(persona_id,text,version_id)
        version,policy=self._scope(persona_id,version_id,channel or 'desktop')
        if mode is not None:policy=choose(mode)
        members=members if members is not None else [text]
        if (not isinstance(members,list) or not 1<=len(members)<=20 or
                any(not isinstance(t,str) or not t.strip() or len(t)>24000 for t in members)):
            raise RuntimeErrorBase('character_retrieval_invalid_input')
        with self.store.connection() as conn:
            row=conn.execute('SELECT p.reply_mode FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.persona_id=? AND h.channel=?',(persona_id,channel or 'desktop')).fetchone()
        contextual=bool(row and row[0]!='legacy_full')
        from .behavior import resolve_behavior
        scenario=[s['act'] for s in resolve_behavior('\n\n'.join(members))['signals']] if contextual else []
        result=self.selection(version,policy,members,scenario,4 if contextual else 2)
        with self.store.connection() as conn:
            if CharacterService(self.store)._version(conn,persona_id,version['id'])!=version:raise RuntimeErrorBase('character_source_changed')
        return result

    def close(self):
        self.closed=True;self.coordinator.close()


def audit(selection,parts,removed):
    """Allowlisted metadata only, reflecting the final serialized character block."""
    from .character_context import manifest
    final=manifest(selection,parts,removed)
    ids={'fact':final['fact_paths'],'lore':list(dict.fromkeys(final['lore_ids'])),'example':final['example_ids']}
    candidates={}
    for kind,rows in selection.get('candidates',{}).items():
        candidates[kind]=[{k:v for k,v in row.items() if k in ('item_id','rrf','lexical_strength','semantic_score','match_types','member_index','semantic_member_index')}
                          | {'member_indices':sorted({h['member_index'] for h in row.get('lexical_hits',[]) if h.get('member_index') is not None})}
                          for row in rows[:110]]
    groups=selection.get('dependency_groups') or [
        {'root':p['id'],'members':[r['id'] for r in json.loads(p['text'])['data']]} for p in selection['parts'] if p['kind']=='lore_group']
    result={k:selection.get(k) for k in ('requested_mode','effective_mode','policy_id','policy_revision','algorithm','policy_fingerprint','config_fingerprint',
          'model_fingerprint','index_fingerprint','preparation_ms','degrade_reason','selection_attempt_id','selector_returned','candidate_fingerprint')}
    result.update(version_id=selection['version_id'],candidates=candidates,selected=ids,
                  rejected=[{k:v for k,v in r.items() if k in ('kind','item_id','reason')} for r in selection.get('rejected',[])[:150]],
                  removed=final['removed'][:180],dependency_groups=[g for g in groups if set(g['members']).issubset(ids['lore'])],
                  budgets={name:sum(len(p['text'])+1 for p in parts if p['kind'] in kinds) for name,kinds in
                      {'anchor':('anchor',),'dynamic':('fact','lore_group'),'examples':('example',)}.items()})
    result['budgets']['total']=len(render_parts(parts))
    return result
