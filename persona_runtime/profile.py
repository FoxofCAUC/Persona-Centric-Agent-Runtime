"""Explicit identity-preserving versions. No model calls or automatic state inference."""
import hashlib
import json
from datetime import datetime, timezone

from .models import LOCAL_USER_ID, PersonaSpec, RuntimeErrorBase, identifier, require_text, timestamp

KINDS = ('core', 'expression', 'relationship', 'state')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def check_persona(conn, persona_id):
    if not conn.execute('SELECT 1 FROM personas WHERE id=?', (persona_id,)).fetchone():
        raise RuntimeErrorBase('Persona not found')


def head(conn, persona_id, kind):
    row = conn.execute('SELECT v.* FROM profile_heads h JOIN profile_versions v ON v.id=h.version_id WHERE h.persona_id=? AND h.kind=?', (persona_id, kind)).fetchone()
    if not row:
        raise RuntimeErrorBase('profile_not_initialized')
    result = dict(row)
    result['data'] = json.loads(result.pop('data_json'))
    result['source_ids'] = json.loads(result.pop('source_ids_json'))
    return result


def source_blocked(conn, source_ids):
    return any(conn.execute('SELECT 1 FROM source_exclusions WHERE event_id=?', (event_id,)).fetchone() for event_id in source_ids)


