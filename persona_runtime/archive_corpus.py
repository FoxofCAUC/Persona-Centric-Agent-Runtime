"""Bounded, transaction-consistent projections of canonical ordinary archives."""
import json

from .archive import encoded, fingerprint, project_turn
from .archive_query import MAX_DOCUMENTS, MAX_SOURCE_CHARS, SearchFailure
from .models import LOCAL_USER_ID, timestamp


def chunks(rows, size=400):
    for start in range(0, len(rows), size):
        yield rows[start:start+size]


def capture(store, spec, budget):
    selected = spec['scope']
    with store.connection() as conn:
        budget.watch(conn)
        if not conn.execute('SELECT 1 FROM personas WHERE id=?', (selected['persona_id'],)).fetchone():
            raise SearchFailure('unavailable')
        args = [selected['persona_id'], selected['channel'], selected['bot'], selected['peer']]
        sql = """SELECT t.rowid AS creation_order,t.id,t.input_batch_id,t.persona_id,t.session_id,
            t.channel,t.user_event_id,t.reply_event_id,t.status,t.created_at,t.origin_json FROM turns t
            LEFT JOIN input_batches b ON b.id=t.input_batch_id
            WHERE t.persona_id=? AND t.channel=? AND t.status='completed'
            AND COALESCE(b.bot,json_extract(t.origin_json,'$.bot'),'')=?
            AND COALESCE(b.peer,json_extract(t.origin_json,'$.sender'),'')=?"""
        if spec['session_id']:
            sql += ' AND t.session_id=?'
            args.append(spec['session_id'])
            known = conn.execute('SELECT 1 FROM turns t LEFT JOIN input_batches b ON b.id=t.input_batch_id WHERE t.session_id=? AND t.persona_id=? AND t.channel=? AND COALESCE(b.bot,json_extract(t.origin_json,\'$.bot\'),\'\')=? AND COALESCE(b.peer,json_extract(t.origin_json,\'$.sender\'),\'\')=? LIMIT 1',
                                 (spec['session_id'], *args[:4])).fetchone()
            current = conn.execute('SELECT 1 FROM channel_sessions WHERE session_id=? AND persona_id=? AND channel=? AND user_id=?',
                                   (spec['session_id'], selected['persona_id'],selected['channel'],LOCAL_USER_ID)).fetchone()
            if not known and not (current and selected['channel']=='desktop'):
                raise SearchFailure('unavailable')
        # The same source set as load_turn_sources, including the reply Event.
        sql += """ AND NOT EXISTS(SELECT 1 FROM source_exclusions x WHERE
            x.event_id=t.user_event_id OR x.event_id=t.reply_event_id OR
            x.event_id IN (SELECT m.event_id FROM input_batch_members m WHERE m.batch_id=t.input_batch_id))
            ORDER BY t.rowid DESC,t.id ASC LIMIT ?"""
        args.append(50 if spec['window']=='recent_50' else MAX_DOCUMENTS+1)
        turns = [dict(r) for r in conn.execute(sql,args)]
        if len(turns)>MAX_DOCUMENTS:
            raise SearchFailure('query_budget_exceeded')
        groups, documents = [], []
        characters = 0
        for batch in chunks(turns):
            budget.check()
            turn_ids = [t['id'] for t in batch]
            marks = ','.join('?' for _ in turn_ids)
            member_rows = list(conn.execute(f'''SELECT t.id AS tid,m.member_order,e.id,e.role,e.text,e.created_at
                FROM turns t JOIN input_batch_members m ON m.batch_id=t.input_batch_id
                JOIN events e ON e.id=m.event_id AND e.persona_id=t.persona_id AND e.user_id=?
                WHERE t.id IN ({marks}) ORDER BY m.member_order''',(LOCAL_USER_ID,*turn_ids)))
            members = {}
            for row in member_rows:
                members.setdefault(row['tid'],[]).append({k:row[k] for k in ('id','role','text','created_at')})
            event_ids = list({t['user_event_id'] for t in batch}|{t['reply_event_id'] for t in batch if t['reply_event_id']})
            events = {}
            for event_chunk in chunks(event_ids):
                slots=','.join('?' for _ in event_chunk)
                events.update({r['id']:dict(r) for r in conn.execute(f'SELECT id,role,text,created_at FROM events WHERE id IN ({slots})',event_chunk)})
            parts = {}
            for row in conn.execute(f'SELECT id,turn_id,text,state,platform_message_id,part_index,sent_at,created_at FROM reply_delivery_parts WHERE turn_id IN ({marks}) ORDER BY part_index',turn_ids):
                parts.setdefault(row['turn_id'],[]).append(dict(row))
            raws = {}
            for row in conn.execute(f'''SELECT t.id AS tid,r.id,r.event_id,r.platform_message_id,r.platform_time,r.received_at,r.representation
                FROM turns t JOIN input_batch_members m ON m.batch_id=t.input_batch_id
                JOIN raw_events r ON r.event_id=m.event_id WHERE t.id IN ({marks}) ORDER BY m.member_order''',turn_ids):
                raws.setdefault(row['tid'],[]).append({k:row[k] for k in row.keys() if k!='tid'})
            for turn in batch:
                budget.check()
                tid = turn['id']
                mem = members.get(tid,[]) if turn['input_batch_id'] else [events[turn['user_event_id']]]
                semantic = events.get(turn['reply_event_id'])
                group = project_turn(turn,selected,{'kind':'turn','id':tid},mem,parts.get(tid,[]),raws.get(tid,[]),
                                     {k:semantic[k] for k in ('id','text')} if semantic else None,False)
                if group['status']!='found':
                    continue
                groups.append(group)
                raw_by_id = {r['event_id']:r for r in group['raw_metadata']}
                part_by_id = {p['id']:p for p in parts.get(tid,[])}
                for source in group['members']+group['replies']:
                    sid=source['id']; is_part=source.get('kind')=='part'
                    raw = raw_by_id.get(sid,{})
                    recorded = part_by_id[sid]['created_at'] if is_part else (source.get('created_at') or (semantic or {}).get('created_at'))
                    document = {'document_id':sid,'turn_id':tid,'session_id':turn['session_id'],
                        'target':{'kind':'part' if is_part else 'event','id':sid},
                        'source_kind':'qq_sent_part' if is_part else 'user_event' if source.get('role')=='user' else 'desktop_reply',
                        'text':source['text'],'source_hash':fingerprint(source['text']),
                        'times':{'recorded':recorded,'received':raw.get('received_at'),'platform':raw.get('platform_time'),
                                 'sent':part_by_id[sid]['sent_at'] if is_part else None},'coverage':group['coverage']}
                    characters += len(document['text'])
                    documents.append(document)
                    if len(documents)>MAX_DOCUMENTS or characters>MAX_SOURCE_CHARS:
                        raise SearchFailure('query_budget_exceeded')
        budget.check()
        # Include membership, order, eligibility, delivery state and time gaps.
        payload = {'scope':selected,'session_id':spec['session_id'],'window':spec['window'],
                   'groups':groups,'documents':documents}
        return payload | {'digest':fingerprint(payload),'read_at':timestamp(),'characters':characters}


def window_manifest(corpus):
    return {'kind':corpus['window'],'order':'turn_creation_desc','turn_ids':[g['turn_id'] for g in corpus['groups']],
            'turn_count':len(corpus['groups']),'source_digest':corpus['digest'],
            'coverage':'most_recent_50_usable_turns' if corpus['window']=='recent_50' else 'authorized_partition'}
