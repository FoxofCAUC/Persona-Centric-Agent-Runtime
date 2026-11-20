"""Explicit native-session memory consent for a visible-only private acceptance copy."""
import hashlib
import hmac
import json

from .config import ConfigStore
from .models import LOCAL_USER_ID, RuntimeErrorBase
from .private_personas import PrivatePersonas, provider_identity
from .security import unprotect


def _key(pid):
    return 'private.copy.session_memory.'+pid


def has_decision(db,pid):
    return db.execute('SELECT 1 FROM knowledge_control WHERE key=?',(_key(pid),)).fetchone() is not None


def _account(config, model):
    secret=model.get('encrypted_key')
    if not secret:return None
    return hmac.new(config.token().encode(),unprotect(secret).encode(),hashlib.sha256).hexdigest()


def _copy(store, db, pid):
    owner=PrivatePersonas(store.paths)
    if owner.record_for_persona(pid,db=db):return None
    policy=owner.acceptance_copy_policy(pid)
    if not policy or policy.get('copy_scope')!='visible_only_v1':return None
    marker=json.loads((store.paths.home/'private-sandbox.json').read_bytes())
    return policy['_copy_binding'] if marker.get('visible_only') is True else None


def set_consent(store, pid, sid, expected_provider, expected_revision, *, enabled):
    """Compare-and-set by a local operator following explicit consent or withdrawal."""
    if type(enabled) is not bool or type(expected_revision) is not int or expected_revision<0:
        raise RuntimeErrorBase('invalid_session_memory_decision')
    config=ConfigStore(store.paths)
    with store.connection(write=True) as db:
        row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(_key(pid),)).fetchone()
        previous=json.loads(row[0]) if row else {}
        revision=previous.get('revision',0)
        copy=_copy(store,db,pid)
        if not copy:raise RuntimeErrorBase('private_copy_scope_invalid')
        if enabled:
            if store.current(db)!=pid or not db.execute("""SELECT 1 FROM session_heads
                WHERE session_id=? AND persona_id=? AND user_id=? AND channel='desktop' AND bot='' AND peer=''""",
                (sid,pid,LOCAL_USER_ID)).fetchone():
                raise RuntimeErrorBase('private_session_mismatch')
            cfg=config.load()
            model=cfg['model'] if cfg['organization']['reuse_chat'] else cfg['organization']['model']
            account=_account(config,model)
            if not account or provider_identity(model)!=expected_provider:raise RuntimeErrorBase('private_provider_changed')
        else:
            if not previous or (sid,expected_provider)!=(previous['session_id'],previous['provider']):
                raise RuntimeErrorBase('private_session_mismatch')
            account=previous['account']
        decision={'enabled':enabled,'persona_id':pid,'session_id':sid,'provider':expected_provider,
                  'account':account,'copy_binding':copy,'revision':expected_revision+1}
        if decision==previous:return revision
        if revision!=expected_revision:raise RuntimeErrorBase('version_conflict')
        db.execute("""INSERT INTO knowledge_control VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(_key(pid),json.dumps(decision)))
        return decision['revision']


def grant(store, db, pid, sid, model):
    """Return the live revision, or None when the copy has no matching grant."""
    row=db.execute('SELECT value FROM knowledge_control WHERE key=?',(_key(pid),)).fetchone()
    if not row:return None
    value=json.loads(row[0])
    if (value['enabled'] and value['persona_id']==pid and value['session_id']==sid
            and value['provider']==provider_identity(model) and value['copy_binding']==_copy(store,db,pid)
            and value['account']==_account(ConfigStore(store.paths),model)):
        return value['revision']
    return None


def local_fact(db, mid, pid, sid):
    from .import_memory_scope import memory_source_ids
    sources=memory_source_ids(db,mid)
    return bool(sources) and all(eid and not eid.startswith('imported.') and db.execute(
        """SELECT 1 FROM events WHERE id=? AND persona_id=? AND session_id=? AND user_id=?
           UNION ALL SELECT 1 FROM reply_delivery_parts p JOIN turns t ON t.id=p.turn_id
           WHERE p.id=? AND t.persona_id=? AND t.session_id=? AND p.user_id=? AND t.channel='desktop'
           AND t.source_key NOT GLOB 'imported.*'""",(eid,pid,sid,LOCAL_USER_ID,eid,pid,sid,LOCAL_USER_ID)).fetchone()
        for eid in sources)


def check_task(store, db, task, payload, model):
    """A session grant is usable only by the extract/verify tasks owned by its memory batch."""
    from .import_history import chronological
    from .import_memory_scope import source_group, imported
    meta=payload['automatic_memory']
    pid,sid=task['persona_id'],task['session_id']
    full=meta.get('session_privacy_revision')
    if full:
        from .ordinary_materials import full_revision,session_access
        if full_revision(store,db,pid,sid,model,'organization')!=full or not session_access(db,sid):
            raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    else:
        rev=grant(store,db,pid,sid,model)
        if not rev or meta.get('copy_session_revision')!=rev:raise RuntimeErrorBase('private_cloud_provider_not_authorized')
    batch=db.execute('SELECT * FROM memory_batches WHERE id=? AND persona_id=? AND session_id=?',
                     (meta.get('batch_id'),pid,sid)).fetchone()
    stage=meta.get('stage')
    if (not batch or stage not in ('extract','verify') or task['id']!=batch[stage+'_task_id']
            or task['channel']!='desktop' or payload['session_id']!=sid or payload['persona_id']!=pid):
        raise RuntimeErrorBase('private_session_mismatch')
    previous=meta.get('previous_fragment')
    if previous and not db.execute("SELECT 1 FROM memory_fragments WHERE id=? AND persona_id=? AND session_id=?",
                                   (previous,pid,sid)).fetchone():
        raise RuntimeErrorBase('private_session_mismatch')
    events=[]
    for ref in payload['bindings']:
        turn=db.execute("SELECT rowid AS ordinal,* FROM turns WHERE id=? AND persona_id=? AND session_id=? AND channel='desktop'",
                        (ref['turn_id'],pid,sid)).fetchone()
        if not turn or imported(turn) and not full:raise RuntimeErrorBase('private_session_mismatch')
        actual=source_group(db,payload['scope'],dict(turn),model=model)
        if actual['status']!='found':raise RuntimeErrorBase('source_expired')
        for e in chronological(actual):
            events.append({'id':e['id'],'kind':'part' if e.get('kind')=='part' else 'event',
                           'role':e.get('role','assistant'),'text':e['text'],'turn':turn['ordinal'],'turn_id':turn['id']})
    if not events or events!=payload['events']:raise RuntimeErrorBase('private_session_mismatch')
    if stage=='verify' and payload.get('proposals')!=json.loads(batch['data_json']).get('proposals'):
        raise RuntimeErrorBase('private_session_mismatch')
    for m in [*payload['existing_memories'],*payload['outdated_claims']]:
        from .ordinary_materials import memory_in_session
        belongs=memory_in_session if full else local_fact
        if not belongs(db,m['id'],pid,sid):raise RuntimeErrorBase('private_session_mismatch')


def main():
    import argparse
    from .paths import RuntimePaths
    from .storage import Store
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('authorize','revoke'))
    parser.add_argument('--home',required=True)
    parser.add_argument('--persona',required=True)
    parser.add_argument('--session',required=True)
    parser.add_argument('--provider',required=True,help='Reviewed provider identity')
    parser.add_argument('--expected-revision',type=int,required=True)
    args=parser.parse_args()
    store=Store(RuntimePaths.from_environment(args.home))
    revision=set_consent(store,args.persona,args.session,args.provider,args.expected_revision,enabled=args.action=='authorize')
    print(json.dumps({'revision':revision,'enabled':args.action=='authorize'}))


if __name__=='__main__':
    main()
