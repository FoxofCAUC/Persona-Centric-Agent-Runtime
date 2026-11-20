"""Same-session private dialogue material. Internal records and unpainted drafts stay outside the dialogue."""
import json
from contextlib import contextmanager
from contextvars import ContextVar

from .archive import fingerprint, read_exact, turn_scope
from .config import ConfigStore
from .context_builders import memory_usable
from .import_history import chronological, history_allowed
from .knowledge_routing import configured_model
from .models import RuntimeErrorBase
from .session_privacy import current, destination, recipients
from .turn_input import load_turn_input

_access = ContextVar('private_history_access', default=None)


@contextmanager
def history_access(db, session, enabled):
    token = _access.set((db, session) if enabled else None)
    try:
        yield
    finally:
        _access.reset(token)


def session_access(db, sid):
    return _access.get() == (db, sid)


def full_revision(store, db, pid, sid, model, role):
    if not sid or not db.execute("""SELECT 1 FROM session_privacy_events
            WHERE persona_id=? AND session_id=? AND enabled=1 LIMIT 1""",(pid,sid)).fetchone():
        return None
    config = ConfigStore(store.paths)
    cfg = config.load()
    from .private_reply_entry import PRIVATE_ROUTES,retired,selected
    if selected(db,cfg['instance_id'],pid) not in PRIVATE_ROUTES or retired(db,cfg['instance_id']):
        return None
    endpoints = recipients(config,cfg)
    state = current(db,config,cfg,pid,sid,endpoints)
    if state['enabled'] and endpoints.get(role) == destination(config,model):
        return state['revision']
    return None


def memory_in_session(db, mid, pid, sid):
    from .import_memory_scope import memory_source_ids
    ids = memory_source_ids(db,mid)
    return bool(ids) and all(db.execute("""SELECT 1 FROM events WHERE id=? AND persona_id=? AND session_id=?""",
                                        (event,pid,sid)).fetchone() for event in ids)


def main_history_access(store,db,pid,sid):
    full=store and full_revision(store,db,pid,sid,ConfigStore(store.paths).load()['model'],'main')
    return history_access(db,sid,bool(full))


@contextmanager
def memory_access(store,db,payload,model):
    revision=(payload.get('automatic_memory') or {}).get('session_privacy_revision')
    if revision is None:
        yield
        return
    sid=payload['session_id']
    if revision!=full_revision(store,db,payload['persona_id'],sid,model,'organization'):
        raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    with history_access(db,sid,True):
        yield


def _dialogue(db, turn, tid, jev, full):
    from .import_history import group
    source = db.execute("SELECT * FROM turns WHERE id=? AND persona_id=? AND session_id=?",
                        (tid,turn['persona_id'],turn['session_id'])).fetchone()
    if not source or source['status'] != 'completed':
        return []
    source = dict(source)
    target = {'kind':'turn','id':tid}
    scope = turn_scope(db,turn)
    imported = source['source_key'].startswith('imported.')
    if imported:
        if not full and not history_allowed(db,turn['session_id'],'jev',jev):
            return []
        result = group(db,source,scope,target)
    else:
        result = read_exact(db,scope,target)
    if result.get('status') != 'found':
        return []
    rows = []
    for event in chronological(result):
        role=event.get('role','assistant')
        if role != 'user' and not imported:
            if not _confirmed(db,tid,event):
                continue
        rows.append({'id':event['id'],'role':role,'text':event['text'],
                     'time':event.get('created_at') or source['created_at']})
    return rows


def _confirmed(db,tid,event):
    from .tk_notice import already_presented
    if event.get('kind')=='search_notice' and already_presented(db,tid):
        return True
    for pubs, visible in (('expression_speech_publications','expression_speech_visible'),
                          ('expression_publications','expression_utterances')):
        pub=db.execute(f"SELECT id FROM {pubs} WHERE canonical_event_id=?",(event['id'],)).fetchone()
        if pub:
            parts=db.execute(f"SELECT ordinal,text FROM {visible} WHERE publication_id=? ORDER BY ordinal",(pub[0],))
            prefix=[]
            for ordinal,text in parts:
                if ordinal!=len(prefix):
                    break
                prefix.append(text)
            return bool(prefix) and ''.join(prefix)==event['text']
    return False


