"""Readable public materials survive protocol expiry; explicit corrections stay effective."""
from copy import deepcopy
import json

from .archive import fingerprint
from .knowledge_service import read
from .models import RuntimeErrorBase
from .turn_input import load_turn_input


def source_states(db, pid):
    return read(db, 'native.source_state.' + pid, {})


def state_for_source(states,source):
    options=[value for key,value in states.items() if key==source['id'] or value.get('url')==source['url']]
    return max(options,key=lambda value:value.get('policy_revision',value.get('revision',0))) if options else {}


def apply_states(db, pid, sources, organized):
    return apply_source_states(source_states(db,pid)['data'],sources,organized)


def apply_source_states(states, sources, organized):
    selected = []; changed = False
    for original in sources:
        row = deepcopy(original); state = state_for_source(states,row)
        if state.get('state') == 'withdrawn':
            changed = True; continue
        row['state_revision'] = state.get('policy_revision',state.get('revision', 0))
        if state.get('state') == 'corrected':
            row.update(content='', summary=state['replacement'], kind='user_correction', material_kind='user_correction')
            row.pop('qualification',None)
            changed = True
        selected.append(row)
    # A blended note cannot be surgically certified after a source correction.
    # Keep original notes in history, but consume only valid readable materials.
    return selected, {} if changed else deepcopy(organized)


def eligible(db, turn_id, *, for_source_edit=False):
    turn = db.execute('SELECT * FROM turns WHERE id=?', (turn_id,)).fetchone()
    if not turn:return False
    if turn['status']!='completed' and not (for_source_edit and turn['status']=='failed'
                                           and turn['error_code']=='knowledge_repeated_notice'):return False
    ids = [turn['user_event_id'], turn['reply_event_id']]
    if 'input_batch_id' in turn.keys() and turn['input_batch_id']:
        ids += [row[0] for row in db.execute('SELECT event_id FROM input_batch_members WHERE batch_id=?', (turn['input_batch_id'],))]
    return not any(db.execute('SELECT 1 FROM source_exclusions WHERE event_id=?', (event_id,)).fetchone()
        for event_id in ids if event_id)


def candidates(db, pid, channel, *, acquired_only=False,for_source_edit=False):
    rows = []
    for (raw,) in db.execute("""SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.conversation.record.%'
            AND json_extract(value,'$.data.persona_id')=? AND json_extract(value,'$.data.channel')=?
            AND (NOT ? OR (json_array_length(value,'$.data.sources')>0
                 AND (COALESCE(json_extract(value,'$.data.retrieval.used_previous'),0)=0
                      OR json_extract(value,'$.data.retrieval.material_acquired')=1)))
            ORDER BY rowid DESC LIMIT 40""", (pid, channel, acquired_only)):
        record = json.loads(raw)['data']
        if eligible(db, record.get('root_turn_id'),for_source_edit=for_source_edit): rows.append(record)
    for (raw,) in db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.native.imported.%' AND json_extract(value,'$.data.persona_id')=? AND json_extract(value,'$.data.channel')=? ORDER BY rowid DESC LIMIT 20", (pid, channel)):
        rows.append(json.loads(raw)['data'])
    return rows


def _inherited(record):
    prior=record.get('retrieval',{})
    return prior.get('used_previous') is True and prior.get('material_acquired') is not True


def _user_text(db, turn):
    if turn.get('input_batch_id') or turn.get('user_event_id'):
        return '\n'.join(member['text'] for member in load_turn_input(db,turn))
    return ''


def _origin_query(db, record):
    root=record.get('root_turn_id')
    if not root:
        return ''
    for task_id in record.get('retrieval',{}).get('task_ids',[]):
        row=db.execute("""SELECT payload_json FROM organization_tasks
            WHERE id=? AND persona_id=? AND channel=? AND status='completed'""",
                       (task_id,record['persona_id'],record['channel'])).fetchone()
        if not row:
            continue
        spec=json.loads(row[0]).get('knowledge_processing',{})
        if spec.get('root_turn_id')==root and spec.get('stage')=='conversation_native':
            return spec.get('input',{}).get('public_task',{}).get('question','')
    return ''