def append_version(conn, persona_id, kind, data, *, actor='user', source_ids=(), restored_from=None, project=True):
    old = conn.execute('SELECT version_id FROM profile_heads WHERE persona_id=? AND kind=?', (persona_id,kind)).fetchone()
    revision = conn.execute('SELECT coalesce(max(revision),0)+1 FROM profile_versions WHERE persona_id=? AND kind=?', (persona_id,kind)).fetchone()[0]
    version_id = identifier()
    conn.execute('INSERT INTO profile_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                 (version_id,persona_id,LOCAL_USER_ID,kind,revision,encode(data),encode(list(source_ids)),actor,old[0] if old else None,restored_from,timestamp()))
    conn.execute('INSERT INTO profile_heads VALUES (?,?,?) ON CONFLICT(persona_id,kind) DO UPDATE SET version_id=excluded.version_id', (persona_id,kind,version_id))
    if not project:
        return head(conn,persona_id,kind)
    if kind in ('core','expression'):
        spec = json.loads(conn.execute('SELECT spec_json FROM personas WHERE id=?', (persona_id,)).fetchone()[0])
        spec.update(data if kind=='core' else {'expression_style':data['style']})
        PersonaSpec.from_dict(spec)
        conn.execute('UPDATE personas SET name=?,spec_json=? WHERE id=?', (spec['name'],encode(spec),persona_id))
    elif kind=='relationship':
        conn.execute('INSERT INTO relationships VALUES (?,?,?,?) ON CONFLICT(persona_id,user_id) DO UPDATE SET description=excluded.description,updated_at=excluded.updated_at', (persona_id,LOCAL_USER_ID,data['description'],timestamp()))
    return head(conn,persona_id,kind)


def initialize_profiles(conn, persona_id=None, actor='migration'):
    rows=conn.execute('SELECT id,spec_json FROM personas WHERE (? IS NULL OR id=?)', (persona_id,persona_id)).fetchall()
    for row in rows:
        spec=json.loads(row['spec_json'])
        relation=conn.execute('SELECT description FROM relationships WHERE persona_id=? AND user_id=?',(row['id'],LOCAL_USER_ID)).fetchone()
        data={'core':{k:v for k,v in spec.items() if k not in ('format_version','expression_style')},
              'expression':{'style':spec['expression_style']},'relationship':{'description':relation[0] if relation else ''},
              'state':{'text':'','expires_at':None}}
        for kind in KINDS:
            if not conn.execute('SELECT 1 FROM profile_heads WHERE persona_id=? AND kind=?',(row['id'],kind)).fetchone():
                append_version(conn,row['id'],kind,data[kind],actor=actor,project=False)


def active_state(version, now=None):
    data=version['data']
    if not data['text']:
        return False
    now=now or datetime.now(timezone.utc)
    return datetime.fromisoformat(data['expires_at'])>now


def snapshot(conn, persona_id):
    return {kind:head(conn,persona_id,kind) for kind in ('core','expression','relationship')}


class ProfileService:
    def __init__(self,store):
        self.store=store

    def current(self,kind=None):
        if kind is not None and kind not in KINDS:
            raise RuntimeErrorBase('unknown_profile_kind')
        with self.store.connection() as conn:
            persona=self.store.current(conn)
            result={k:head(conn,persona,k) for k in KINDS if kind is None or k==kind}
            for k,version in result.items():
                version['source_blocked']=source_blocked(conn,version['source_ids'])
                if k=='state':
                    version['active']=active_state(version)
            return result[kind] if kind else result

    @staticmethod
    def owned(conn,table,record_id,persona_id):
        row=conn.execute(f'SELECT * FROM {table} WHERE id=? AND persona_id=? AND user_id=?',(record_id,persona_id,LOCAL_USER_ID)).fetchone()
        if not row:
            raise RuntimeErrorBase('record_not_in_selected_persona')
        return dict(row)

    def history(self,kind):
        if kind not in KINDS:
            raise RuntimeErrorBase('unknown_profile_kind')
        with self.store.connection() as conn:
            rows=[]
            current=head(conn,self.store.current(conn),kind)['id']
            for row in conn.execute('SELECT * FROM profile_versions WHERE persona_id=? AND kind=? ORDER BY revision DESC',(self.store.current(conn),kind)):
                value=dict(row)
                value['data']=json.loads(value.pop('data_json'))
                value['source_ids']=json.loads(value.pop('source_ids_json'))
                value['current']=value['id']==current
                value['source_blocked']=source_blocked(conn,value['source_ids'])
                rows.append(value)
            return rows

    def compare(self,left_id,right_id):
        with self.store.connection() as conn:
            left=self.owned(conn,'profile_versions',left_id,self.store.current(conn))
            right=self.owned(conn,'profile_versions',right_id,self.store.current(conn))
            if left['kind']!=right['kind']:
                raise RuntimeErrorBase('profile_kind_mismatch')
            a,b=json.loads(left['data_json']),json.loads(right['data_json'])
            return {'kind':left['kind'],'left_id':left_id,'right_id':right_id,
                    'changes':{k:{'before':a.get(k),'after':b.get(k)} for k in a.keys()|b.keys() if a.get(k)!=b.get(k)}}

    @staticmethod
    def validate(conn,persona_id,kind,data):
        if not isinstance(data,dict):
            raise RuntimeErrorBase('profile_data_must_be_object')
        if kind=='core':
            spec={'format_version':1,**data,'expression_style':head(conn,persona_id,'expression')['data']['style']}
            PersonaSpec.from_dict(spec)
            if set(data)!={'name','introduction','core_traits','values_and_boundaries','initial_background'}:
                raise RuntimeErrorBase('invalid_core_fields')
        elif kind=='expression':
            if set(data)!={'style'}:
                raise RuntimeErrorBase('invalid_expression_fields')
            require_text(data['style'],'style')
        elif kind=='relationship':
            if set(data)!={'description'} or not isinstance(data['description'],str):
                raise RuntimeErrorBase('invalid_relationship_fields')
        elif kind=='state':
            if set(data)!={'text','expires_at'} or not isinstance(data['text'],str):
                raise RuntimeErrorBase('invalid_state_fields')
            if data['text']:
                try:
                    expiry=datetime.fromisoformat(data['expires_at'])
                    seconds=(expiry-datetime.now(timezone.utc)).total_seconds()
                    if expiry.tzinfo is None or not 0<seconds<=7*86400:
                        raise ValueError()
                    data={'text':data['text'],'expires_at':expiry.astimezone(timezone.utc).isoformat()}
                except (ValueError,TypeError,OverflowError):
                    raise RuntimeErrorBase('state_expiry_must_be_aware_future_within_7_days') from None
            elif data['expires_at'] is not None:
                raise RuntimeErrorBase('empty_state_requires_null_expiry')
        else:
            raise RuntimeErrorBase('unknown_profile_kind')
        if len(encode(data))>16000:
            raise RuntimeErrorBase('profile_too_long')
        return data

    def _write(self,conn,persona_id,kind,data,expected_version,operation_id,*,source_ids=(),actor='user',restored_from=None):
        require_text(operation_id,'operation_id')
        if len(operation_id)>200:
            raise RuntimeErrorBase('invalid_operation_id')
        fingerprint=hashlib.sha256(encode([persona_id,kind,data,expected_version,list(source_ids),restored_from]).encode()).hexdigest()
        old=conn.execute('SELECT * FROM profile_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if old:
            if old['persona_id']!=persona_id or old['fingerprint']!=fingerprint:
                raise RuntimeErrorBase('submission_conflict')
            row=self.owned(conn,'profile_versions',old['version_id'],persona_id)
            return {'id':row['id'],'kind':row['kind'],'revision':row['revision'],'data':json.loads(row['data_json']),'already_applied':True}
        current=head(conn,persona_id,kind)
        if expected_version!=current['id']:
            raise RuntimeErrorBase('profile_version_conflict')
        data=self.validate(conn,persona_id,kind,data)
        if data==current['data'] and list(source_ids)==current['source_ids'] and restored_from is None:
            result=current
        else:
            result=append_version(conn,persona_id,kind,data,source_ids=source_ids,actor=actor,restored_from=restored_from)
        conn.execute('INSERT INTO profile_operations VALUES (?,?,?,?)',(operation_id,persona_id,fingerprint,result['id']))
        return result

    def update(self,kind,data,expected_version,operation_id):
        with self.store.connection(write=True) as conn:
            return self._write(conn,self.store.current(conn),kind,data,expected_version,operation_id)

    def restore(self,version_id,expected_version,operation_id):
        with self.store.connection(write=True) as conn:
            persona=self.store.current(conn)
            row=self.owned(conn,'profile_versions',version_id,persona)
            sources=json.loads(row['source_ids_json'])
            if source_blocked(conn,sources):
                raise RuntimeErrorBase('source_expired')
            return self._write(conn,persona,row['kind'],json.loads(row['data_json']),expected_version,operation_id,
                               source_ids=sources,actor='rollback',restored_from=version_id)

    def propose_relationship(self,text,source_event_id,operation_id):
        require_text(text,'text')
        require_text(operation_id,'operation_id')
        if len(text)>8000 or len(operation_id)>200:
            raise RuntimeErrorBase('proposal_too_long')
        with self.store.connection(write=True) as conn:
            persona=self.store.current(conn)
            event=self.owned(conn,'events',source_event_id,persona)
            if event['role'] not in ('user','note') or source_blocked(conn,[source_event_id]):
                raise RuntimeErrorBase('source_expired_or_not_user_evidence')
            old=conn.execute('SELECT * FROM relationship_proposals WHERE id=?',(operation_id,)).fetchone()
            if old:
                if old['persona_id']!=persona or old['text']!=text or old['source_event_id']!=source_event_id:
                    raise RuntimeErrorBase('submission_conflict')
                return dict(old)
            conn.execute('INSERT INTO relationship_proposals VALUES (?,?,?,?,?,?,?,NULL,?)',(operation_id,persona,LOCAL_USER_ID,text,source_event_id,head(conn,persona,'relationship')['id'],'pending',timestamp()))
            return dict(conn.execute('SELECT * FROM relationship_proposals WHERE id=?',(operation_id,)).fetchone())

    def proposals(self):
        with self.store.connection() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM relationship_proposals WHERE persona_id=? ORDER BY rowid DESC',(self.store.current(conn),))]

    def proposal_show(self,proposal_id):
        with self.store.connection() as conn:
            row=self.owned(conn,'relationship_proposals',proposal_id,self.store.current(conn))
            row['source']=self.owned(conn,'events',row['source_event_id'],row['persona_id'])
            row['source_blocked']=source_blocked(conn,[row['source_event_id']])
            return row

    def review_relationship(self,proposal_id,accept,operation_id,text=None):
        require_text(operation_id,'operation_id')
        if len(operation_id)>200:
            raise RuntimeErrorBase('invalid_operation_id')
        if type(accept) is not bool:
            raise RuntimeErrorBase('accept_must_be_boolean')
        with self.store.connection(write=True) as conn:
            persona=self.store.current(conn)
            row=self.owned(conn,'relationship_proposals',proposal_id,persona)
            if row['status'] in ('accepted','rejected'):
                if (row['status']=='accepted')!=accept:
                    raise RuntimeErrorBase('review_conflict')
                if accept and text is not None:
                    applied=self.owned(conn,'profile_versions',row['version_id'],persona)
                    if json.loads(applied['data_json'])['description']!=text:
                        raise RuntimeErrorBase('review_conflict')
                return row
            if row['status']!='pending' or source_blocked(conn,[row['source_event_id']]):
                raise RuntimeErrorBase('source_expired')
            if accept:
                data={'description':row['text'] if text is None else text}
                result=self._write(conn,persona,'relationship',data,row['expected_version'],operation_id,source_ids=[row['source_event_id']],actor='candidate')
            else:
                require_text(operation_id,'operation_id')
                result=None
            conn.execute('UPDATE relationship_proposals SET status=?,version_id=? WHERE id=?',('accepted' if accept else 'rejected',result['id'] if result else None,proposal_id))
            return dict(conn.execute('SELECT * FROM relationship_proposals WHERE id=?',(proposal_id,)).fetchone())
