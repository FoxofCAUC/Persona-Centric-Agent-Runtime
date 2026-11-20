"""Reconcile the local evidence in a verified restore copy against the live instance."""
from contextlib import closing
import json
import sqlite3

from . import local_knowledge as library, knowledge_revisions as revisions
from .knowledge_native_memory import state_for_source
from .archive import encoded,fingerprint
from .models import RuntimeErrorBase


def _family(row,spec):
    return (row['scope'],row['persona_id'],row['session_id'],row['consent_revision'],row['grant_hash'],
            revisions.subject_key(spec))


def _rows(db):
    return db.execute("""SELECT f.id AS fact_id,f.previous_id,f.spec_json,f.withdrawn,
                         d.id,d.scope,d.persona_id,d.session_id,d.consent_revision,d.grant_hash,
                         d.url,d.published_at,d.acquired_at
                         FROM local_knowledge_facts f JOIN local_knowledge_documents d ON f.document_id=d.id
                         ORDER BY f.row_id""")


def _readable(db,row,spec,config):
    pid,sid=row['persona_id'],row['session_id']
    if row['withdrawn']:return False
    if row['consent_revision']:
        from . import session_privacy
        cfg=config.load()
        consent=session_privacy.current(db,config,cfg,pid,sid,session_privacy.recipients(config,cfg))
        if not consent['enabled'] or consent['revision']!=row['consent_revision']:return False
        if library._grant(db,cfg['instance_id'],pid,sid,row['consent_revision'])!=row['grant_hash']:return False
    if spec['turn_source']:
        turn,key,signature=spec['turn_source']
        source=library._turn_source(db,pid,sid,turn,key)
        return bool(source and fingerprint(source)==signature)
    if spec['legacy_claim']:return library._legacy(db,pid,spec['legacy_claim']) is not None
    return True


def _restore_source_states(dst,src,rows):
    sessions={}
    for row in rows:
        spec=json.loads(row['spec_json'])
        if not spec['turn_source']:continue
        origin,source_id,_=spec['turn_source']
        pid,sid=row['persona_id'],row['session_id']
        key='private.lookup.sources.'+pid+'.'+sid
        if key not in sessions:
            old=library.read(dst,key,{})
            live=library.read(src,key,{}) if src is not None else {'revision':0,'data':{}}
            sessions[key]=(old,live,{})
        old,live,changes=sessions[key]
        source={'id':source_id,'url':row['url']}
        policy=state_for_source(live['data'],source)
        if policy==state_for_source(old['data'],source) and src is not None:
            active=library._turn_source(src,pid,sid,origin,source_id)
            previous=library._turn_source(dst,pid,sid,origin,source_id)
            if active==previous:continue
        # Retire the old source binding. A new correction needs its own currently authorized receipt.
        changes[source_id]={'state':'withdrawn','policy_revision':max(live['revision'],old['revision'])+1}
        if policy.get('state')=='withdrawn':changes[source_id]['url']=row['url']
    for key,(old,live,changes) in sessions.items():
        if changes:
            dst.execute('INSERT OR REPLACE INTO knowledge_control VALUES(?,?)',('knowledge.'+key,
                        encoded({'revision':max(live['revision'],old['revision'])+1,'data':old['data']|changes})))


