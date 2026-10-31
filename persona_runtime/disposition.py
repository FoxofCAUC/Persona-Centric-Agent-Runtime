"""Author-owned source projections. No model dispatch and no prompt mutation."""
import json
from copy import deepcopy

from .archive import encoded, fingerprint
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .profile import check_persona, head as profile_head
from .character import head as character_head, decode_version
from .disposition_core.contracts import (ACTIONS, VERSION, ContractError,
                                         validate_input)

SHADOW_VERSION='persona-disposition-shadow-v1'
COMPILER_VERSION='disposition-compiler-v1'


def fail(code):
    raise RuntimeErrorBase(code)


def source_catalog(conn,persona_id,versions=None):
    """Read only versioned public canon, never private source files or history."""
    check_persona(conn,persona_id);rows=[]
    versions=versions or {}
    def add(kind,version,path,value,entry_id=''):
        if value is None or value=='' or value==[]:return
        body=value if isinstance(value,str) else encoded(value)
        rows.append({'source_id':fingerprint([kind,path,entry_id])[:32],'kind':kind,
            'version_id':version,'path':path,'entry_id':entry_id,'text':body,
            'content_hash':fingerprint(body)})
    for kind in ('core','expression','relationship'):
        if kind in versions:
            row=conn.execute('SELECT * FROM profile_versions WHERE id=? AND persona_id=? AND user_id=? AND kind=?',
                             (versions[kind],persona_id,LOCAL_USER_ID,kind)).fetchone()
            if not row:fail('disposition_source_version_unavailable')
            v=dict(row);v['data']=json.loads(v['data_json']);v['source_ids']=json.loads(v['source_ids_json'])
        else:v=profile_head(conn,persona_id,kind)
        if kind=='relationship' and v['source_ids']:continue
        fields={'core':('core_traits','values_and_boundaries','introduction','initial_background'),
                'expression':('style',),'relationship':('description',)}[kind]
        for field in fields:add(kind,v['id'],field,v['data'].get(field))
    if 'character' in versions:
        row=conn.execute('SELECT * FROM character_versions WHERE id=? AND persona_id=? AND user_id=?',
                         (versions['character'],persona_id,LOCAL_USER_ID)).fetchone()
        if not row:fail('disposition_source_version_unavailable')
        v=decode_version(row)
    else:v=character_head(conn,persona_id)
    for group in ('interests','social'):
        for field,value in sorted(v['data'][group].items()):add('character',v['id'],group+'.'+field,value)
    for group in ('lorebook','examples'):
        for entry in v['data'][group]:
            if not entry['enabled'] or entry.get('visibility','conversation')!='conversation':continue
            # Title/keyword/example are creator material; no relationship or history extraction.
            add('character',v['id'],group,{k:x for k,x in entry.items() if k not in ('enabled','visibility')},entry['id'])
    if versions:
        authorized={r['source_id'] for r in source_catalog(conn,persona_id)}
        rows=[r for r in rows if r['source_id'] in authorized]
    return rows


def projection_head(conn,pid):
    row=conn.execute('SELECT * FROM disposition_heads WHERE persona_id=? AND user_id=?',(pid,LOCAL_USER_ID)).fetchone()
    return dict(row) if row else {'persona_id':pid,'user_id':LOCAL_USER_ID,'draft_id':None,'active_id':None,'revision':0}


def version(conn,pid,vid):
    row=conn.execute('SELECT * FROM disposition_versions WHERE id=? AND persona_id=? AND user_id=?',(vid,pid,LOCAL_USER_ID)).fetchone()
    if not row:fail('disposition_record_unavailable')
    value=dict(row);value['document']=json.loads(value.pop('document_json'));return value


