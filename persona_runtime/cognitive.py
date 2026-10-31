"""Prepare outside write transactions; audit exactly the submitted input."""
import json
import sqlite3
import time

from .archive import binding,encoded,fingerprint,read_exact,scope,turn_scope
from .context_builders import (BUILDER_VERSION,TASK_TYPES,DialogueBuilder,ReflectionBuilder,
                               AffectBuilder,DirectorBuilder,PlanningBuilder,policy_for,pool)
from .archive_query import Budget,SearchFailure
from .models import LOCAL_USER_ID,RuntimeErrorBase,identifier,timestamp
from .profile import head,active_state,snapshot,source_blocked
from .turn_input import load_turn_input


class CognitiveService:
    def __init__(self,store,engine):
        from .disposition_shadow import ShadowService
        self.shadow=ShadowService(store,engine)
        self.store=store;self.engine=engine;self.dialogue=DialogueBuilder(store,engine)
        from .character_runtime import CharacterRuntime
        self.characters=CharacterRuntime(store)
        store.character_committed=self.characters.notify

    def _guard(self,conn,turn,details,*,expected=False):
        if turn['channel']=='test':return fingerprint({'model':turn['model_snapshot']})
        current=dict(conn.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
        selected=turn_scope(conn,current);history=[]
        for item in details.get('history_groups',[]):
            found=read_exact(conn,selected,{'kind':'turn','id':item['turn_id']})
            history.append(binding(found))
        memories=[]
        for item in details.get('memories',[]):
            row=conn.execute('SELECT * FROM memories WHERE id=?',(item['id'],)).fetchone()
            sources=[r[0] for r in conn.execute('SELECT event_id FROM memory_sources WHERE memory_id=?',(item['id'],))]
            from .context_builders import memory_usable
            memories.append({'row':dict(row) if row else None,'sources':sources,'usable':memory_usable(conn,current,dict(row)) if row else False})
        summary=conn.execute('SELECT * FROM summaries WHERE id=?',(details.get('summary_id'),)).fetchone()
        from .context_builders import summary_usable
        summary_valid=summary_usable(conn,current,summary) if summary else False
        state=head(conn,current['persona_id'],'state')
        frozen=json.loads(current['profile_snapshot']) if current.get('profile_snapshot') else snapshot(conn,current['persona_id'])
        from .references import prepare
        block,references=prepare(conn,current)
        try:
            _,keys=pool(conn,current,Budget(),keys_only=True)
            window=keys['turn_ids']
        except (RuntimeErrorBase,SearchFailure):window=[]
        # Hashes are internal transient guards. Full rows never enter the
        # persisted manifest or ordinary logs.
        settings=turn if expected else current
        return fingerprint({'turn_settings':{k:settings.get(k) for k in ('profile_snapshot','character_snapshot','context_policy_snapshot','model_snapshot','reply_mode_snapshot','expression_snapshot','origin_json')},
            'inputs':load_turn_input(conn,current),'history':history,'memories':memories,
            'summary':dict(summary) if summary else None,'summary_valid':summary_valid,
            'state':details.get('state_version') if expected else state['id'] if active_state(state) else None,
            'relation_blocked':source_blocked(conn,frozen['relationship']['source_ids']),
            'references_hash':fingerprint(block),'window':window})

    def _manifest(self,conn,turn,messages,details):
        from .reply_protocol import descriptor
        states=[{'kind':k,**v} for k,v in details.get('profile_versions',{}).items()]
        if turn.get('expression_snapshot'):
            states.append({'kind':'expression_policy','snapshot_hash':fingerprint(turn['expression_snapshot']),
                           'policy':json.loads(turn['expression_snapshot'])})
        character=details.get('character') or {}
        if character:states.append({'kind':'character','id':character['version_id'],'revision':character['revision'],'origin':'creator_setting'})
        if details.get('state_version'):states.append({'kind':'manual_state','id':details['state_version']})
        if details.get('summary_id'):states.append({'kind':'lossy_summary','id':details['summary_id'],'revision':details['summary_revision']})
        states.extend({'kind':'memory','id':m['id'],'revision':m['revision']} for m in details.get('memories',[]))
        evidence=[{'kind':'current_input','id':m['id'],'hash':fingerprint(m['text'])} for m in load_turn_input(conn,turn)] if turn['channel']!='test' else []
        selected=turn_scope(conn,turn) if turn['channel']!='test' else None
        for group in details.get('history_groups',[]):
            item=read_exact(conn,selected,{'kind':'turn','id':group['turn_id']});bound=binding(item)
            evidence.append({'kind':group['kind'],'turn_id':group['turn_id'],'hash':bound.get('hash'),
                             'member_ids':bound.get('member_ids',[]),'reply_ids':bound.get('reply_ids',[])})
        for row in conn.execute('SELECT r.event_id,r.binding_json FROM input_references r JOIN input_batch_members m ON m.event_id=r.event_id WHERE m.batch_id=? ORDER BY m.member_order',(turn.get('input_batch_id'),)):
            bound=json.loads(row['binding_json'])
            evidence.append({'kind':'accepted_reference','input_event_id':row['event_id'],'target':bound.get('target'),
                             'turn_id':bound.get('turn_id'),'hash':bound.get('hash')})
        retrieval=details.get('retrieval') or {};missing=[]
        if retrieval.get('degraded_reason'):missing.append(retrieval['degraded_reason'])
        if retrieval.get('mode')=='working_set' and not retrieval.get('matched_targets'):missing.append('retrieval_no_matches')
        if not details.get('state_version'):missing.append('manual_state_unset_or_expired')
        return {'context_id':identifier(),'task_type':'dialogue','purpose':'provider_probe' if turn['channel']=='test' else 'conversation',
            'scope':selected,'turn_id':turn['id'],'builder_version':BUILDER_VERSION,'created_at':timestamp(),
            'input_snapshot_hash':fingerprint(messages),'model_snapshot_hash':fingerprint({k:v for k,v in json.loads(turn['model_snapshot']).items() if k!='encrypted_key'}),
            'included_state_refs':states,'excluded_state_refs':details.get('removed',[]),
            'evidence_refs':evidence,'hard_constraints':['identity_scope','core_boundary','complete_current_input','mandatory_character_facts','accepted_reference_binding'],
            'soft_tendencies':{'behavior':(details.get('behavior') or {}).get('signals',[]),'manual_state':bool(details.get('state_version'))},
            'truncation_info':details.get('removed',[])+character.get('removed',[]),'missing_data':missing,
            'retrieval':retrieval,'character_retrieval':character.get('retrieval'),**({'decision_capsule':details['decision_capsule']} if 'decision_capsule' in details else {}),'partitions':{'hard':['core','current_input','required_character_facts','explicit_references'],
                'soft_priority':['recent_turns','relevant_memories','retrieved_turns','lossy_summary','optional_character']},
            'input_characters':sum(len(m['content']) for m in messages),
            'reply_output_contract':descriptor(turn.get('reply_mode_snapshot') or 'legacy_full',turn.get('expression_snapshot')),'persisted':False}

    def prepare(self,turn_id,character_selection=None):
        started=time.perf_counter()
        with self.store.connection() as conn:turn=dict(conn.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone())
        try:character=self.characters.prepare(turn,character_selection)
        except RuntimeErrorBase as exc:
            from .chat import ModelError
            raise ModelError(str(exc)) from None
        decision=self.shadow.prepare(turn)
        from .decision_capsule import capsule
        evidence=self.dialogue.evidence(turn)
        with self.store.connection() as conn:
            if evidence is not None:
                selected=turn_scope(conn,turn)
                if any(binding(read_exact(conn,selected,{'kind':'turn','id':i['group']['turn_id']}))!=binding(i['group']) for i in evidence['items']):
                    evidence=self.dialogue.basic_evidence(conn,turn)
            messages,details=self.dialogue.render(conn,turn,evidence,character,capsule=capsule(decision))
            decision=self.bind_decision(decision,details)
            guard=self._guard(conn,turn,details,expected=True)
            manifest=self._manifest(conn,turn,messages,details)
        manifest['_shadow_prepared']=decision
        manifest['preparation_ms']=round((time.perf_counter()-started)*1000,3)
        return {'messages':messages,'details':details,'guard':guard,'manifest':manifest,'character_prepared':character,'accepted_policy_hash':fingerprint(turn.get('context_policy_snapshot'))}

    @staticmethod
    def bind_decision(decision,details):
        if decision is None or decision.get('mode')!='assist':return decision
        from .decision_capsule import capsule,metadata
        budget=details.get('capsule_budget') or {}
        text=capsule(decision) if budget.get('applied') else ''
        meta=metadata(decision,text,budget.get('removed',[]),budget.get('reason'))
        details['decision_capsule']=meta
        return decision|{'applied_to_prompt':bool(text),'capsule':meta}

    def finalize(self,conn,turn,prepared):
        started=time.perf_counter()
        from .references import prepare
        if (prepared['manifest'].get('_shadow_prepared') or {}).get('mode')=='assist' and prepared.get('accepted_policy_hash')!=fingerprint(turn.get('context_policy_snapshot')):
            from .chat import ModelError
            raise ModelError('assist_snapshot_invalid')
        if turn['channel']!='test':prepare(conn,turn)
        try:character=self.characters.recheck(conn,turn,prepared.get('character_prepared'))
        except RuntimeErrorBase as exc:
            from .chat import ModelError
            raise ModelError(str(exc)) from None
        decision=self.shadow.recheck(conn,turn,prepared['manifest'].get('_shadow_prepared'))
        from .decision_capsule import capsule
        if decision!=prepared['manifest'].get('_shadow_prepared') or character!=prepared.get('character_prepared') or prepared['guard']!=self._guard(conn,turn,prepared['details']):
            evidence=self.dialogue.basic_evidence(conn,turn)
            original=(prepared['details'].get('retrieval') or {}).get('degraded_reason')
            if original:evidence['retrieval']['degraded_reason']=original
            messages,details=self.dialogue.render(conn,turn,evidence,character,capsule=capsule(decision))
            decision=self.bind_decision(decision,details)
            manifest=self._manifest(conn,turn,messages,details)
            manifest['preparation_ms']=prepared['manifest'].get('preparation_ms',0)
            manifest['_shadow_prepared']=decision
            prepared={**prepared,'messages':messages,'details':details,'manifest':manifest,'character_prepared':character}
        prepared['manifest']['final_verification_ms']=round((time.perf_counter()-started)*1000,3)
        prepared['manifest']['_shadow_prepared']=self.shadow.recheck(conn,turn,prepared['manifest'].get('_shadow_prepared'))
        return prepared

    @staticmethod
    def persist(conn,manifest,*,turn=None,task=None,attempt_id=None):
        manifest={**manifest,'persisted':True}
        if turn is not None:
            from .disposition_shadow import persist_shadow
            persist_shadow(conn,turn,manifest,attempt_id)
        else:manifest.pop('_shadow_prepared',None)
        if len(encoded(manifest).encode('utf-8'))>128*1024:raise RuntimeErrorBase('context_manifest_too_large')
        try:
            conn.execute('INSERT INTO context_manifests (id,persona_id,user_id,task_type,turn_id,organization_task_id,attempt_id,organization_attempt_id,manifest_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (manifest['context_id'],turn['persona_id'] if turn else task['persona_id'],LOCAL_USER_ID,
                          manifest['task_type'],turn['id'] if turn else None,task['id'] if task else None,
                          attempt_id if turn else None,attempt_id if task else None,encoded(manifest),timestamp()))
        except (sqlite3.Error,OSError):
            from .chat import ModelError
            raise ModelError('context_manifest_storage_failed') from None
        return manifest['context_id']

    def blocked(self,conn,turn,error):
        manifest={'context_id':identifier(),'task_type':'dialogue','purpose':'provider_probe' if turn['channel']=='test' else 'conversation',
                  'turn_id':turn['id'],'builder_version':BUILDER_VERSION,'input_snapshot_hash':None,
                  'missing_data':[error],'status':'blocked_before_call','created_at':timestamp()}
        return self.persist(conn,manifest,turn=turn)

    @staticmethod
    def reflection(conn,task,payload,messages,attempt_id):
        manifest=ReflectionBuilder.build(task,payload,messages)|{'context_id':identifier(),'task_type':'reflection',
            'organization_task_id':task['id'],'created_at':timestamp(),'input_characters':sum(len(m['content']) for m in messages),
            'excluded_state_refs':[],'truncation_info':[],'soft_tendencies':[]}
        return CognitiveService.persist(conn,manifest,task=task,attempt_id=attempt_id)

    @staticmethod
    def _owner(conn,persona_id,channel,bot,peer,turn_id=None,task_id=None):
        if bool(turn_id)==bool(task_id):raise RuntimeErrorBase('invalid_context_target')
        selected=scope(persona_id,channel,bot,peer)
        if not conn.execute('SELECT 1 FROM personas WHERE id=?',(persona_id,)).fetchone():raise RuntimeErrorBase('context_unavailable')
        if turn_id:
            row=conn.execute('SELECT * FROM turns WHERE id=?',(turn_id,)).fetchone()
            project_probe=bool(row and row['channel']=='test' and row['persona_id'] is None and channel=='desktop')
            if not row or not project_probe and (row['persona_id']!=persona_id or turn_scope(conn,dict(row))!=selected):raise RuntimeErrorBase('context_unavailable')
        else:
            row=conn.execute('SELECT * FROM organization_tasks WHERE id=? AND persona_id=? AND user_id=?',(task_id,persona_id,LOCAL_USER_ID)).fetchone()
            if not row or row['channel']!=channel and not (row['mode']=='test' and channel=='desktop'):raise RuntimeErrorBase('context_unavailable')
            if channel=='qq':
                events=json.loads(row['payload_json']).get('events',[])
                if not events or any(read_exact(conn,selected,{'kind':e.get('kind','event'),'id':e['id']})['status']!='found' for e in events):raise RuntimeErrorBase('context_unavailable')
        return dict(row)

    def preview(self,persona_id,channel,task_type='dialogue',turn_id=None,task_id=None,bot='',peer=''):
        if task_type not in TASK_TYPES:raise RuntimeErrorBase('invalid_context_task_type')
        scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            self.engine.service._persona(conn,persona_id)
            if task_type in ('dialogue','reflection'):
                owner=self._owner(conn,persona_id,channel,bot,peer,turn_id,task_id)
            elif turn_id or task_id:raise RuntimeErrorBase('invalid_context_target')
        if task_type=='dialogue':
            if not turn_id:raise RuntimeErrorBase('invalid_context_target')
            prepared=self.prepare(turn_id)
            with self.store.connection() as conn:prepared=self.finalize(conn,owner,prepared)
            return {'status':'ok','manifest':prepared['manifest'],'creates_request':False}
        if task_type=='reflection':
            if not task_id:raise RuntimeErrorBase('invalid_context_target')
            from .organization import OrganizationEngine
            payload=json.loads(owner['payload_json']);messages=OrganizationEngine.messages(payload)
            with self.store.connection() as conn:
                from .session_organization import valid as source_valid
                valid=owner['mode']=='test' or (payload.get('guard')==OrganizationEngine.guard(conn,persona_id,owner['session_id']) and source_valid(conn,payload))
            manifest=ReflectionBuilder.build(owner,payload,messages)|{'task_type':task_type,'persisted':False}
            if not valid:manifest['missing_data'].append('organization_source_changed')
            return {'status':'ok' if valid else 'not_ready','manifest':manifest,'creates_request':False}
        builder={'affect':AffectBuilder,'director':DirectorBuilder,'planning':PlanningBuilder}[task_type]
        with self.store.connection() as conn:versions={k:{'id':v['id'],'revision':v['revision']} for k,v in snapshot(conn,persona_id).items()}
        return {'status':'not_ready','manifest':builder.build()|{'task_type':task_type,'builder_version':BUILDER_VERSION,
                    'included_state_refs':versions,'persisted':False},'creates_request':False}

    def inspect(self,persona_id,channel,turn_id=None,task_id=None,context_id=None,bot='',peer=''):
        with self.store.connection() as conn:
            owner=self._owner(conn,persona_id,channel,bot,peer,turn_id,task_id)
            sql='SELECT manifest_json FROM context_manifests WHERE persona_id IS ? AND '+('turn_id=?' if turn_id else 'organization_task_id=?')
            args=[owner['persona_id'],owner['id']]
            if context_id:sql+=' AND id=?';args.append(context_id)
            rows=conn.execute(sql+' ORDER BY rowid DESC LIMIT 50',args).fetchall()
            return {'status':'ok','manifests':[json.loads(r[0]) for r in rows],'source_text_requires_current_authorization':True}