def _continuation_anchor(db, turn, current_rowid):
    """Trace only the uninterrupted same-session chain of readable material."""
    from .knowledge_native_topic import is_followup
    source_ids=None
    for row in db.execute("""SELECT * FROM turns WHERE session_id=? AND persona_id=? AND channel=? AND rowid<?
            ORDER BY rowid DESC LIMIT 40""",(turn['session_id'],turn['persona_id'],turn['channel'],current_rowid)):
        if row['status']!='completed' or not eligible(db,row['id']):
            break
        record=read(db,'conversation.record.'+row['id'],{})['data']
        if not record.get('sources'):
            break
        ids={s['id'] for s in record['sources']}
        source_ids=ids if source_ids is None else source_ids & ids
        if not source_ids:
            break
        text=_user_text(db,dict(row))
        if not _inherited(record) or not is_followup(text):
            return row['id'],source_ids,text
    return None


def previous(db, turn, question):
    from .recall_runtime import selection as recall_selection
    recalled=recall_selection(db,turn)
    if recalled is not None:
        for item in recalled['selected']:
            if item['kind']!='native':continue
            for (raw,) in db.execute('SELECT reference_json FROM recall_support_locations WHERE unit_id=? ORDER BY observed_epoch DESC',(item['id'],)):
                ref=json.loads(raw)
                row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(ref.get('key'),)).fetchone()
                if not row:continue
                record=json.loads(row[0])['data']
                sources,organized=apply_states(db,turn['persona_id'],record.get('sources',[]),record.get('organized',{}))
                supporting=set(organized.get('source_ids',[]))
                sources=[s for s in sources if not supporting or s['id'] in supporting]
                if any(s['id']==item['source_id'] for s in sources):return {'sources':sources,'organized':organized,'record':record}
        return {'sources':[],'organized':{},'record':None}
    from .knowledge_native_topic import is_followup, TopicQuery
    query=_user_text(db,turn) or question
    subject=TopicQuery(query)
    followup=is_followup(query)
    current=db.execute('SELECT rowid FROM turns WHERE id=?',(turn['id'],)).fetchone() if followup else None
    anchor=_continuation_anchor(db,turn,current[0]) if current else None
    earlier_subject=TopicQuery(anchor[2]) if anchor else None
    ranked=[]
    for index, record in enumerate(candidates(db, turn['persona_id'], turn['channel'], acquired_only=True)):
        if record.get('root_turn_id')==turn['id'] or _inherited(record):
            continue
        sources, organized = apply_states(db, turn['persona_id'], record.get('sources', []), record.get('organized', {}))
        support_ids=set(organized.get('source_ids',[]))
        if support_ids:
            supporting=[source for source in sources if source['id'] in support_ids]
            if supporting:sources=supporting
        if not sources: continue
        sid = record.get('session_id')
        if sid is None and record.get('root_turn_id'):
            row = db.execute('SELECT session_id FROM turns WHERE id=?', (record['root_turn_id'],)).fetchone()
            sid = row[0] if row else None
        same = sid is not None and sid == turn['session_id']
        labels={'previous_question':_origin_query(db,record),'titles':[s.get('title') or '' for s in sources],
                'urls':[s['url'] for s in sources],'notes':organized.get('notes') or ''}
        score=subject.score(**labels)
        continuing=bool(anchor and anchor[1].intersection(s['id'] for s in sources) and (
            record.get('root_turn_id')==anchor[0] or earlier_subject.score(**labels)))
        # API callers can inspect a proposed turn before it is persisted.
        if not score and not continuing and not (followup and not current and same):
            continue
        ranked.append((score, int(continuing), int(same), -index, sources, organized, record))
    if not ranked: return {'sources': [], 'organized': {}, 'record': None}
    *_, sources, organized, record = max(ranked, key=lambda item: item[:4])
    return {'sources': sources, 'organized': organized, 'record': record}


def validate_live(db, pid, metadata, record):
    if not record.get('retrieval', {}).get('executor'): return
    root = db.execute('SELECT status FROM turns WHERE id=?', (record.get('root_turn_id'),)).fetchone()
    if not root or root[0] == 'completed': return  # Historical speech stays history.
    states = source_states(db, pid)['data']
    used = set(metadata.get('source_ids', [row['id'] for row in record.get('sources', [])]))
    for row in record.get('sources', []):
        state = state_for_source(states,row)
        if row['id'] in used and (state.get('state') == 'withdrawn' or state.get('policy_revision',state.get('revision', 0)) != row.get('state_revision', 0)):
            raise RuntimeErrorBase('native_readable_source_changed')


