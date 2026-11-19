"""Portable business history is reviewable data, never executable authorization."""
import json
from .models import LOCAL_USER_ID,PersonaSpec,RuntimeErrorBase,identifier,timestamp
from .organization import digest,encoded


HISTORY_REVIEW = "imported_history_review_required"
REVIEW_MARK = "persona.portable.history.checked."


def inherited_history_remaps(db, archive_id):
    """Follow identity maps retained by older portable exports through each copy."""
    row = db.execute("""SELECT json_extract(data_json,'$.identity_map'),
        json_extract(data_json,'$.package.memory_imports') FROM memory_imports
        WHERE id=? AND json_array_length(data_json,'$.package.memory_imports')>0""",(archive_id,)).fetchone()
    if not row:
        return
    pending = [(old,(json.loads(row[0]),)) for old in json.loads(row[1])]
    while pending:
        old, parents = pending.pop()
        if not isinstance(old,dict) or not isinstance(old.get("data_json"),str):
            continue
        try:
            archive = json.loads(old["data_json"])
        except (ValueError,TypeError):
            continue
        if not isinstance(archive,dict) or not isinstance(archive.get("identity_map"),dict):
            continue
        remap = archive["identity_map"]
        for source,destination in remap.items():
            if not source.startswith("imported."):
                continue
            for parent in parents:
                destination = parent.get(destination) if isinstance(destination,str) else None
                if destination is None:
                    break
            if isinstance(destination,str):
                yield destination
        package = archive.get("package")
        if isinstance(package,dict) and isinstance(package.get("memory_imports"),list):
            pending.extend((prior,(remap,*parents)) for prior in package["memory_imports"])


def protect_history_remaps(db, import_id=None):
    """Reapply source restrictions after portable imports assign new event IDs."""
    query = """SELECT i.id,i.persona_id,i.user_id FROM memory_imports i
               WHERE NOT EXISTS(SELECT 1 FROM knowledge_control c WHERE c.key=?||i.id)"""
    params = [REVIEW_MARK]
    if import_id is not None:
        query += " AND i.id=?"
        params.append(import_id)
    for archive_id, pid, uid in db.execute(query, params).fetchall():
        inherited = list(inherited_history_remaps(db,archive_id))
        db.execute("""INSERT INTO source_exclusions(event_id,persona_id,user_id,reason,created_at)
            SELECT e.id,e.persona_id,e.user_id,?,? FROM events e JOIN (
                SELECT remap.value AS id FROM memory_imports i,json_each(i.data_json,'$.identity_map') remap
                WHERE i.id=? AND remap.key GLOB 'imported.*'
                UNION ALL SELECT value AS id FROM json_each(?)
            ) original ON original.id=e.id
            WHERE e.persona_id=? AND e.user_id=?
            ON CONFLICT(event_id) DO UPDATE SET reason=excluded.reason,created_at=excluded.created_at
            WHERE source_exclusions.reason='memory_revised'""",(HISTORY_REVIEW,timestamp(),archive_id,json.dumps(inherited),pid,uid))
        db.execute("INSERT INTO knowledge_control(key,value) VALUES(?, 'true')", (REVIEW_MARK+archive_id,))


def import_export(store,package,expected_selected,operation_id):
    if not isinstance(package,dict) or package.get('format_version') not in (17,18,19,20,21,22,23) or not isinstance(operation_id,str) or not operation_id:
        raise RuntimeErrorBase('invalid_business_export')
    def secrets(v):
        if isinstance(v,dict):return any(k in ('encrypted_key','admin_token','bridge_token','claim_token','model_snapshot','sender_json','console_token','session_token') or secrets(x) for k,x in v.items())
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
            if e['session_id'] not in sids or e['role'] not in ('user','persona','note','system') or not isinstance(e['text'],str):raise ValueError()
        for m in memories:
            if m['source_event_id'] not in eids or m['root_id'] not in mids or m['previous_id'] and m['previous_id'] not in mids:raise ValueError()
            if m['status'] not in ('active','superseded','retracted') or type(m['revision']) is not int or m['revision']<1:raise ValueError()
        for s in package.get('memory_sources',[]):
            if s['memory_id'] not in mids or s['event_id'] not in eids or not s['quote'] or s['quote'] not in eids[s['event_id']]['text']:raise ValueError()
        for s in package.get('source_exclusions',[]):
            if s['event_id'] not in eids or s['persona_id']!=source_pid:raise ValueError()
    except (KeyError,TypeError,ValueError):raise RuntimeErrorBase('invalid_business_export') from None
    if package['format_version']==20 and package.get('knowledge',{}).get('web'):
        raise RuntimeErrorBase('invalid_business_export')
    if package['format_version']<22 and package.get('knowledge',{}).get('web',{}).get('version')==2:raise RuntimeErrorBase('invalid_business_export')
    if package['format_version']<23 and package.get('knowledge',{}).get('web',{}).get('version')==3:raise RuntimeErrorBase('invalid_business_export')
    if package['format_version'] >= 20:
        from .knowledge_transfer import validate
        validate(package.get('knowledge'))
        native=package['knowledge'].get('native_readable',{})
        if (any(row['persona_id']!=source_pid for row in native.get('records',[]))
                or any(owner!=source_pid for owner in native.get('source_states',{}))):
            raise RuntimeErrorBase('invalid_business_export')
        if any(row['owner_persona'] != source_pid for row in package['knowledge']['tables']['knowledge_batches']):
            raise RuntimeErrorBase('invalid_business_export')
        if any(row['owner_persona']!=source_pid for row in package['knowledge'].get('web',{}).get('documents',[])):
            raise RuntimeErrorBase('invalid_business_export')
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
        archive_id = identifier()
        db.execute('INSERT INTO memory_imports VALUES(?,?,?,?,?,?)',(archive_id,pid,LOCAL_USER_ID,package['format_version'],encoded(archived),now))
        for s in sessions:db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(mapping[s['id']],pid,LOCAL_USER_ID,s.get('scope','private'),s['created_at']))
        for e in events:
            role = "note" if e["role"] == "system" else e["role"]
            db.execute('INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,?,?,?)',
                       (mapping[e["id"]],mapping[e["session_id"]],pid,LOCAL_USER_ID,role,e["text"],e["created_at"]))
        for s in package.get('source_exclusions',[]):db.execute('INSERT INTO source_exclusions VALUES(?,?,?,?,?)',(mapping[s['event_id']],pid,LOCAL_USER_ID,s['reason'],s['created_at']))
        protect_history_remaps(db, archive_id)
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
        if package['format_version']>=19:
            latest={}
            for r in package.get('proactive_rules',[]):
                if r['rule_id'] not in latest or r['revision']>latest[r['rule_id']]['revision']:latest[r['rule_id']]=r
            for rule in latest.values():
                data=json.loads(rule['data_json']);data['enabled']=False
                db.execute('INSERT INTO proactive_rules VALUES(?,?,?,?,?,?)',(identifier(),identifier(),pid,1,encoded(data),now))
        if package['format_version'] >= 20:
            from .knowledge_transfer import merge_snapshot
            merge_snapshot(db, package['knowledge'], imported_persona=pid)
        result={'knowledge_review_required':package['format_version']>=20,'id':pid,'format_version':package['format_version'],'history_imported':True,'memory_review_required':True,'automation_enabled':False,'permissions_imported':False}
        db.execute('INSERT INTO memory_operations VALUES(?,?,?,?,?,?)',(operation_id,pid,LOCAL_USER_ID,fp,encoded(result),now))
    return result