def status_for(conn,pid,vid,*,current=True):
    if not vid:return 'not_configured'
    v=version(conn,pid,vid)
    if conn.execute('SELECT 1 FROM disposition_revocations WHERE persona_id=? AND version_id=?',(pid,vid)).fetchone():return 'withdrawn'
    if v['kind']!='confirmed':return 'draft_unconfirmed'
    doc=v['document']
    if not doc['definitions']:return 'not_configured'
    try:
        frozen={r['source_id']:r for r in source_catalog(conn,pid,doc['source_versions'])}
        latest={r['source_id']:r for r in source_catalog(conn,pid)}
        for d in doc['definitions']:
            for ref in d['source_refs']:
                old=frozen.get(ref['source_id']);now=latest.get(ref['source_id'])
                if old is None or old['content_hash']!=ref['content_hash']:return 'source_invalid'
                # Disappearance/disable/private is unavailable even to accepted old requests.
                if now is None:return 'source_unavailable'
                if current and now['version_id']!=ref['version_id']:return 'needs_review'
    except RuntimeErrorBase:return 'source_unavailable'
    return 'ready'


def accepted_snapshot(conn,pid,mode):
    if mode not in ('baseline','shadow','assist'):fail('invalid_disposition_mode')
    if mode=='baseline':return {'mode':'baseline','policy':SHADOW_VERSION,'kernel':VERSION,'compiler':COMPILER_VERSION,'version_id':None,'status':'disabled','document_hash':None}
    h=projection_head(conn,pid);vid=h['active_id']
    state=status_for(conn,pid,vid)
    return {'mode':mode,'policy':SHADOW_VERSION,'kernel':VERSION,'compiler':COMPILER_VERSION,
            'version_id':vid if state=='ready' else None,'status':state,
            'document_hash':fingerprint(version(conn,pid,vid)['document']) if state=='ready' else None}


def normalized_document(conn,pid,document,*,require_current=True):
    if not isinstance(document,dict) or set(document)!= {'schema_version','source_versions','definitions'} or type(document['schema_version']) is not int or document['schema_version']!=1:
        fail('invalid_disposition_document')
    if not isinstance(document['source_versions'],dict) or not set(document['source_versions'])<= {'core','character','expression','relationship'}:
        fail('invalid_disposition_sources')
    catalog={r['source_id']:r for r in source_catalog(conn,pid,document['source_versions'])}
    current={r['source_id']:r for r in source_catalog(conn,pid)}
    definitions=document['definitions']
    if type(definitions) is not list or len(definitions)>32:fail('invalid_disposition_document')
    scope={'persona_id':pid,'user_id':LOCAL_USER_ID,'channel':'desktop','bot':'','peer':''}
    core_sources={};core_defs=[];seen=set()
    for d in definitions:
        if not isinstance(d,dict) or set(d)!= {'id','label','source_refs','triggers','preferences','priority','enabled'}:
            fail('invalid_disposition_definition')
        if type(d['label']) is not str or not 1<=len(d['label'].strip())<=160:fail('invalid_disposition_label')
        if type(d['enabled']) is not bool or d['id'] in seen:fail('invalid_disposition_definition')
        seen.add(d['id']);refs=[]
        if type(d['source_refs']) is not list or not 1<=len(d['source_refs'])<=8:fail('invalid_disposition_sources')
        for ref in d['source_refs']:
            if not isinstance(ref,dict) or set(ref)!= {'source_id','version_id','path','entry_id','content_hash','start','end','fragment_hash'}:
                fail('invalid_disposition_reference')
            r=catalog.get(ref['source_id']);now=current.get(ref['source_id'])
            if r is None or now is None:fail('disposition_source_unavailable')
            if any(ref[k]!=r[k] for k in ('version_id','path','entry_id','content_hash')):fail('disposition_source_mismatch')
            if require_current and now['version_id']!=r['version_id']:fail('disposition_source_changed')
            start,end=ref['start'],ref['end']
            if type(start) is not int or type(end) is not int or not 0<=start<end<=len(r['text']) or end-start>2000:
                fail('invalid_disposition_span')
            if fingerprint(r['text'][start:end])!=ref['fragment_hash']:fail('disposition_fragment_mismatch')
            core_sources[r['source_id']]={'id':r['source_id'],'scope':scope,'kind':r['kind'],'version':r['version_id'],
                'content_hash':r['content_hash'],'allowed':True,'withdrawn':False}
            refs.append({'id':r['source_id'],'version':r['version_id'],'content_hash':r['content_hash']})
        core_defs.append({'id':d['id'],'persona_id':pid,'source_refs':refs,'confirmed':True,'enabled':d['enabled'],
                          'triggers':d['triggers'],'preferences':d['preferences'],'priority':d['priority'],'revision':1})
    try:
        validate_input({'scope':scope,'members':[{'id':'validation','text':'人工校验'}],'sources':list(core_sources.values()),
            'definitions':core_defs,'context':{'relationship':'neutral','relationship_source_id':'','information_gap':False,
            'reference_gap':False,'resource_unavailable':False,'boundary_source_id':''},'policy':VERSION})
    except (ContractError,TypeError,KeyError):fail('invalid_disposition_definition')
    if len(encoded(document).encode())>128*1024:fail('disposition_document_too_large')
    return deepcopy(document)


