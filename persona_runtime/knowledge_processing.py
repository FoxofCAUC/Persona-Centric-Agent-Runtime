"""Opt-in dialogue knowledge processing on the existing managed execution chain."""
import copy
import json

from .archive import binding, fingerprint, read_exact, turn_scope
from .knowledge_processing_output import VERSION, messages, parse
from .knowledge_processing_material import bind, chunk, collect, groups
from .knowledge_service import POLICY, read, validate_used
from .knowledge_wait import check as active, mark, search_turn, refresh_turn
from .models import LOCAL_USER_ID, RuntimeErrorBase
from .private_personas import provider_identity
from .turn_input import load_turn_input,load_turn_sources


def model_contract(model):
    return {k:v for k,v in model.items() if k != 'encrypted_key'}


def put(db, key, data):
    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               ('knowledge.processing.'+key, json.dumps({'revision':0,'data':data},ensure_ascii=False)))


def record_used(db, paths, persona_id, channel, metadata):
    if not metadata: return
    row = read(db, 'processing.record.'+metadata['record_id'], {})['data']
    policy = read(db, 'processing.'+persona_id, {})
    if (not row or row['persona_id'] != persona_id or row['channel'] != channel
            or policy['data'].get('pipeline')!=VERSION or not policy['data'].get('enabled') or channel not in policy['data']['channels']
            or row['policy'] != policy or fingerprint(row['result']) != metadata['result_hash']):
        raise RuntimeErrorBase('knowledge_processing_changed')
    validate_used(db, paths, persona_id, channel, {'evidence':row['result']['evidence']})


