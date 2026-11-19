"""Canonical source adapters for recall. Every selected unit is revalidated."""
from datetime import datetime,timezone
import json
from .archive import fingerprint,scope as make_scope,turn_scope,find_turn,read_exact
from .models import LOCAL_USER_ID,RuntimeErrorBase

CHUNK_CHARS=4096

def epoch(value):
    stamp=datetime.fromisoformat(value)
    if stamp.tzinfo is None:raise RuntimeErrorBase('recall_source_time_unavailable')
    return stamp.timestamp()

def normalize_scope(value):
    expected=make_scope(value['persona_id'],value['channel'],value.get('bot',''),value.get('peer',''))
    if value!=expected:raise RuntimeErrorBase('recall_scope_invalid')
    return expected

def _scope_turn(selected,session=None):
    return {'persona_id':selected['persona_id'],'channel':selected['channel'],'input_batch_id':None,
        'origin_json':json.dumps({'bot':selected['bot'],'sender':selected['peer']}),'session_id':session}

def unavailable_reason(db,kind,ref,now):
    """Explain denied/withdrawn/expired separately from an empty cold search."""
    if kind=='memory':
        row=db.execute('SELECT * FROM memories WHERE id=?',(ref['id'],)).fetchone()
        if not row:return 'source_unavailable'
        if row['status']=='retracted':return 'source_withdrawn'
        if row['status']=='superseded':return 'source_version_changed'
        fact=db.execute('SELECT * FROM memory_facts WHERE memory_id=?',(ref['id'],)).fetchone()
        if fact:
            if json.loads(fact['data_json']).get('import_review_required'):return 'review_required'
            if fact['valid_until'] and epoch(fact['valid_until'])<=now:return 'source_expired'
            if fact['valid_from'] and epoch(fact['valid_from'])>now:return 'source_not_yet_valid'
        ids=[row['source_event_id']]+[r[0] for r in db.execute('SELECT event_id FROM memory_sources WHERE memory_id=?',(ref['id'],))]
    elif kind=='dialogue':ids=[ref['id']]
    else:
        row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(ref['key'],)).fetchone()
        if not row:return 'source_unavailable'
        record=json.loads(row[0])['data']
        from .knowledge_native_memory import source_states,state_for_source
        source=next((s for s in record.get('sources',[]) if s['id']==ref['id']),None)
        if source and state_for_source(source_states(db,record['persona_id'])['data'],source).get('state')=='withdrawn':
            return 'source_withdrawn'
        turn=db.execute('SELECT user_event_id FROM turns WHERE id=?',(record.get('root_turn_id'),)).fetchone()
        ids=[turn[0]] if turn else []
    from .turn_input import load_turn_sources
    expanded=set(ids)
    for ident in ids:
        turn=find_turn(db,'event',ident)
        if turn:expanded.update(load_turn_sources(db,turn))
    if any(db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(ident,)).fetchone() for ident in expanded):
        return 'source_withdrawn'
    return 'source_unavailable'