def append_version(conn,pid,document,kind,*,parent=None,restored=None,attempt=None):
    vid=identifier();revision=conn.execute('SELECT coalesce(max(revision),0)+1 FROM disposition_versions WHERE persona_id=?',(pid,)).fetchone()[0]
    conn.execute('INSERT INTO disposition_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
        (vid,pid,LOCAL_USER_ID,revision,kind,encoded(document),parent,restored,attempt,timestamp()))
    return vid


class DispositionService:
    def __init__(self,store,ingress=None):self.store,self.ingress=store,ingress

    def _check(self,conn,pid,write=False):
        check_persona(conn,pid)
        if write:
            from .private_personas import guard_acceptance_write
            guard_acceptance_write(self.store,pid)
            if self.store.current(conn)!=pid:fail('disposition_selection_conflict')

    def current(self,persona_id):
        with self.store.connection() as conn:
            self._check(conn,persona_id);h=projection_head(conn,persona_id)
            return h|{'status':status_for(conn,persona_id,h['active_id'])}

    def sources(self,persona_id,versions=None):
        with self.store.connection() as conn:
            self._check(conn,persona_id);rows=source_catalog(conn,persona_id,versions)
            return {'sources':rows,'source_characters':sum(len(r['text']) for r in rows),'max_source_characters':12000}

    def show(self,persona_id,version_id):
        with self.store.connection() as conn:
            self._check(conn,persona_id);v=version(conn,persona_id,version_id)
            # Source pointers remain visible; no historical source-body authorization.
            return v|{'status':status_for(conn,persona_id,version_id)}

    def history(self,persona_id):
        with self.store.connection() as conn:
            self._check(conn,persona_id)
            return [{k:v for k,v in dict(r).items() if k!='document_json'} for r in conn.execute(
                'SELECT * FROM disposition_versions WHERE persona_id=? ORDER BY revision DESC LIMIT 50',(persona_id,))]

    def compare(self,persona_id,left_id,right_id):
        a=self.show(persona_id,left_id)['document'];b=self.show(persona_id,right_id)['document']
        return {'left_id':left_id,'right_id':right_id,'changed':a!=b,'before':a,'after':b}

    def _write(self,persona_id,expected_revision,operation_id,action,payload,pending_action=None):
        if type(operation_id) is not str or not 1<=len(operation_id)<=200:fail('invalid_operation_id')
        fp=fingerprint([persona_id,expected_revision,action,payload,pending_action])
        with self.store.connection(write=True) as conn:
            self._check(conn,persona_id,True)
            old=conn.execute('SELECT * FROM disposition_operations WHERE operation_id=?',(operation_id,)).fetchone()
            if old:
                if old['persona_id']!=persona_id or old['fingerprint']!=fp:fail('submission_conflict')
                return json.loads(old['result_json'])|{'already_applied':True}
            h=projection_head(conn,persona_id)
            if type(expected_revision) is not int or h['revision']!=expected_revision:fail('disposition_revision_conflict')
            if action in ('confirm','withdraw'):
                pending=conn.execute("SELECT * FROM input_batches WHERE persona_id=? AND state='collecting'",(persona_id,)).fetchall()
                if pending and (pending_action not in ('seal','cancel') or self.ingress is None):fail('pending_batch_resolution_required')
                for row in pending:
                    if pending_action=='seal':self.ingress._seal(conn,dict(row),'disposition_changed')
                    else:conn.execute("UPDATE input_batches SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",(timestamp(),row['id']))
            draft,active=h['draft_id'],h['active_id']
            if action=='draft-update':
                doc=normalized_document(conn,persona_id,payload['document'])
                draft=append_version(conn,persona_id,doc,'draft',parent=draft)
            elif action=='restore':
                source=version(conn,persona_id,payload['version_id'])
                # Rebinding to latest is never automatic. Author must edit/recompile if stale.
                doc=normalized_document(conn,persona_id,source['document'])
                draft=append_version(conn,persona_id,doc,'draft',parent=draft,restored=source['id'])
            elif action=='confirm':
                if payload['draft_id']!=draft or payload['expected_active_id']!=active:fail('disposition_revision_conflict')
                doc=normalized_document(conn,persona_id,version(conn,persona_id,draft)['document'])
                active=append_version(conn,persona_id,doc,'confirmed',parent=draft)
                if not doc['definitions']:active=None
            elif action=='withdraw':
                target=payload['version_id']
                version(conn,persona_id,target)
                conn.execute('INSERT INTO disposition_revocations VALUES(?,?,?,?,?,?)',
                    (identifier(),persona_id,LOCAL_USER_ID,target,operation_id,timestamp()))
                # Keep the pointer as an auditable withdrawn head. Snapshots
                # never activate it; a later explicit confirmation replaces it.
            else:fail('invalid_disposition_operation')
            conn.execute('INSERT INTO disposition_heads VALUES(?,?,?,?,?) ON CONFLICT(persona_id) DO UPDATE SET draft_id=excluded.draft_id,active_id=excluded.active_id,revision=excluded.revision',
                (persona_id,LOCAL_USER_ID,draft,active,h['revision']+1))
            result=projection_head(conn,persona_id)
            conn.execute('INSERT INTO disposition_operations VALUES(?,?,?,?,?)',(operation_id,persona_id,fp,encoded(result),timestamp()))
        if self.ingress:self.ingress.schedule_committed()
        return result

    def draft_update(self,persona_id,document,expected_revision,operation_id):
        return self._write(persona_id,expected_revision,operation_id,'draft-update',{'document':document})

    def confirm(self,persona_id,draft_id,expected_active_id,expected_revision,operation_id,pending_action=None):
        return self._write(persona_id,expected_revision,operation_id,'confirm',{'draft_id':draft_id,'expected_active_id':expected_active_id},pending_action)

    def restore(self,persona_id,version_id,expected_revision,operation_id):
        return self._write(persona_id,expected_revision,operation_id,'restore',{'version_id':version_id})

    def withdraw(self,persona_id,version_id,expected_revision,operation_id,pending_action=None):
        return self._write(persona_id,expected_revision,operation_id,'withdraw',{'version_id':version_id},pending_action)


def clone_projection(conn,source_pid,target_pid):
    old=projection_head(conn,source_pid)['active_id']
    if not old or status_for(conn,source_pid,old)!='ready':return False
    document=deepcopy(version(conn,source_pid,old)['document']);new={r['source_id']:r for r in source_catalog(conn,target_pid)}
    versions={}
    for d in document['definitions']:
        for ref in d['source_refs']:
            row=new.get(ref['source_id'])
            if not row or row['content_hash']!=ref['content_hash']:return False
            ref['version_id']=row['version_id'];versions[row['kind']]=row['version_id']
    document['source_versions']=versions
    document=normalized_document(conn,target_pid,document)
    vid=append_version(conn,target_pid,document,'confirmed')
    conn.execute('INSERT INTO disposition_heads VALUES(?,?,?,?,1)',(target_pid,LOCAL_USER_ID,None,vid))
    return True
