"""Versioned local recall lifecycle, bounded candidate search and consumption ledger.

This package is callable on isolated instances. Production default activation and
continuous human review are package .3; no independent scheduler/model is created.
"""
from datetime import datetime,timezone
import json,time,sqlite3,re
from .archive import fingerprint
from .models import RuntimeErrorBase
from . import recall_policy as policy
from .recall_sources import material,normalize_scope,scan,unavailable_reason

def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
INDEX_VERSION='cjk-unigram-and-existing-terms-v1'

def tokens(text,*,query=False):
    from .chat import terms
    found=terms(text)
    if query:
        if re.fullmatch(r'[\u3400-\u9fff]',text.strip()):found.add(text.strip())
    else:found.update(re.findall(r'[\u3400-\u9fff]',text))
    return sorted(t for t in found if len(t)<=128)

class RecallSupport:
    def __init__(self,store,*,clock=time.time):self.store=store;self.clock=clock

    def _time(self,db,*,confirm_jump=False):
        row=db.execute('SELECT * FROM recall_support_clock WHERE singleton=1').fetchone()
        observed=policy.finite(self.clock())
        now,anomaly=policy.clock(row['effective_epoch'] if row else None,observed,confirm_jump=confirm_jump)
        db.execute('INSERT INTO recall_support_clock VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET effective_epoch=excluded.effective_epoch,observed_epoch=excluded.observed_epoch,anomaly=excluded.anomaly',(now,observed,anomaly))
        data=encoded(policy.snapshot())
        old=db.execute('SELECT data_json FROM recall_support_policies WHERE version=?',(policy.VERSION,)).fetchone()
        if old and old[0]!=data:raise RuntimeErrorBase('recall_policy_version_conflict')
        db.execute('INSERT OR IGNORE INTO recall_support_policies VALUES(?,?)',(policy.VERSION,data))
        return now,anomaly

    def confirm_time(self):
        with self.store.connection(write=True) as db:
            now,anomaly=self._time(db,confirm_jump=True)
            return {'effective_epoch':now,'anomaly':anomaly}

    def _register(self,db,kind,ref,data,now,*,rebuild_index=False):
        selected=data['scope'];key=fingerprint(selected)
        ident=fingerprint([policy.VERSION,key,kind,data['canonical_id'],ref['offset'],data['version']])
        existing=db.execute('SELECT metadata_json FROM recall_support_units WHERE id=?',(ident,)).fetchone()
        if existing:
            meta=json.loads(existing[0])
            if rebuild_index or meta.get('index_version')!=INDEX_VERSION:
                db.execute('DELETE FROM recall_support_terms WHERE unit_id=?',(ident,))
                db.executemany('INSERT INTO recall_support_terms VALUES(?,?)',[(t,ident) for t in tokens(data['text'])])
                meta['index_version']=INDEX_VERSION
                db.execute('UPDATE recall_support_units SET metadata_json=? WHERE id=?',(encoded(meta),ident))
            db.execute('INSERT OR IGNORE INTO recall_support_locations VALUES(?,?,?,?,?)',
                (ident,fingerprint(ref),encoded(ref),data['session_id'],now))
            return ident
        p=policy.INITIAL_PRIORITY
        db.execute('''INSERT INTO recall_support_units
            (id,persona_id,scope_key,scope_json,kind,source_id,source_version,fragment_offset,lineage,session_id,reference_json,
             registered_epoch,origin_epoch,policy_version,initial_priority,anchor_priority,anchor_epoch,active_until,indexed_text,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident,selected['persona_id'],key,encoded(selected),kind,data['canonical_id'],data['version'],ref['offset'],data['lineage'],
             data['session_id'],encoded(ref),now,data['origin_epoch'],policy.VERSION,p,p,now,policy.due(kind,p,now),data['text'],
             encoded({'source_type':data['source_type'],'offset':ref['offset'],'historical_usage':'unknown','index_version':INDEX_VERSION})))
        db.executemany('INSERT INTO recall_support_terms VALUES(?,?)',[(t,ident) for t in tokens(data['text'])])
        # Historical versions and their ledger stay, but superseded postings must
        # not consume the current candidate budget after an explicit resync.
        db.execute('''DELETE FROM recall_support_terms WHERE unit_id IN
            (SELECT id FROM recall_support_units WHERE scope_key=? AND kind=? AND lineage=? AND id!=?)''',
            (key,kind,data['lineage'],ident))
        db.execute('INSERT INTO recall_support_locations VALUES(?,?,?,?,?)',
            (ident,fingerprint(ref),encoded(ref),data['session_id'],now))
        return ident

    def register(self,kind,reference,scope):
        selected=normalize_scope(scope)
        with self.store.connection(write=True) as db:
            now,_=self._time(db);ref=dict(reference);ref.setdefault('offset',0)
            data=material(db,kind,ref,now=max(now,policy.finite(self.clock())))
            if not data or data.get('skip_fragment'):raise RuntimeErrorBase('recall_source_unavailable')
            if data['scope']!=selected:raise RuntimeErrorBase('recall_scope_denied')
            return self._register(db,kind,ref,data,now)

    def synchronize(self,pid,kind,*,limit=64,restart=False,rebuild_index=False):
        if type(limit) is not int or not 1<=limit<=256:raise RuntimeErrorBase('recall_sync_budget_invalid')
        if type(restart) is not bool or type(rebuild_index) is not bool:raise RuntimeErrorBase('recall_sync_options_invalid')
        with self.store.connection(write=True) as db:
            now,anomaly=self._time(db)
            row=db.execute('SELECT cursor FROM recall_support_sync WHERE persona_id=? AND kind=?',(pid,kind)).fetchone()
            cursor=[0,0] if restart or not row else json.loads(row[0])
            entries,next_cursor,complete=scan(db,pid,kind,cursor,limit,max(now,policy.finite(self.clock())))
            ids=[self._register(db,kind,ref,data,now,rebuild_index=rebuild_index) for ref,data in entries if not data.get('skip_fragment')]
            db.execute('INSERT INTO recall_support_sync VALUES(?,?,?,?) ON CONFLICT(persona_id,kind) DO UPDATE SET cursor=excluded.cursor,completed=excluded.completed',(pid,kind,encoded(next_cursor),int(complete)))
            return {'registered':len(ids),'ids':ids,'cursor':next_cursor,'completed':complete,'clock_anomaly':anomaly}

    @staticmethod
    def _live(db,row,scope,now,session_id=None):
        if row['scope_key']!=fingerprint(scope):return None,'permission_denied'
        observed=db.execute('SELECT observed_epoch FROM recall_support_clock WHERE singleton=1').fetchone()
        source_now=max(now,observed[0] if observed else now)
        sql='SELECT reference_json FROM recall_support_locations WHERE unit_id=?';args=[row['id']]
        if session_id is not None:sql+=' AND session_id=?';args.append(session_id)
        sql+=' ORDER BY observed_epoch DESC,location_id LIMIT 32'
        reason='source_unavailable'
        for (ref,) in db.execute(sql,args):
            data=material(db,row['kind'],json.loads(ref),now=source_now)
            if not data:reason=unavailable_reason(db,row['kind'],json.loads(ref),source_now);continue
            if data['scope']!=scope:reason='permission_denied';continue
            if data['version']!=row['source_version']:reason='source_version_changed';continue
            return data,None
        return None,reason

    def inspect(self,ident,scope):
        selected=normalize_scope(scope)
        with self.store.connection(write=True) as db:
            now,anomaly=self._time(db)
            row=db.execute('SELECT * FROM recall_support_units WHERE id=?',(ident,)).fetchone()
            if not row:return {'state':'not_found'}
            data,error=self._live(db,row,selected,now)
            if error:return {'state':error}
            p=policy.priority(row['kind'],row['anchor_priority'],row['anchor_epoch'],now)
            return {'state':'available','id':ident,'priority':p,'layer':'active' if row['active_until']>now else 'sleeping',
                'revision':row['revision'],'source_type':data['source_type'],'historical_usage':'unknown',
                'clock_anomaly':anomaly,'text':data['text']}

    def select(self,scope,query,*,root_id=None,session_id=None,kinds=policy.KINDS,limit=8,
               candidate_budget=64,deep=False,required_ids=()):
        selected=normalize_scope(scope);key=fingerprint(selected)
        if not isinstance(query,str) or len(query)>16000:raise RuntimeErrorBase('recall_query_invalid')
        if not kinds or not set(kinds)<=set(policy.KINDS):raise RuntimeErrorBase('recall_kind_invalid')
        if type(limit) is not int or not 1<=limit<=32 or type(candidate_budget) is not int or not limit<=candidate_budget<=256:
            raise RuntimeErrorBase('recall_candidate_budget_invalid')
        if type(deep) is not bool or len(required_ids)>min(32,candidate_budget):raise RuntimeErrorBase('recall_search_options_invalid')
        query_tokens=tokens(query,query=True);ts=query_tokens[:32]
        request=fingerprint([selected,query,root_id,session_id,kinds,limit,candidate_budget,deep,required_ids])
        with self.store.connection(write=True) as db:
            now,anomaly=self._time(db);items=[];excluded={};read=0;layers=[];seen=set()
            def accept(row,hits,required=False):
                nonlocal read
                if row['id'] in seen:return
                seen.add(row['id']);read+=1
                data,error=self._live(db,row,selected,now,session_id)
                if error:excluded[error]=excluded.get(error,0)+1;return
                p=policy.priority(row['kind'],row['anchor_priority'],row['anchor_epoch'],now)
                items.append({'id':row['id'],'kind':row['kind'],'source_id':row['source_id'],
                    'source_version':row['source_version'],'lineage':row['lineage'],'text':data['text'],
                    'source_type':data['source_type'],'relevance':hits,'priority':p,'required':required,
                    'layer':'active' if row['active_until']>now else 'sleeping','revision':row['revision']})
            for ident in required_ids:
                row=db.execute('SELECT * FROM recall_support_units WHERE id=?',(ident,)).fetchone()
                if row:accept(row,0,True)
            explicit=deep or any(token in query for token in ('以前','上次','还记得','那时候','之前','旧事','当时','前面'))
            def search(layer):
                nonlocal read
                layers.append(layer)
                if not ts:return
                marks=','.join('?' for _ in ts);km=','.join('?' for _ in kinds)
                sql=f'''SELECT u.*,count(DISTINCT t.term) AS hits FROM recall_support_terms t
                    JOIN recall_support_units u ON u.id=t.unit_id
                    WHERE t.term IN ({marks}) AND u.scope_key=? AND u.kind IN ({km})
                    AND u.active_until {'>' if layer=='active' else '<='} ?'''
                args=[*ts,key,*kinds,now]
                if session_id is not None:
                    sql+=' AND EXISTS(SELECT 1 FROM recall_support_locations l WHERE l.unit_id=u.id AND l.session_id=?)';args.append(session_id)
                sql+=' GROUP BY u.id ORDER BY hits DESC,u.anchor_priority DESC,u.id LIMIT ?'
                quota=candidate_budget-read
                if layer=='active':quota=min(quota,max(limit,candidate_budget//2))
                args.append(max(0,quota))
                deadline=time.monotonic()+0.25
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                try:
                    for row in db.execute(sql,args):accept(row,row['hits'])
                except sqlite3.OperationalError as error:
                    if 'interrupted' in str(error):raise RuntimeErrorBase('recall_query_budget_exceeded') from error
                    raise
                finally:db.set_progress_handler(None,0)
            search('active')
            enough=len({i['lineage'] for i in items})>=limit and max((i['relevance'] for i in items),default=0)>1
            reason='explicit_history' if explicit else 'active_evidence_insufficient' if not enough else None
            if reason and read<candidate_budget:search('sleeping')
            items.sort(key=lambda item:(item['required'],item['relevance'],item['priority'],item['id']),reverse=True)
            unique=[];lineages=set()
            for item in items:
                if item['lineage'] in lineages:continue
                lineages.add(item['lineage']);unique.append(item)
            chosen=unique[:max(limit,sum(i['required'] for i in unique))]
            cold='not_searched' if 'sleeping' not in layers else 'found' if any(i['layer']=='sleeping' for i in items) else (
                'permission_denied' if excluded.get('permission_denied') else 'source_withdrawn' if excluded.get('source_withdrawn') else
                'source_expired' if excluded.get('source_expired') else 'source_version_changed' if excluded.get('source_version_changed') else 'no_result')
            ident=fingerprint([request,now,[i['id'] for i in chosen],[i['revision'] for i in chosen]])
            audit={'id':ident,'root_id':root_id,'scope':selected,'policy_version':policy.VERSION,
                'selected':[dict(i, text_hash=fingerprint(i['text'])) for i in chosen],
                'searched_layers':layers,'deep_reason':reason,'cold_status':cold,'excluded':excluded,
                'candidate_rows_materialized':read,'candidate_budget':candidate_budget,'query_terms':len(ts),
                'query_terms_omitted':max(0,len(query_tokens)-len(ts)),'clock_anomaly':anomaly}
            # Store explanations without source prose; raw content remains canonical.
            saved=dict(audit,selected=[{k:v for k,v in i.items() if k!='text'} for i in audit['selected']])
            db.execute('INSERT OR IGNORE INTO recall_support_searches VALUES(?,?,?,?,?,?,?)',(ident,root_id,key,request,fingerprint(query),now,encoded(saved)))
            return audit

    def record(self,unit_id,root_id,scope,*,kind,status,occurred_at,evidence):
        """Trusted adapter entry. Evidence must resolve a real scoped root interaction."""
        selected=normalize_scope(scope)
        if kind not in policy.ALPHAS or status not in ('completed','failed','cancelled','unknown'):
            raise RuntimeErrorBase('recall_event_invalid')
        when=policy.finite(occurred_at)
        with self.store.connection(write=True) as db:
            now,anomaly=self._time(db)
            if when>now:raise RuntimeErrorBase('recall_event_future')
            root=db.execute('SELECT * FROM turns WHERE id=?',(root_id,)).fetchone()
            from .archive import turn_scope
            if not root or turn_scope(db,dict(root))!=selected:raise RuntimeErrorBase('recall_root_scope_invalid')
            from .recall_sources import epoch
            if when!=epoch(root['created_at']):raise RuntimeErrorBase('recall_root_time_invalid')
            unit=db.execute('SELECT * FROM recall_support_units WHERE id=?',(unit_id,)).fetchone()
            if not unit:raise RuntimeErrorBase('recall_unit_missing')
            data,error=self._live(db,unit,selected,now)
            if error:raise RuntimeErrorBase('recall_'+error)
            if kind=='user_mention':
                from .turn_input import input_text
                quote=evidence.get('mention_quote','')
                if evidence.get('resolved_unit_id')!=unit_id or evidence.get('user_event_id')!=root['user_event_id']:
                    raise RuntimeErrorBase('recall_mention_evidence_invalid')
                if not isinstance(quote,str) or len(quote.strip())<2 or quote not in input_text(db,dict(root)) or quote not in data['text']:
                    raise RuntimeErrorBase('recall_mention_quote_invalid')
            elif status=='completed':
                receipt=db.execute('SELECT * FROM attempts WHERE id=? AND turn_id=?',(evidence.get('attempt_id'),root_id)).fetchone()
                use=db.execute('SELECT details_json FROM context_uses WHERE attempt_id=?',(evidence.get('attempt_id'),)).fetchone()
                if not receipt or receipt['status']!='completed' or root['status']!='completed' or not use:
                    raise RuntimeErrorBase('recall_consumption_unconfirmed')
                metadata=json.loads(use[0]).get('recall_support',{})
                if unit_id not in metadata.get('selected_ids',[]):raise RuntimeErrorBase('recall_consumption_not_adopted')
                selection=db.execute('SELECT * FROM recall_support_searches WHERE id=?',(metadata.get('selection_id'),)).fetchone()
                if not selection or selection['root_id']!=root_id or selection['scope_key']!=fingerprint(selected):
                    raise RuntimeErrorBase('recall_selection_identity_invalid')
                adopted=json.loads(selection['result_json'])['selected']
                if not any(i['id']==unit_id and i['source_version']==unit['source_version'] for i in adopted):
                    raise RuntimeErrorBase('recall_selection_identity_invalid')
                if kind=='supported_recall':
                    quote=evidence.get('source_quote','')
                    reply=db.execute('SELECT text FROM events WHERE id=?',(root['reply_event_id'],)).fetchone()
                    if not isinstance(quote,str) or len(quote.strip())<2 or quote not in data['text'] or not reply or quote not in reply[0]:
                        raise RuntimeErrorBase('recall_output_evidence_required')
            alpha=policy.ALPHAS[kind] if status=='completed' else 0.0
            old=db.execute('SELECT * FROM recall_support_events WHERE unit_id=? AND root_id=?',(unit_id,root_id)).fetchone()
            if old and old['alpha']>=alpha:return {'applied':False,'event_id':old['id'],'reason':'same_root_not_stronger'}
            if old and old['effective_epoch']!=max(when,unit['registered_epoch']):
                raise RuntimeErrorBase('recall_event_identity_conflict')
            event_id=fingerprint([unit_id,root_id])
            revision=old['revision']+1 if old else 1
            db.execute('''INSERT INTO recall_support_events VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(unit_id,root_id) DO UPDATE SET kind=excluded.kind,status=excluded.status,
                alpha=excluded.alpha,revision=excluded.revision,evidence_json=excluded.evidence_json''',
                (event_id,unit_id,root_id,kind,status,max(when,unit['registered_epoch']),alpha,revision,encoded(evidence)))
            events=db.execute('SELECT * FROM recall_support_events WHERE unit_id=? AND alpha>0 ORDER BY effective_epoch,root_id LIMIT ?',
                (unit_id,policy.MAX_REPLAY_EVENTS+1)).fetchall()
            if len(events)>policy.MAX_REPLAY_EVENTS:raise RuntimeErrorBase('recall_replay_budget_exceeded')
            p=unit['initial_priority'];anchor=unit['registered_epoch']
            for event in events:p,anchor=policy.reinforce(unit['kind'],p,anchor,event['effective_epoch'],event['alpha'])
            if alpha:
                db.execute('UPDATE recall_support_units SET anchor_priority=?,anchor_epoch=?,active_until=?,revision=revision+1 WHERE id=?',
                    (p,anchor,policy.due(unit['kind'],p,anchor),unit_id))
            return {'applied':bool(alpha),'event_id':event_id,'priority':policy.priority(unit['kind'],p,anchor,now),
                'clock_anomaly':anomaly,'contribution':alpha}