def _sync(dst,src,rows,config):
    families={}
    for r in rows:
        spec=json.loads(r['spec_json'])
        families.setdefault(_family(r,spec),set()).add(r['fact_id'])
    existing={r['fact_id'] for r in rows}
    visible=set()
    suppress=set()
    checks=set()
    imported_public=imported_scoped=0
    if src is not None:
        for r in _rows(src):
            spec=json.loads(r['spec_json'])
            family=_family(r,spec)
            if family not in families:continue
            if r['previous_id'] in existing:
                versions,_=revisions.split_conditions(spec['conditions'])
                effective=spec['valid_from'] or spec.get('verification',r)['published_at'] or spec.get('verified_at') or r['acquired_at']
                key=revisions._edge_prefix(r['previous_id'])+r['fact_id']
                dst.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',
                            (key,encoded({'effective':effective,'state':spec['state'],'versions':sorted(versions)})))
            if not _readable(src,r,spec,config):continue
            ident=r['fact_id']
            visible.add(ident)
            if ident in existing:continue
            scoped=r['scope']=='session' or spec['turn_source'] or spec['legacy_claim']
            if (scoped and (not (spec['turn_source'] or spec['legacy_claim']) or not _readable(dst,r,spec,config))
                    or (r['previous_id'] and r['previous_id'] not in existing)):
                # Later private receipts can depend on conversations absent from the old backup.
                suppress.update(families[family])
                continue
            doc=src.execute('SELECT * FROM local_knowledge_documents WHERE id=?',(r['id'],)).fetchone()
            dst.execute('INSERT OR IGNORE INTO local_knowledge_documents VALUES('+','.join('?' for _ in doc)+')',tuple(doc))
            f=src.execute('SELECT id,document_id,previous_id,spec_json,entities,attributes,terms,withdrawn FROM local_knowledge_facts WHERE id=?',(ident,)).fetchone()
            dst.execute('INSERT INTO local_knowledge_facts(id,document_id,previous_id,spec_json,entities,attributes,terms,withdrawn) VALUES(?,?,?,?,?,?,?,?)',tuple(f))
            existing.add(ident)
            if spec.get('verification'):
                checks.add('private.study.acquisition.'+fingerprint([r['id'],spec['verification']['receipt']]))
            if scoped:imported_scoped+=1
            else:imported_public+=1
        # Copy only revision guards whose predecessor exists in this recovery image.
        for ident in existing.intersection(visible):
            prefix=revisions._edge_prefix(ident)
            for guard in src.execute('SELECT key,value FROM knowledge_control WHERE key>=? AND key<?',(prefix,prefix+'~')):
                dst.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',tuple(guard))
            effective=src.execute('SELECT key,value FROM knowledge_control WHERE key=?',('private.study.effective.'+ident,)).fetchone()
            if effective:dst.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',tuple(effective))
        for (pid,) in dst.execute('SELECT id FROM personas'):
            if library.enabled(dst,pid) and not library.enabled(src,pid):
                key='knowledge.policy.'+pid
                dst.execute('INSERT OR REPLACE INTO knowledge_control VALUES(?,?)',
                            (key,encoded(library.read(src,'policy.'+pid,library.POLICY))))
        # Retain the verification records for documents in the resulting image.
        for key in checks:
            check=src.execute('SELECT key,value FROM knowledge_control WHERE key=?',(key,)).fetchone()
            if not check:raise sqlite3.IntegrityError('missing_evidence_verification')
            dst.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',tuple(check))
        for row in rows:
            key='private.study.withdrawn_document.'+row['id']
            guard=src.execute('SELECT key,value FROM knowledge_control WHERE key=?',(key,)).fetchone()
            if guard:dst.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',tuple(guard))
    hidden=0
    for r in rows:
        if not r['withdrawn'] and (r['fact_id'] not in visible or r['fact_id'] in suppress):
            library.withdraw(dst,r['fact_id']);hidden+=1
    _restore_source_states(dst,src,rows)
    return {'imported_public_evidence':imported_public,'imported_scoped_evidence':imported_scoped,
            'disabled_old_evidence':hidden}


def reconcile(database,live,config):
    with closing(sqlite3.connect(database)) as dst:
        dst.row_factory=sqlite3.Row
        if not dst.execute("SELECT 1 FROM sqlite_master WHERE name='local_knowledge_facts'").fetchone():return None
        rows=list(_rows(dst))
        if not rows:return None
        dst.execute('PRAGMA foreign_keys=ON')
        src=None
        try:
            try:
                src=sqlite3.connect(live.as_uri()+'?mode=ro',uri=True)
                src.row_factory=sqlite3.Row
                src.execute('BEGIN')
                src.execute('SELECT id FROM local_knowledge_facts LIMIT 1').fetchone()
            except sqlite3.Error as exc:
                if getattr(exc,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    raise RuntimeErrorBase('local_knowledge_storage_wait') from exc
                if src:src.close()
                src=None
            with dst:
                result=_sync(dst,src,rows,config)
                result['live_evidence_checked']=src is not None
                if dst.execute('PRAGMA foreign_key_check').fetchone():raise sqlite3.IntegrityError('restored_local_evidence')
        finally:
            if src:src.close()
        return result
