"""Explicit process-admitted evaluation variants; never selected by HTTP payloads."""
from copy import deepcopy
import json
from .archive import fingerprint,turn_scope
from .turn_input import load_turn_input
from .models import RuntimeErrorBase


class EvaluationSession:
    def __init__(self, admission, *, prompt_comparison=False):
        self.admission=admission;self.turns={};self.shared={};self.verified={}
        self.prompt_comparison=prompt_comparison
        self.peer_engines=None

    @classmethod
    def register_independent(cls,engines,turns,*,material_turns=None,prompt_comparison=False):
        """Three independent instances preserve ordinary single-active-session rules.

        Authorities are cloned from the same frozen artificial seed, not imported
        from a private identity. Every peer requires its own process admission.
        """
        if set(engines)!=set('ABC') or set(turns)!=set('ABC'):raise RuntimeErrorBase('evaluation_arms_invalid')
        if len({str(e.store.paths.home.resolve()) for e in engines.values()})!=3:raise RuntimeErrorBase('evaluation_instances_not_independent')
        sessions={};rows={};bindings=[]
        for arm,engine in engines.items():
            admission=getattr(engine,'assist_admission',None)
            if admission is None:raise RuntimeErrorBase('evaluation_not_admitted')
            session=cls(admission,prompt_comparison=prompt_comparison).attach(engine);sessions[arm]=session
            with engine.store.connection() as db:
                row=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns[arm],)).fetchone())
                if json.loads(row['context_policy_snapshot'])['disposition_mode']!=('assist' if arm=='C' else 'baseline'):
                    raise RuntimeErrorBase('evaluation_arm_mode_invalid')
                rows[arm]=row;bindings.append(session.binding(engine,db,row))
        if len(set(bindings))!=1:raise RuntimeErrorBase('evaluation_material_scope_mismatch')
        material_turns=turns if material_turns is None else material_turns
        if set(material_turns)!=set('ABC'):raise RuntimeErrorBase('evaluation_material_arms_invalid')
        source_rows={}
        for arm,engine in engines.items():
            with engine.store.connection() as db:
                source=db.execute('SELECT * FROM turns WHERE id=?',(material_turns[arm],)).fetchone()
                if not source or source['execution_revision']!=1 or sessions[arm].binding(engine,db,dict(source))!=bindings[0]:
                    raise RuntimeErrorBase('evaluation_material_scope_mismatch')
                source_rows[arm]=dict(source)
        group=fingerprint(material_turns);mapping={row['id']:{'arm':arm,'group':group,'binding':bindings[0],'b_turn_id':material_turns['B']} for arm,row in rows.items()}
        # A supplemental B request still cites the original paid B selection.
        # Its sources and complete input must match; it cannot select a new pool.
        mapping.setdefault(material_turns['B'],{'arm':'B','group':group,'binding':bindings[0],'b_turn_id':material_turns['B']})
        if prompt_comparison:
            for arm,row in rows.items():mapping[row['id']]['frozen_model_hash']=fingerprint(row['model_snapshot'])
            for arm,row in source_rows.items():
                if row['id'] in mapping:mapping[row['id']]['frozen_model_hash']=fingerprint(row['model_snapshot'])
        shared={};verified={}
        path=sessions['B'].material_path(engines['B'],group)
        if path.exists():
            saved=json.loads(path.read_text(encoding='utf-8'))
            if saved.get('binding')!=bindings[0] or saved.get('hash')!=fingerprint(saved.get('result')):raise RuntimeErrorBase('evaluation_saved_material_invalid')
            shared[group]=saved
        if material_turns!=turns and group not in shared:raise RuntimeErrorBase('evaluation_supplement_material_missing')
        for session in sessions.values():
            session.turns=mapping;session.shared=shared;session.verified=verified;session.peer_engines=engines
            session.supplemental_material_reuse=material_turns!=turns
        return sessions

    def verify_independent(self,turns,prepared):
        engines=self.peer_engines
        if engines is None:raise RuntimeErrorBase('evaluation_instances_not_independent')
        left=engines['B'].evaluation_session.normalized(engines['B'],turns['B'],prepared['B'])
        right=engines['C'].evaluation_session.normalized(engines['C'],turns['C'],prepared['C'])
        if self.prompt_comparison:
            # Explicit isolated v2 experiment: prompt and sampler are treatment.
            # Sources/current input remain bound by binding(); actual hashes for
            # both prompts are recorded rather than falsely claiming equality.
            with engines['B'].store.connection() as db:
                b=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns['B'],)).fetchone())
            with engines['C'].store.connection() as db:
                c=dict(db.execute('SELECT * FROM turns WHERE id=?',(turns['C'],)).fetchone())
            from .dialogue_voice import read
            if read(b) is not None or read(c) is None:raise RuntimeErrorBase('evaluation_voice_treatment_invalid')
            for key in ('profile_versions','memories','history_rows','references'):
                if prepared['B']['details'].get(key)!=prepared['C']['details'].get(key):
                    raise RuntimeErrorBase('evaluation_evidence_differs')
            for key in ('version_id','fact_paths','lore_ids','example_ids'):
                if (prepared['B']['details'].get('character') or {}).get(key)!=(prepared['C']['details'].get('character') or {}).get(key):
                    raise RuntimeErrorBase('evaluation_character_differs')
            proof={'B':left,'C':right}
        else:
            if left!=right:raise RuntimeErrorBase('evaluation_materials_differ')
            proof=left
        self.verified[self.turns[turns['B']]['group']]=proof
        return proof

    def attach(self,engine):
        engine.evaluation_session=self
        engine.selector.evaluation_session=self
        engine.selector.engine=engine
        return self

    def has_material(self,row):
        return row['group'] in self.shared

    def normalized(self,engine,turn_id,prepared):
        from .decision_capsule import capsule
        messages=deepcopy(prepared['messages'])
        decision=prepared['manifest'].get('_shadow_prepared')
        text=capsule(decision) if decision and decision.get('applied_to_prompt') else ''
        if text:
            if not messages[0]['content'].endswith(text):raise RuntimeErrorBase('evaluation_capsule_location')
            messages[0]['content']=messages[0]['content'][:-len(text)]
        with engine.store.connection() as db:
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone());self.get(engine,db,turn)
            members=load_turn_input(db,turn)
        # Normalize only the program-generated complete current-input block.
        # Batched input uses event_id (with JSON spacing); string replacement
        # inside arbitrary user prose could corrupt literal examples.
        from .turn_input import render_input
        original=render_input(members,bool(turn.get('input_batch_id')))
        normalized=render_input([dict(m,id='member-'+str(i)) for i,m in enumerate(members)],bool(turn.get('input_batch_id')))
        for message in messages:
            if message['role']=='user' and message['content']==original:message['content']=normalized
        return fingerprint(messages)

    def verify_pair(self,engine,b_turn,b_prepared,c_turn,c_prepared):
        b=self.turns[b_turn];c=self.turns[c_turn]
        if b['arm']!='B' or c['arm']!='C' or b['group']!=c['group']:raise RuntimeErrorBase('evaluation_pair_invalid')
        left=self.normalized(engine,b_turn,b_prepared);right=self.normalized(engine,c_turn,c_prepared)
        if left!=right:raise RuntimeErrorBase('evaluation_materials_differ')
        self.verified[b['group']]=left
        return left

    def before_dispatch(self,engine,turn,prepared):
        row=self.turns.get(turn['id'])
        if not row:return
        expected=self.verified.get(row['group'])
        if self.prompt_comparison and isinstance(expected,dict):expected=expected.get(row['arm'])
        if row['arm'] in ('B','C') and expected!=self.normalized(engine,turn['id'],prepared):
            raise RuntimeErrorBase('evaluation_pair_not_verified')

    def register(self,engine,turn_ids):
        if set(turn_ids)!=set('ABC') or len(set(turn_ids.values()))!=3:raise RuntimeErrorBase('evaluation_arms_invalid')
        with engine.store.connection() as db:
            rows={a:dict(db.execute('SELECT * FROM turns WHERE id=?',(t,)).fetchone()) for a,t in turn_ids.items()}
            keys=[self.binding(engine,db,t) for t in rows.values()]
            if len(set(keys))!=1:raise RuntimeErrorBase('evaluation_material_scope_mismatch')
            for arm,t in rows.items():
                mode=json.loads(t['context_policy_snapshot'])['disposition_mode']
                if mode!=('assist' if arm=='C' else 'baseline'):raise RuntimeErrorBase('evaluation_arm_mode_invalid')
                if t['id'] in self.turns:raise RuntimeErrorBase('evaluation_already_registered')
        key=fingerprint(turn_ids)
        for arm,t in rows.items():self.turns[t['id']]={'arm':arm,'group':key,'binding':keys[0],'b_turn_id':turn_ids['B']}
        path=self.material_path(engine,key)
        if path.exists():
            stored=json.loads(path.read_text(encoding='utf-8'))
            if stored.get('binding')!=keys[0] or stored.get('hash')!=fingerprint(stored.get('result')):
                raise RuntimeErrorBase('evaluation_saved_material_invalid')
            self.shared[key]=stored

    def material_path(self,engine,key):
        from .private_personas import safe
        return safe(engine.store.paths.home/'exports/acceptance/evaluation-materials'/(key+'.json'),engine.store.paths.home)

    def binding(self,engine,db,turn):
        self.admission.check(engine.store,engine.config,turn['persona_id'])
        from .character_runtime import validate_snapshot
        validate_snapshot(db,turn)
        from .references import prepare
        references,_=prepare(db,turn)
        policy=json.loads(turn['context_policy_snapshot'])
        model=turn['model_snapshot']
        if self.prompt_comparison:
            model=json.loads(model);model.pop('_dialogue_profile',None)
        return fingerprint({'scope':turn_scope(db,turn),'input':[m['text'] for m in load_turn_input(db,turn)],
            'profile':turn['profile_snapshot'],'character':turn['character_snapshot'],'model':model,
            'selector_model':turn['selector_model_snapshot'],'reply':turn['reply_mode_snapshot'],'expression':{k:v for k,v in json.loads(turn['expression_snapshot'] or '{}').items() if k!='policy_id'},
            'references':references,'context':{k:policy.get(k) for k in ('context_mode','cross_session','character_retrieval')}})

    def get(self,engine,db,turn):
        row=self.turns.get(turn['id'])
        if row and (row['binding']!=self.binding(engine,db,turn) or turn['execution_revision']!=1):
            raise RuntimeErrorBase('evaluation_material_invalidated')
        if row and self.prompt_comparison and row.get('frozen_model_hash')!=fingerprint(turn['model_snapshot']):
            raise RuntimeErrorBase('evaluation_prompt_treatment_changed')
        return row

    def capture(self,engine,turn,result):
        with engine.store.connection() as db:
            row=self.get(engine,db,turn)
            if not row or row['arm']!='B':return
            aid=result.get('selection_attempt_id')
            if aid:
                a=db.execute('SELECT * FROM character_selection_attempts WHERE id=? AND turn_id=?',(aid,turn['id'])).fetchone()
                m=db.execute('SELECT id FROM context_manifests WHERE selection_attempt_id=?',(aid,)).fetchone()
                valid_terminal=bool(a and (a['status']=='completed' or
                    (a['status'] in ('failed','interrupted') and result.get('effective_mode')=='lexical' and result.get('degrade_reason')==a['error_code'])))
                if not valid_terminal or a['purpose']!='conversation' or not m:raise RuntimeErrorBase('evaluation_selection_not_terminal')
            saved={'result':deepcopy(result),'hash':fingerprint(result),'selection_attempt_id':aid,'binding':row['binding']}
            path=self.material_path(engine,row['group'])
            path.parent.mkdir(parents=True,exist_ok=True)
            from .config import atomic_json
            atomic_json(path,saved)
            self.shared[row['group']]=saved

    def reuse(self,engine,db,turn):
        row=self.get(engine,db,turn)
        if not row or row['arm'] not in ('B','C'):return None
        material=self.shared.get(row['group'])
        if not material or material['hash']!=fingerprint(material['result']):raise RuntimeErrorBase('evaluation_material_missing')
        source=self.peer_engines['B'] if self.peer_engines else engine
        with source.store.connection() as authoritative:
            original=dict(authoritative.execute('SELECT * FROM turns WHERE id=?',(row['b_turn_id'],)).fetchone())
            source.evaluation_session.get(source,authoritative,original)
            if material['selection_attempt_id']:
                a=authoritative.execute('SELECT status,error_code FROM character_selection_attempts WHERE id=? AND turn_id=?',(material['selection_attempt_id'],original['id'])).fetchone()
                fallback=material['result'].get('effective_mode')=='lexical' and a and material['result'].get('degrade_reason')==a['error_code']
                if not a or not (a[0]=='completed' or a[0] in ('failed','interrupted') and fallback):raise RuntimeErrorBase('evaluation_selection_invalidated')
        return deepcopy(material['result'])


def variant(engine,db,turn):
    session=getattr(engine,'evaluation_session',None)
    return session.get(engine,db,turn) if session else None


def descriptor(engine,db,turn):
    row=variant(engine,db,turn)
    if not row:return None
    session=engine.evaluation_session;shared=session.shared.get(row['group'],{})
    return {**row,'version':'three-arm-v1','material_hash':shared.get('hash'),
        'prompt_comparison':session.prompt_comparison,
        'independent_instance':bool(session.peer_engines),
        'selection_runtime_id':session.peer_engines['B'].config.load()['instance_id'] if session.peer_engines else None,
        'evaluation_reuse':row['arm']=='C' or (row['arm']=='B' and row['b_turn_id']!=turn['id']),
        'supplemental_material_reuse':bool(getattr(session,'supplemental_material_reuse',False)),
        'selection_turn_id':row['b_turn_id'],'selection_attempt_id':shared.get('selection_attempt_id'),
        'removed':['optional_character','expression_style','situation_hint','disposition'] if row['arm']=='A' else [],
        'reserved_capsule_characters':600}