def snapshot(db, pid=None):
    records = []
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'personas','turns','events','knowledge_control'} <= tables:
        return {'version':1,'records':[],'source_states':{}}
    pids = [pid] if pid else [row[0] for row in db.execute('SELECT id FROM personas')]
    for owner in pids:
        for record in candidates(db, owner, 'desktop'):
            if not record.get('sources'):continue
            # No reasoning, signatures, replay account binding or execution IDs.
            records.append({'id': fingerprint([owner, record.get('root_turn_id'), record.get('sources')]),
                'persona_id': owner, 'channel': 'desktop', 'sources': deepcopy(record.get('sources', [])),
                'organized': {key: deepcopy(value) for key, value in record.get('organized', {}).items()
                    if key in ('notes', 'source_ids', 'kind')}})
    states = {}
    for owner in pids:
        owned=[row for record in records if record['persona_id']==owner for row in record['sources']]
        current=source_states(db,owner)['data']
        if owned:states[owner]={row['id']:deepcopy(state_for_source(current,row)) for row in owned if state_for_source(current,row)}
    return {'version': 1, 'records': records, 'source_states': states}


def validate_snapshot(value):
    try:
        if value['version'] != 1 or not isinstance(value['records'], list) or not isinstance(value['source_states'], dict): raise ValueError()
        from .knowledge_general import public_url
        for record in value['records']:
            if set(record) != {'id', 'persona_id', 'channel', 'sources', 'organized'} or record['channel'] != 'desktop': raise ValueError()
            if not isinstance(record['organized'].get('notes', ''), str): raise ValueError()
            for source in record['sources']:
                public_url(source['url'])
                if not isinstance(source['id'], str): raise ValueError()
        owners={record['persona_id'] for record in value['records']}
        if set(value['source_states'])-owners:raise ValueError()
        for owner,states in value['source_states'].items():
            if not isinstance(states,dict):raise ValueError()
            ids={row['id'] for record in value['records'] if record['persona_id']==owner for row in record['sources']}
            for source_id,state in states.items():
                if (source_id not in ids or not isinstance(state,dict) or set(state)-{'state','revision','replacement','url','policy_revision'}
                        or state.get('state') not in ('active','withdrawn','corrected')
                        or type(state.get('revision')) is not int or state['revision']<1):raise ValueError()
                if state['state']=='corrected' and (not isinstance(state.get('replacement'),str) or not state['replacement'].strip()):raise ValueError()
                if 'url' in state:public_url(state['url'])
                if 'policy_revision' in state and (type(state['policy_revision']) is not int or state['policy_revision']<1):raise ValueError()
        def forbidden(item):
            if isinstance(item, dict): return any(key in ('signature', 'reasoning_content', 'server_tool_calls', 'encrypted_key', 'account_binding') or forbidden(child) for key, child in item.items())
            return isinstance(item, list) and any(forbidden(child) for child in item)
        if forbidden(value): raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise RuntimeErrorBase('native_readable_snapshot_invalid') from None


def merge_snapshot(db, value, imported_persona=None):
    validate_snapshot(value)
    for record in value['records']:
        owner = imported_persona or record['persona_id']; data = deepcopy(record)
        mapping = {row['id']: fingerprint([owner, row['id']]) if imported_persona else row['id'] for row in data['sources']}
        for row in data['sources']: row['id'] = mapping[row['id']]
        data['organized']['source_ids'] = [mapping[key] for key in data['organized'].get('source_ids', []) if key in mapping]
        data.update(persona_id=owner, session_id=None, root_turn_id=None, acquisition_origin='imported_readable_snapshot')
        key = 'knowledge.native.imported.' + owner + '.' + fingerprint(data)
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO NOTHING', (key, json.dumps({'revision': 0, 'data': data}, ensure_ascii=False)))
        current = source_states(db, owner); states = deepcopy(current['data'])
        for old_id, state in value['source_states'].get(record['persona_id'], {}).items():
            if old_id in mapping: states.setdefault(mapping[old_id], deepcopy(state))
        if states!=current['data']:
            revision=max([current['revision']]+[item.get('policy_revision',item.get('revision',0)) for item in states.values()])+1
            db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('knowledge.native.source_state.' + owner, json.dumps({'revision':revision, 'data': states}, ensure_ascii=False)))