class KnowledgeProcessing:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.service = engine.chat.knowledge
        self.service.processing = self

    def model(self,persona_id):
        with self.store.connection() as db:
            policy=read(db,'processing.'+persona_id,{})['data']
        return dict(self.engine.config.load()['model']) if policy.get('model_source')=='main' else self.engine.model()

    def defaults(self,model_source='organization'):
        model = dict(self.engine.config.load()['model']) if model_source=='main' else self.engine.model()
        return {'enabled':False, 'channels':['desktop','qq'], 'provider':provider_identity(model),
                'model_contract':fingerprint(model_contract(model)), 'pipeline':VERSION, 'prompt_profile':'question_focused_v6', **({'model_source':'main'} if model_source=='main' else {})}

    def policy(self, persona_id):
        self.service.current_policy(persona_id)
        with self.store.connection() as db:
            return read(db, 'processing.'+persona_id, self.defaults())

    def update_policy(self, persona_id, data, expected_revision, operation_id):
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store, persona_id)
        expected = self.defaults(data.get('model_source','organization')) if isinstance(data,dict) else self.defaults()
        if (not isinstance(data,dict) or data.get('model_source','organization') not in ('organization','main') or set(data) not in (set(expected),set(expected)-{'prompt_profile'}) or data.get('prompt_profile','legacy_v1') not in ('legacy_v1','question_focused_v2','question_focused_v3','question_focused_v4','question_focused_v5','question_focused_v6') or type(data['enabled']) is not bool
                or not isinstance(data['channels'],list) or not data['channels']
                or len(set(data['channels'])) != len(data['channels']) or not set(data['channels']) <= {'desktop','qq'}
                or any(data[k] != expected[k] for k in ('provider','model_contract','pipeline'))):
            raise RuntimeErrorBase('knowledge_processing_policy_invalid')
        self.policy(persona_id)
        return self.service._update('processing.'+persona_id,data,expected_revision,operation_id)

    def enabled(self, turn):
        policy = self.policy(turn['persona_id'])['data']
        return bool(policy['enabled'] and turn['channel'] in policy['channels'])

    def context(self, db, turn):
        selected = turn_scope(db,turn)
        focused=read(db,'processing.'+turn['persona_id'],self.defaults())['data'].get('prompt_profile') in ('question_focused_v2','question_focused_v3','question_focused_v4','question_focused_v5','question_focused_v6')
        states="('completed','failed','interrupted')" if focused else "('completed')"
        rows = db.execute("SELECT * FROM turns WHERE persona_id=? AND session_id=? AND rowid<(SELECT rowid FROM turns WHERE id=?) AND status IN "+states+" ORDER BY rowid DESC LIMIT 5",
                          (turn['persona_id'],turn['session_id'],turn['id'])).fetchall()
        recent, used = [], 0
        for row in rows:
            failed=row['status']!='completed'
            if failed and any(db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(ident,)).fetchone() for ident in load_turn_sources(db,dict(row))):continue
            found = read_exact(db,selected,{'kind':'turn','id':row['id']},audit=failed)
            if found['status'] != 'found': continue
            item = {'turn_id':row['id'], 'user':'\n'.join(m['text'] for m in found['members']),
                    'assistant':'' if failed else '\n'.join(m['text'] for m in found['replies']),
                    'binding':{'status':'input_only','turn_id':row['id'],'hash':fingerprint(found['members'])} if failed else binding(found)}
            if focused:item['request_state']=row['status']
            length = len(json.dumps(item,ensure_ascii=False))
            if used+length > 4000: break
            recent.append(item); used += length
        return {'current':load_turn_input(db,turn),'recent_context':list(reversed(recent))}

    def _call(self, turn, stage, inputs):
        active(self.service,turn)
        policy = self.policy(turn['persona_id'])
        with self.store.connection() as db:
            guard = fingerprint(self.context(db,turn))
        token = fingerprint([turn_scope_value(turn),policy,stage,inputs,VERSION])
        cache_key = 'processing.cache.'+token
        mark(self.service,turn,phase_for(stage))
        with self.store.connection(write=True) as db:
            old = read(db,cache_key,{})['data']
            if old:
                task = db.execute('SELECT * FROM organization_tasks WHERE id=?',(old['task_id'],)).fetchone()
                if task and task['status']=='completed':
                    # Cached work is revalidated against this caller's inputs.
                    self._grant(db,turn,policy,inputs)
                    return copy.deepcopy(read(db,'processing.result.'+task['id'],{})['data']),task['id']
            current = dict(db.execute('SELECT * FROM turns WHERE id=?',(turn['id'],)).fetchone())
            if current['status']!='queued' or current['cancelled_at']:
                raise RuntimeErrorBase('knowledge_processing_request_inactive')
            data={'stage':stage,'input':inputs,'pipeline':VERSION,'root_turn_id':turn['id'],
                  'context_guard':guard,'policy':policy,'cache_key':cache_key}
            payload={'persona_id':turn['persona_id'],'user_id':LOCAL_USER_ID,'session_id':turn['session_id'],
                     'channel':turn['channel'],'mode':'knowledge_processing','scope':turn_scope_value(turn),
                     'knowledge_processing':data}
            payload['fingerprint']=fingerprint(payload)
            if sum(len(m['content']) for m in messages(payload)) > self.model(turn['persona_id'])['max_input_chars']:
                raise RuntimeErrorBase('knowledge_processing_input_budget')
            request_id='kp-'+fingerprint([turn['id'],token])
            task=self.engine.managed.register(db,payload,request_id,automatic=1,trigger='incremental')
            key='wait.'+turn['id']
            value=read(db,'processing.'+key,{})['data']
            tasks=value.get('task_ids',[]) if value.get('execution_revision')==turn['execution_revision'] else []
            put(db,key,{'execution_revision':turn['execution_revision'],'task_ids':list(dict.fromkeys(tasks+[task['id']]))})
        if task['status'] not in ('queued','waiting_quota','running','completed'):
            with self.store.connection() as db:
                c=self.engine.managed.contract(db,task['id'])
            resumable=task.get('error_code') in ('knowledge_processing_request_inactive','knowledge_stopped','organization_lease_lost')
            if c and c['data']['phase']=='received' and task.get('error_code')=='knowledge_processing_output_invalid':
                with self.store.connection() as db:
                    saved=db.execute('SELECT t.payload_json,r.response_text FROM organization_tasks t JOIN organization_attempts a ON a.task_id=t.id JOIN organization_receipts r ON r.attempt_id=a.id WHERE t.id=? ORDER BY a.rowid DESC LIMIT 1',(task['id'],)).fetchone()
                try:
                    if saved:parse(json.loads(saved['payload_json']),saved['response_text']);resumable=True
                except RuntimeErrorBase:pass
            if c and c['data']['phase']=='received' and resumable:
                self.engine.managed.retry(task['id'])
        phase=None
        while True:
            active(self.service,turn)
            with self.store.connection() as db:
                row=db.execute('SELECT status,error_code FROM organization_tasks WHERE id=?',(task['id'],)).fetchone()
                if row['status']=='completed':
                    return copy.deepcopy(read(db,'processing.result.'+task['id'],{})['data']),task['id']
                if row['status'] not in ('queued','waiting_quota','running'):
                    raise RuntimeErrorBase(row['error_code'] if (row['error_code'] or '').startswith(('web_','wiki_','knowledge_route','knowledge_routing','jev_')) else 'knowledge_processing_'+row['status'])
                state='knowledge_waiting_quota' if row['status']=='waiting_quota' else phase_for(stage)
            if state!=phase:
                mark(self.service,turn,state);phase=state
            self.service.closed.wait(.1)

    def _model_from_policy(self,policy):
        return dict(self.engine.config.load()['model']) if policy.get('model_source')=='main' else self.engine.model()

    def _grant(self, db, turn, policy, inputs):
        current=read(db,'processing.'+turn['persona_id'],self.defaults())
        reader=read(db,'policy.'+turn['persona_id'],POLICY)['data']
        if (current!=policy or current['data']['pipeline']!=VERSION or not current['data']['enabled'] or turn['channel'] not in current['data']['channels']
                or not reader['enabled'] or turn['channel'] not in reader['channels']
                or current['data']['model_contract']!=fingerprint(model_contract(self._model_from_policy(current['data'])))):
            raise RuntimeErrorBase('knowledge_processing_policy_changed')
        if 'routing_policy' in inputs:self.engine.routing.grant(db,turn,inputs)
        if 'web_policy' in inputs:self.engine.web.check_inputs(db,turn,inputs)
        validate_used(db,self.service.paths,turn['persona_id'],turn['channel'],
                      {'evidence':[m['evidence'] for m in inputs.get('materials',[])]})
        for source in inputs.get('sources',[]):
            if 'evidence' in source:validate_used(db,self.service.paths,turn['persona_id'],turn['channel'],{'evidence':[source['evidence']]})
            elif 'web_policy' not in inputs:raise RuntimeErrorBase('knowledge_processing_source_invalid')

    def check(self, db, task, contract, payload):
        data=payload['knowledge_processing']
        turn=db.execute('SELECT * FROM turns WHERE id=?',(data['root_turn_id'],)).fetchone()
        if (not turn or turn['persona_id']!=task['persona_id'] or turn['status']!='queued' or turn['cancelled_at']
                or data['pipeline']!=VERSION or payload['scope']!=contract['scope']
                or turn_scope(db,dict(turn))!=payload['scope']
                or fingerprint(self.context(db,dict(turn)))!=data['context_guard']):
            raise RuntimeErrorBase('knowledge_processing_request_inactive')
        self._grant(db,dict(turn),data['policy'],data['input'])
        if data['stage'] in ('search','fetch','wiki_search','wiki_fetch','web_plan','web_fetch_plan'):
            self.engine.web.check(db,dict(turn),data)

    def published(self, db, task, payload, response, audit):
        if payload['knowledge_processing']['stage'] in ('route','route_retry'):
            output=self.engine.routing.accept(payload,response)
        elif payload['knowledge_processing']['stage'] in ('wiki_search','wiki_fetch'):
            from .knowledge_wikimedia import parse as parse_tool
            output=self.engine.web.accept(db,payload,parse_tool(payload['knowledge_processing']['stage'],response))
        elif payload['knowledge_processing']['stage'] in ('search','fetch'):
            from .knowledge_web_transport import parse as parse_tool
            output=self.engine.web.accept(db,payload,parse_tool(payload['knowledge_processing']['stage'],response))
        else:output=parse(payload,response)
        audit.setdefault('normalizations',[]).extend(output.pop('_normalizations',[]))
        put(db,'result.'+task['id'],output)
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   ('knowledge.'+payload['knowledge_processing']['cache_key'],json.dumps({'revision':0,'data':{'task_id':task['id']}})))

    def show(self, persona_id, turn_id):
        with self.store.connection() as db:
            row=db.execute('SELECT t.channel,m.manifest_json FROM turns t JOIN context_manifests m ON m.turn_id=t.id WHERE t.id=? AND t.persona_id=? ORDER BY m.rowid DESC LIMIT 1',(turn_id,persona_id)).fetchone()
            if not row:raise RuntimeErrorBase('knowledge_turn_not_found')
            metadata=json.loads(row['manifest_json']).get('knowledge',{}).get('processing')
            if not metadata:return {'state':'not_processed'}
            record_used(db,self.service.paths,persona_id,row['channel'],metadata)
            record=read(db,'processing.record.'+metadata['record_id'],{})['data']
            return {'state':'ready','processing':metadata,'knowledge':record['result'],'coverage':record['coverage']}

    def previous(self, turn, turn_id):
        with self.store.connection() as db:
            row=db.execute('SELECT manifest_json FROM context_manifests WHERE turn_id=? AND persona_id=? ORDER BY rowid DESC LIMIT 1',
                           (turn_id,turn['persona_id'])).fetchone()
            metadata=json.loads(row[0]).get('knowledge',{}).get('processing') if row else None
            if not metadata: return None
            try:record_used(db,self.service.paths,turn['persona_id'],turn['channel'],metadata)
            except RuntimeErrorBase:return {'state':'refresh_unavailable','reason':'referenced_evidence_changed','evidence':[]}
            record=read(db,'processing.record.'+metadata['record_id'],{})['data']
            return copy.deepcopy(record['result'])|{'processing':metadata}

    def prepare(self, turn, question, reuse_turn_id=None, *, path=None):
        frozen_policy=self.policy(turn['persona_id'])
        if reuse_turn_id:
            previous=self.previous(turn,reuse_turn_id)
            if previous is not None:return previous
        from .knowledge_service import should_retrieve
        if path is None and not should_retrieve(question): return {'state':'not_triggered','evidence':[]}
        with self.store.connection() as db:
            context=self.context(db,turn)
        goal,goal_task=self._call(turn,'goal',{'question':question,'recent_context':context['recent_context']})
        if not goal['queries']:raise RuntimeErrorBase('knowledge_route_goal_conflict')
        if path=='local' and goal['freshness']=='current':raise RuntimeErrorBase('knowledge_route_freshness_conflict')
        if path=='web' or path is None and self.engine.web.policy(turn['persona_id'])['data']['enabled']:
            materials=self.engine.web.gather(self,turn,question,goal)
            return self.translate_materials(turn,question,goal,goal_task,materials,frozen_policy)
        results=[search_turn(self.service,turn,query) for query in goal['queries']]
        combined={'state':'ready','evidence':[e for r in results for e in r.get('evidence',[])]}
        if self.service.maintenance:
            latest=goal['freshness']=='current' and path is None
            refresh=self.service.maintenance.on_result(turn,combined,latest)
            if latest:
                if not refresh.get('job_ids'):return {'state':'refresh_unavailable','reason':'current_evidence_not_ready','evidence':[]}
                refresh_turn(self.service,turn,refresh)
                from .knowledge_store import search as overlay,merge
                with self.store.connection() as db:
                    results=[merge({'state':'ready','evidence':[]},overlay(db,turn['persona_id'],turn['channel'],q,current=True)) for q in goal['queries']]
        materials=collect(self.service,turn,results)
        return self.translate_materials(turn,question,goal,goal_task,materials,frozen_policy)

    def translate_materials(self,turn,question,goal,goal_task,materials,frozen_policy):
        if not materials:return {'state':'ready','reason':'no_matches','evidence':[]}
        budget=self.model(turn['persona_id'])['max_input_chars']-5000
        if budget<3000:raise RuntimeErrorBase('knowledge_processing_input_budget')
        sources=[{'id':m['id'],'title':m['evidence']['article_title'],'section':m['evidence']['section_title'],
                  'lead':m['text'][:250],'characters':len(m['text']),'evidence':m['evidence']} for m in materials]
        selected=[];selection_tasks=[]
        for group in groups(sources,budget):
            choice,task=self._call(turn,'select',{'question':question,'goal':goal,'sources':group})
            selected.extend(choice['section_ids']);selection_tasks.append(task)
        selected=list(dict.fromkeys(selected))
        if len(selected)>6:
            choice,task=self._call(turn,'select',{'question':question,'goal':goal,'sources':[s for s in sources if s['id'] in selected]})
            selected=choice['section_ids'];selection_tasks.append(task)
        if frozen_policy['data'].get('prompt_profile') in ('question_focused_v5','question_focused_v6'):
            # First choose relevant sections as before, then select full small
            # windows within those sections. Every selected section's tail is
            # considered without translating all of its irrelevant material.
            windows=[piece for m in materials if m['id'] in selected for piece in chunk(m,500)]
            window_sources=[{'id':m['id'],'title':m['evidence']['article_title'],'section':m['evidence']['section_title'],
                'lead':m['text'],'characters':len(m['text']),'evidence':m['evidence']} for m in windows]
            chosen=[]
            for group in groups(window_sources,budget):
                choice,task=self._call(turn,'select',{'question':question,'goal':goal,'sources':group,'full_windows':True})
                chosen.extend(choice['section_ids']);selection_tasks.append(task)
            chosen=list(dict.fromkeys(chosen))
            while len(chosen)>6:
                narrowed=[]
                for group in groups([s for s in window_sources if s['id'] in chosen],budget):
                    choice,task=self._call(turn,'select',{'question':question,'goal':goal,'sources':group,'full_windows':True})
                    narrowed.extend(choice['section_ids']);selection_tasks.append(task)
                narrowed=list(dict.fromkeys(narrowed))
                if len(narrowed)>=len(chosen):raise RuntimeErrorBase('knowledge_processing_selection_not_progressing')
                chosen=narrowed
            fragments=[m for m in windows if m['id'] in chosen]
        else:
            fragments=[piece for m in materials if m['id'] in selected for piece in chunk(m,max(1000,budget-2500))]
        if not fragments:return {'state':'ready','reason':'no_relevant_sources','evidence':[]}
        units=[];translation_tasks=[]
        for group in groups(fragments,budget):
            output,task=self._call(turn,'translate',{'question':question,'goal':goal,'materials':group})
            units.extend(output['units']);translation_tasks.append(task)
        # Merge chunk results with their original references; never silently
        # truncate long sections or discard the tail of a selected section.
        def packets(points):
            result=[]
            for point in points:
                support=[]
                for material in fragments:
                    quotes=[r['quote'] for r in point['refs'] if r['material_id']==material['id']]
                    if quotes:support.append(dict(material,text='\n'.join(quotes)))
                result.append({'unit':point,'materials':support})
            return result
        def needs_merge(points):
            try:bind(points,fragments)
            except RuntimeErrorBase as error:
                if str(error)=='knowledge_processing_evidence_budget':return True
                raise
            return len(points)>6 or sum(len(u['text']) for u in points)>3000
        while needs_merge(units):
            merged=[]
            for group in groups(packets(units),budget-1500):
                support={}
                for packet in group:
                    for material in packet['materials']:
                        if material['id'] in support:support[material['id']]['text']+='\n'+material['text']
                        else:support[material['id']]=dict(material)
                output,task=self._call(turn,'compose',{'question':question,'units':[p['unit'] for p in group],'materials':list(support.values())})
                merged.extend(output['units']);translation_tasks.append(task)
            if len(merged)>=len(units) and needs_merge(merged):
                raise RuntimeErrorBase('knowledge_processing_merge_not_progressing')
            units=merged
        if not units:return {'state':'ready','reason':'no_supported_knowledge','evidence':[]}
        # Revalidate every final quote against the original contiguous chunk.
        parse({'knowledge_processing':{'stage':'translate','input':{'materials':fragments}}},
              json.dumps({'format_version':1,'units':units},ensure_ascii=False))
        review_packets=[]
        for point in units:
            support=[]
            for ref in point['refs']:
                fragment=next(m for m in fragments if m['id']==ref['material_id'])
                original=next(m for m in materials if m['evidence']['article_id']==fragment['evidence']['article_id']
                    and m['evidence'].get('section_id')==fragment['evidence'].get('section_id')
                    and m['evidence'].get('package_sha256')==fragment['evidence'].get('package_sha256'))
                start=fragment['evidence']['start']-original['evidence']['start']+fragment['text'].index(ref['quote'])
                context=original['text'][max(0,start-250):start+len(ref['quote'])+250]
                support.append(dict(fragment,text=context))
            review_packets.append({'unit':point,'materials':support})
        review_tasks=[]
        for group in groups(review_packets,budget-1500):
            # Repeated material IDs retain all quoted contexts, rather than
            # dropping a prior citation when two points cite the same section.
            support={}
            for packet in group:
                for material in packet['materials']:
                    if material['id'] in support:support[material['id']]['text']+='\n'+material['text']
                    else:support[material['id']]=dict(material)
            review,review_task=self._call(turn,'verify',{'question':question,'goal':goal,'units':[p['unit'] for p in group],'materials':list(support.values())})
            review_tasks.append(review_task)
            if not all(d['supported'] and d['temporal_ok'] and d['version_ok'] for d in review['decisions']):
                raise RuntimeErrorBase('knowledge_translation_review_rejected')
        points,evidence=bind(units,fragments)
        result={'state':'ready','evidence':evidence,'translation':points}
        record_id=fingerprint([turn_scope_value(turn),goal_task,selection_tasks,translation_tasks,review_tasks,result])
        metadata={'record_id':record_id,'result_hash':fingerprint(result),'pipeline':VERSION,
                  'goal_task_id':goal_task,'selection_task_ids':selection_tasks,
                  'translation_task_ids':translation_tasks,'review_task_id':review_tasks[-1],'review_task_ids':review_tasks,
                  'unit_hashes':[fingerprint(u) for u in points]}
        with self.store.connection(write=True) as db:
            self._grant(db,turn,frozen_policy,{'materials':fragments})
            put(db,'record.'+record_id,{'persona_id':turn['persona_id'],'channel':turn['channel'],
                'policy':frozen_policy,'result':result,
                'coverage':{'considered_section_ids':[m['id'] for m in materials],'selected_section_ids':selected,
                            'chunks':[{'id':m['id'],'start':m['evidence']['start'],'end':m['evidence']['end']} for m in fragments]}})
            key='last.'+fingerprint([turn['persona_id'],turn['channel'],turn['session_id']])
            last=read(db,key,{})
            db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('knowledge.'+key,json.dumps({'revision':last['revision']+1,'data':{'query':question,'state':'ready','turn_id':turn['id']}})))
        mark(self.service,turn,'knowledge_ready')
        return result|{'processing':metadata}


def turn_scope_value(turn):
    origin=json.loads(turn.get('origin_json') or '{}')
    return {'persona_id':turn['persona_id'],'user_id':LOCAL_USER_ID,'channel':turn['channel'],
            'bot':str(origin.get('bot','')) if turn['channel']=='qq' else '',
            'peer':str(origin.get('sender','')) if turn['channel']=='qq' else ''}


def phase_for(stage):
    return {'route':'knowledge_routing','route_retry':'knowledge_routing','goal':'knowledge_goal','select':'knowledge_selection','translate':'knowledge_translation',
        'compose':'knowledge_translation','verify':'knowledge_review','web_plan':'knowledge_web_plan',
        'web_fetch_plan':'knowledge_web_plan','search':'knowledge_search_web','fetch':'knowledge_fetch_web','wiki_search':'knowledge_search_web','wiki_fetch':'knowledge_fetch_web'}[stage]
