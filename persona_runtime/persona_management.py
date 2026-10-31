"""Identity-only copy and reversible trash. Canon and historical evidence stay intact."""
import hashlib
import json
from .models import LOCAL_USER_ID,RuntimeErrorBase,identifier,require_text,timestamp

SCHEMA = '''
CREATE TABLE persona_lifecycle (
 persona_id TEXT PRIMARY KEY REFERENCES personas(id),
 user_id TEXT NOT NULL REFERENCES users(id),
 copy_of TEXT REFERENCES personas(id),
 privacy_root TEXT REFERENCES personas(id),
 deleted_at TEXT,
 revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE persona_operations (
 operation_id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(id),
 fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_at TEXT NOT NULL
);
'''
TABLES=('persona_lifecycle','persona_operations')


def active(conn,pid):
    # Older migration fixtures initialize canon before this additive schema exists.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='persona_lifecycle'").fetchone():
        row=conn.execute('SELECT deleted_at FROM persona_lifecycle WHERE persona_id=?',(pid,)).fetchone()
        if row and row[0]:raise RuntimeErrorBase('persona_in_trash')


class PersonaManagement:
    def __init__(self,store):self.store=store

    def _view(self,db,pid):
        from .profile import head
        from .character import head as character_head
        row=db.execute('SELECT id,name FROM personas WHERE id=?',(pid,)).fetchone()
        if not row:raise RuntimeErrorBase('Persona not found')
        meta=db.execute('SELECT * FROM persona_lifecycle WHERE persona_id=?',(pid,)).fetchone()
        from .private_personas import PrivatePersonas
        private=PrivatePersonas(self.store.paths)
        root=meta['privacy_root'] if meta else None
        protected=next((r for r in private.load()['records'] if r.get('persona_id')==pid),None)
        return dict(row)|{'selected':self.store.current(db,required=False)==pid,'current_persona_id':self.store.current(db,required=False),
            'revision':meta['revision'] if meta else 0,'deleted':bool(meta and meta['deleted_at']),
            'copy_of':meta['copy_of'] if meta else None,'privacy_root':root,
            'private_protected':bool(protected or root),'private_original':bool(protected),
            'versions':{**{kind:head(db,pid,kind)['id'] for kind in ('core','expression')},'character':character_head(db,pid)['id']}}

    def inspect(self,persona_id):
        with self.store.connection() as db:return self._view(db,persona_id)

    def _operation(self,db,op,value):
        require_text(op,'operation_id')
        if len(op)>200:raise RuntimeErrorBase('invalid_operation_id')
        fp=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        old=db.execute('SELECT * FROM persona_operations WHERE operation_id=?',(op,)).fetchone()
        if old:
            if old['fingerprint']!=fp:raise RuntimeErrorBase('submission_conflict')
            return fp,json.loads(old['result_json'])|{'already_applied':True}
        return fp,None

    def _save(self,db,op,fp,result):
        db.execute('INSERT INTO persona_operations VALUES(?,?,?,?,?)',(op,LOCAL_USER_ID,fp,json.dumps(result),timestamp()))
        return result

    def _selection(self,db,expected):
        if self.store.current(db)!=expected:raise RuntimeErrorBase('persona_selection_conflict')

    def clone(self,persona_id,expected_versions,expected_selected,operation_id):
        from .private_personas import PrivatePersonas,guard_acceptance_write
        from .profile import initialize_profiles,append_version
        from .character import head,append
        from .context_policy import initialize
        with self.store.connection(write=True) as db:
            fp,old=self._operation(db,operation_id,['clone',persona_id,expected_versions,expected_selected])
            if old:return old
            self._selection(db,expected_selected);view=self._view(db,persona_id);active(db,persona_id)
            if view['versions']!=expected_versions:raise RuntimeErrorBase('version_conflict')
            guard_acceptance_write(self.store,persona_id)
            spec=json.loads(db.execute('SELECT spec_json FROM personas WHERE id=?',(persona_id,)).fetchone()[0])
            new_id=identifier();db.execute('INSERT INTO personas VALUES(?,?,?,?,?)',(new_id,spec['name'],1,json.dumps(spec,ensure_ascii=False),timestamp()))
            initialize_profiles(db,new_id,actor='copy');append(db,new_id,head(db,persona_id)['data'],'copy');initialize(db,new_id,new_persona=True)
            root=persona_id if view['private_original'] else view['privacy_root']
            if root:
                private=PrivatePersonas(self.store.paths)
                record=next(r for r in private.load()['records'] if r.get('persona_id')==root)
                relationship=(private.root/record['id']/'initial-relationship.txt').read_text(encoding='utf-8')
                from .private_personas import canonical
                if canonical(relationship)!=record['relationship_sha256']:raise RuntimeErrorBase('private_original_changed')
                append_version(db,new_id,'relationship',{'description':relationship},actor='creator_copy')
            from .disposition import clone_projection
            clone_projection(db,persona_id,new_id)
            db.execute('INSERT INTO persona_lifecycle VALUES(?,?,?,?,NULL,0)',(new_id,LOCAL_USER_ID,persona_id,root))
            result=self._save(db,operation_id,fp,{'id':new_id,'copied_from':persona_id,'private_protected':bool(root),'history_copied':False})
        callback=getattr(self.store,'character_committed',None)
        if callback:callback(new_id)
        return result

    def _idle(self,db,pid):
        for table,condition in (
            ('turns',"status IN ('queued','running')"),
            ('input_batches',"state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')"),
            ('organization_tasks',"status IN ('queued','running','waiting_quota','interrupted')"),
            ('reply_delivery_parts',"state IN ('pending','sending','failed','uncertain')"),
            ('character_selection_attempts',"status='running'"),
            ('disposition_compile_attempts',"status='running'"),
            ('quota_reservations',"state='active'")):
            if db.execute('SELECT 1 FROM '+table+' WHERE persona_id=? AND '+condition+' LIMIT 1',(pid,)).fetchone():
                raise RuntimeErrorBase('persona_has_pending_work')
        if db.execute("SELECT 1 FROM deliveries d JOIN turns t ON t.id=d.turn_id WHERE t.persona_id=? AND d.state IN ('pending','sending','failed','uncertain') AND NOT EXISTS(SELECT 1 FROM legacy_delivery_dispositions x WHERE x.turn_id=t.id) LIMIT 1",(pid,)).fetchone():
            raise RuntimeErrorBase('persona_has_pending_work')

    def trash(self,persona_id,expected_revision,expected_selected,operation_id):
        return self._change(persona_id,expected_revision,expected_selected,operation_id,True)

    def restore(self,persona_id,expected_revision,expected_selected,operation_id):
        return self._change(persona_id,expected_revision,expected_selected,operation_id,False)

    def _change(self,pid,revision,selected,op,delete):
        if type(revision) is not int:raise RuntimeErrorBase('invalid_revision')
        with self.store.connection(write=True) as db:
            fp,old=self._operation(db,op,['trash' if delete else 'restore',pid,revision,selected])
            if old:return old
            self._selection(db,selected);view=self._view(db,pid)
            if view['revision']!=revision:raise RuntimeErrorBase('version_conflict')
            if delete:
                if view['private_original']:raise RuntimeErrorBase('private_original_delete_forbidden')
                if view['selected']:raise RuntimeErrorBase('switch_persona_before_delete')
                self._idle(db,pid)
            from .private_personas import guard_acceptance_write
            guard_acceptance_write(self.store,pid)
            if view['deleted']!=delete:
                db.execute('INSERT OR IGNORE INTO persona_lifecycle(persona_id,user_id) VALUES(?,?)',(pid,LOCAL_USER_ID))
                db.execute('UPDATE persona_lifecycle SET deleted_at=?,revision=revision+1 WHERE persona_id=?',(timestamp() if delete else None,pid))
            return self._save(db,op,fp,{'id':pid,'deleted':delete,'revision':revision+(view['deleted']!=delete),'history_preserved':True})