def material(db,kind,ref,*,now):
    """Return a live source, including scope; no index text is authoritative."""
    if kind=='memory':
        row=db.execute('SELECT * FROM memories WHERE id=?',(ref['id'],)).fetchone()
        if not row or row['status']!='active':return None
        memory=dict(row)
        event=db.execute('SELECT * FROM events WHERE id=?',(memory['source_event_id'],)).fetchone()
        if not event:return None
        original=find_turn(db,'event',event['id'])
        selected=turn_scope(db,original) if original else make_scope(memory['persona_id'],'desktop')
        from .context_builders import memory_usable
        from .automatic_memory import usable_fact
        if not memory_usable(db,_scope_turn(selected,event['session_id']),memory,for_review=True):return None
        if not usable_fact(db,memory,datetime.fromtimestamp(now,timezone.utc)):return None
        refs=[dict(r) for r in db.execute('SELECT event_id,quote FROM memory_sources WHERE memory_id=? ORDER BY event_id',(memory['id'],))]
        data={'text':memory['text'],'scope':selected,'session_id':event['session_id'],
            'origin_epoch':epoch(memory['created_at']),'version':fingerprint([memory,refs]),
            'lineage':memory['root_id'],'source_type':'user_memory','canonical_id':memory['id']}
    elif kind=='dialogue':
        event=db.execute('SELECT * FROM events WHERE id=?',(ref['id'],)).fetchone()
        if not event or event['role'] not in ('user','persona'):return None
        turn=find_turn(db,'event',event['id'])
        if turn:
            if turn['persona_id']!=event['persona_id']:return None
            selected=turn_scope(db,turn)
            projection=read_exact(db,selected,{'kind':'event','id':event['id']})
            if projection['status']!='found':return None
            if event['role']=='persona' and turn['channel']=='qq':return None  # semantic output is not a sent receipt
        else:
            from .ingress import unconfirmed_event
            if unconfirmed_event(db,event['id']):return None
            if event['id'].startswith('imported.'):
                return None
            if db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?',(event['id'],)).fetchone():return None
            if db.execute("SELECT 1 FROM channel_sessions WHERE session_id=? AND channel!='desktop'",(event['session_id'],)).fetchone():return None
            selected=make_scope(event['persona_id'],'desktop')
        original_text, recorded = event['text'], event['created_at']
        if turn and turn['source_key'].startswith('imported.'):
            material_row = next((e for e in projection['members'] + projection['replies'] if e['id']==event['id']), None)
            if not material_row or not material_row['text']:
                return None
            original_text, recorded = material_row['text'], material_row['created_at']
        data={'text':original_text,'scope':selected,'session_id':event['session_id'],
            'origin_epoch':epoch(recorded) if recorded else None,'version':fingerprint([dict(event),original_text]) if turn and turn['source_key'].startswith('imported.') else fingerprint(dict(event)),
            'lineage':event['id'],'source_type':'user_statement' if event['role']=='user' else 'assistant_statement',
            'canonical_id':event['id']}
    elif kind=='native':
        if not ref.get('key','').startswith(('knowledge.conversation.record.','knowledge.native.imported.')):
            raise RuntimeErrorBase('recall_source_reference_invalid')
        row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(ref['key'],)).fetchone()
        if not row:return None
        record=json.loads(row[0])['data'];pid=record['persona_id']
        from .knowledge_native_memory import eligible,apply_states
        root=record.get('root_turn_id');turn=None
        if root:
            if not eligible(db,root):return None
            turn=dict(db.execute('SELECT * FROM turns WHERE id=?',(root,)).fetchone())
            if turn['persona_id']!=pid or turn['channel']!=record['channel']:return None
        selected=turn_scope(db,turn) if turn else make_scope(pid,record['channel'])
        original=next((s for s in record.get('sources',[]) if s['id']==ref['id']),None)
        if not original:return None
        sources,_=apply_states(db,pid,[original],{})
        if not sources:return None
        source=sources[0]
        text='\n'.join(str(source.get(k) or '') for k in ('title','url','summary','content'))
        data={'text':text,'scope':selected,'session_id':turn['session_id'] if turn else None,
            'origin_epoch':epoch(turn['created_at']) if turn else None,
            'version':fingerprint(source),'lineage':source['id'],'source_type':source.get('kind','public_source'),
            'canonical_id':source['id']}
    else:raise RuntimeErrorBase('recall_kind_invalid')
    offset=ref.get('offset',0)
    if type(offset) is not int or offset<0 or offset%CHUNK_CHARS:raise RuntimeErrorBase('recall_fragment_invalid')
    if offset>=len(data['text']):return None
    data['text']=data['text'][offset:offset+CHUNK_CHARS]
    data['skip_fragment']=not data['text'].strip()
    data['scope']=normalize_scope(data['scope'])
    data['lineage']=fingerprint([kind,data['lineage'],offset])
    return data

def scan(db,pid,kind,cursor,limit,now,*,only_row=None,imported_session=None):
    """Keyset pagination over canonical rows, resumable even within a large page."""
    rowid,part=cursor
    if kind=='memory':sql='SELECT rowid AS source_row,id FROM memories WHERE persona_id=? AND rowid>=? ORDER BY rowid'
    elif kind=='dialogue':sql='SELECT rowid AS source_row,id FROM events WHERE persona_id=? AND rowid>=? ORDER BY rowid'
    elif kind=='native':sql="SELECT rowid AS source_row,key,value FROM knowledge_control WHERE json_extract(value,'$.data.persona_id')=? AND (key LIKE 'knowledge.conversation.record.%' OR key LIKE 'knowledge.native.imported.%') AND rowid>=? ORDER BY rowid"
    else:raise RuntimeErrorBase('recall_kind_invalid')
    result=[];next_cursor=[rowid,part];seen=0
    # Bound row reads even when every source is withdrawn or otherwise ineligible.
    if only_row is not None:
        sql=sql.replace(' ORDER BY rowid',' AND rowid='+str(int(only_row))+' ORDER BY rowid')
    args = [pid,rowid]
    if imported_session is not None:
        if kind != 'dialogue':
            raise RuntimeErrorBase('recall_kind_invalid')
        sql = sql.replace(' ORDER BY rowid', " AND session_id=? AND id LIKE 'imported.%' ORDER BY rowid")
        args.append(imported_session)
    for row in db.execute(sql+' LIMIT ?',(*args,limit+1)):
        if seen>=limit: return result,next_cursor,False
        seen+=1;rid=row['source_row'];skip=part if rid==rowid else 0
        if kind=='native':
            record=json.loads(row['value'])['data']
            refs=[{'key':row['key'],'id':s['id']} for s in record.get('sources',[])]
        else:refs=[{'id':row['id']}]
        n=0
        for base in refs:
            offset=0
            while True:
                ref=base|{'offset':offset};data=material(db,kind,ref,now=now)
                if data is None:break
                if n>=skip:
                    if len(result)>=limit:return result,[rid,n],False
                    result.append((ref,data))
                n+=1;offset+=CHUNK_CHARS
        next_cursor=[rid+1,0]
    return result,next_cursor,True
