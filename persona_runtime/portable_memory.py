"""Portable business history is reviewable data, never executable authorization."""
import json
from .models import LOCAL_USER_ID,PersonaSpec,RuntimeErrorBase,identifier,timestamp
from .organization import digest,encoded


def import_export(store,package,expected_selected,operation_id):
    if not isinstance(package,dict) or package.get('format_version') not in (17,18) or not isinstance(operation_id,str) or not operation_id:
        raise RuntimeErrorBase('invalid_business_export')
    def secrets(v):
        if isinstance(v,dict):return any(k in ('encrypted_key','admin_token','bridge_token','claim_token','model_snapshot') or secrets(x) for k,x in v.items())
        if isinstance(v,list):return any(secrets(x) for x in v)
        return False
    if secrets(package):raise RuntimeErrorBase('portable_credentials_forbidden')
    try:
        source_pid=package['persona']['id'];spec=PersonaSpec.from_dict(package['persona']['spec'])
        sessions=package['sessions'];events=package['events'];memories=package['memories']
        if any(not isinstance(rows,list) for rows in (sessions,events,memories)):raise ValueError()
        sids={s['id'] for s in sessions};eids={e['id']:e for e in events};mids={m['id'] for m in memories}
        if len(sids)!=len(sessions) or len(eids)!=len(events) or len(mids)!=len(memories):raise ValueError()
        for rows in (sessions,events,memories):
            if any(r['persona_id']!=source_pid or r['user_id']!=LOCAL_USER_ID for r in rows):raise ValueError()
        for e in events:
            if e['session_id'] not in sids or e['role'] not in ('user','persona','system') or not isinstance(e['text'],str):raise ValueError()
        for m in memories:
            if m['source_event_id'] not in eids or m['root_id'] not in mids or m['previous_id'] and m['previous_id'] not in mids:raise ValueError()
            if m['status'] not in ('active','superseded','retracted') or type(m['revision']) is not int or m['revision']<1:raise ValueError()
        for s in package.get('memory_sources',[]):
            if s['memory_id'] not in mids or s['event_id'] not in eids or not s['quote'] or s['quote'] not in eids[s['event_id']]['text']:raise ValueError()
        for s in package.get('source_exclusions',[]):
            if s['event_id'] not in eids or s['persona_id']!=source_pid:raise ValueError()
    except (KeyError,TypeError,ValueError):raise RuntimeErrorBase('invalid_business_export') from None
    fp=digest([package,expected_selected])
    with store.connection(write=True) as db:
        old=db.execute('SELECT * FROM memory_operations WHERE id=?',(operation_id,)).fetchone()
        if old:
            if old['fingerprint']!=fp:raise RuntimeErrorBase('submission_conflict')
            return json.loads(old['result_json'])
        if store.current(db)!=expected_selected:raise RuntimeErrorBase('persona_selection_conflict')
        from .persona_management import active
        active(db,expected_selected)
        from .private_personas import PrivatePersonas
        private=PrivatePersonas(store.paths);policy=private.record_for_persona(source_pid)
        protected=bool(policy or package.get('private_protected'))
        pid=identifier();now=timestamp();mapping={source_pid:pid}
        for rows in (sessions,events,memories):
            for row in rows:
                if row['id'] in mapping:raise RuntimeErrorBase('portable_identity_collision')
                mapping[row['id']]=identifier()
        db.execute('INSERT INTO personas VALUES(?,?,?,?,?)',(pid,spec.name,1,encoded(spec.to_dict()),now))
        from .profile import initialize_profiles
        from .character import initialize_characters
        from .context_policy import initialize
        initialize_profiles(db,pid,actor='import');initialize_characters(db,pid,actor='import');initialize(db,pid,new_persona=True)
        if policy:
            db.execute('INSERT INTO persona_lifecycle VALUES(?,?,?,?,NULL,0)',(pid,LOCAL_USER_ID,None,policy['persona_id']))
        # Keep all exported audit/source/temporal records without restoring
        # sender identities, task leases, policies or old execution IDs.
        archived={'package':package,'private_review_required':protected,'imported_as_review_only':True,'identity_map':mapping}
        db.execute('INSERT INTO memory_imports VALUES(?,?,?,?,?,?)',(identifier(),pid,LOCAL_USER_ID,package['format_version'],encoded(archived),now))
        for s in sessions:db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(mapping[s['id']],pid,LOCAL_USER_ID,s.get('scope','private'),s['created_at']))
        for e in events:db.execute('INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,?,?,?)',(mapping[e['id']],mapping[e['session_id']],pid,LOCAL_USER_ID,e['role'],e['text'],e['created_at']))
        for s in package.get('source_exclusions',[]):db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(mapping[s['event_id']],pid,LOCAL_USER_ID,s['reason'],s['created_at']))
        remaining=list(memories)
        exported_facts={f['memory_id']:f for f in package.get('memory_facts',[])}
        while remaining:
            progressed=False
            for m in remaining[:]:
                if m['previous_id'] and not db.execute('SELECT 1 FROM memories WHERE id=?',(mapping[m['previous_id']],)).fetchone():continue
                # A root references itself; revisions are inserted in parent order.
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mapping[m['id']],pid,LOCAL_USER_ID,m['category'],m['text'],mapping[m['source_event_id']],mapping[m['root_id']],mapping.get(m['previous_id']),m['revision'],m['status'],m['created_at'],m['updated_at']))
                f=exported_facts.get(m['id'],{})
                db.execute('INSERT INTO memory_facts VALUES(?,?,?,?,?,?,?,?,NULL,?)',(mapping[m['id']],pid,LOCAL_USER_ID,encoded({'persona_id':pid,'user_id':LOCAL_USER_ID,'channel':'desktop','bot':'','peer':''}),f.get('subject','imported'),f.get('attribute',m['category']),f.get('valid_from'),f.get('valid_until'),encoded({'import_review_required':True,'original_fact':f,'original_memory_id':m['id']})))
                remaining.remove(m);progressed=True
            if not progressed:raise RuntimeErrorBase('portable_memory_cycle')
        for s in package.get('memory_sources',[]):db.execute('INSERT INTO memory_sources VALUES(?,?,?)',(mapping[s['memory_id']],mapping[s['event_id']],s['quote']))
        result={'id':pid,'format_version':package['format_version'],'history_imported':True,'memory_review_required':True,'automation_enabled':False,'permissions_imported':False}
        db.execute('INSERT INTO memory_operations VALUES(?,?,?,?,?,?)',(operation_id,pid,LOCAL_USER_ID,fp,encoded(result),now))
    return result