def collect(entry, db, turn, contract, frozen=None):
    from .chat import terms
    from .references import prepare, ReferenceError
    from .profile import model_persona_data, source_blocked
    pid,sid = turn['persona_id'],turn['session_id']
    full = contract['consent']['enabled']
    jev = configured_model(entry.store.paths)
    if not full:
        from .private_personas import guard_cloud_dispatch
        guard_cloud_dispatch(entry.store,pid,'knowledge_router',jev,db=db)
    profile = json.loads(turn['profile_snapshot'])
    if any(source_blocked(db,item['source_ids']) for item in profile.values()):
        raise RuntimeErrorBase('private_reply_source_changed')
    current_rows = load_turn_input(db,turn)
    current_ids = [e['id'] for e in current_rows]
    catalog = {e['id']:{'id':e['id'],'text':e['text'],'role':'user','time':e['created_at']} for e in current_rows}
    recent_ids, refs, memory_ids = [], [], []
    previous_exchange = []
    with history_access(db,sid,full):
        history = frozen['history'] if frozen else [r[0] for r in db.execute("""SELECT id FROM turns WHERE session_id=? AND persona_id=? AND channel='desktop'
            AND status='completed' AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY rowid DESC LIMIT 4""",
            (sid,pid,turn['id']))]
        for previous in reversed(history):
            for item in _dialogue(db,turn,previous,jev,full):
                catalog[item['id']] = item
                recent_ids.append(item['id'])
                if previous==history[0]:
                    previous_exchange.append(item['id'])
        try:
            _, manifest = prepare(db,turn)
        except ReferenceError as exc:
            raise RuntimeErrorBase('private_reply_reference_unavailable') from exc
        for ref in manifest['references']:
            target = ref['target']
            rows = _dialogue(db,turn,ref['turn_id'],jev,full)
            accepted=json.loads(db.execute("SELECT binding_json FROM input_references WHERE event_id=?",
                                (ref['input_event_id'],)).fetchone()[0])
            retained=set(accepted['member_ids']+accepted['reply_ids'])
            scoped = [e for e in rows if e['id'] in retained and (target['kind']=='turn' or e['id']==target['id'])]
            if not scoped:
                raise RuntimeErrorBase('private_reply_reference_unavailable')
            for e in scoped:
                catalog[e['id']] = e
                if e['id'] not in refs:
                    refs.append(e['id'])
        query = terms(' '.join(e['text'] for e in current_rows))
        memories = []
        selection = (" AND m.id IN (SELECT value FROM json_each(?))" if frozen else "")
        parameters = (pid,sid,json.dumps(frozen['memories'])) if frozen else (pid,sid)
        for row in db.execute("""SELECT m.* FROM memories m JOIN events e ON e.id=m.source_event_id
                WHERE m.persona_id=? AND e.session_id=? AND m.status='active'""" + selection +
                " ORDER BY m.rowid DESC LIMIT 128",parameters):
            item = dict(row)
            if not memory_in_session(db,item['id'],pid,sid) or not memory_usable(db,turn,item):
                continue
            if not full and not memory_usable(db,turn,item,purpose='jev',providers=jev):
                continue
            memories.append((len(query & terms(item['text'])),item))
        for _,item in sorted(memories,key=lambda pair:pair[0],reverse=True)[:8]:
            mid=item['id']
            catalog[mid]={'id':mid,'role':'memory','text':item['text'],'revision':item['revision']}
            memory_ids.append(mid)
    from .character_context import select_context
    chosen=select_context(json.loads(turn['character_snapshot']), ' '.join(row['text'] for row in current_rows),
                          example_limit=0)
    character=[]
    for part in chosen['parts']:
        ident='character/'+part['id']
        catalog[ident]={'id':ident,'role':'character','text':part['text'],'version':chosen['version_id']}
        character.append(ident)
    if frozen and 'admitted_ids' in frozen:
        admitted=set(frozen['admitted_ids'])
        recent_ids=[i for i in recent_ids if i in admitted]
        previous_exchange=[i for i in previous_exchange if i in admitted]
        catalog={k:v for k,v in catalog.items() if k in admitted}
    return {'current':current_ids,'recent':recent_ids,'references':refs,'memories':memory_ids,'character':character,
            'history':history,'previous_exchange':previous_exchange,'jev_model':jev['name'],
            'catalog':catalog,'persona':{kind:model_persona_data(item['data']) for kind,item in profile.items()},
            'profile_versions':{kind:item['id'] for kind,item in profile.items()}}


def selected_proof(material, ids):
    return {i:fingerprint(material['catalog'][i]) for i in ids if i in material['catalog']}
