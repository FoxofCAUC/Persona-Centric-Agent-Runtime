"""Public retrieval settings and historical source records, without truth gates."""
import copy,json,threading
from .archive import fingerprint
from .models import RuntimeErrorBase,require_text

POLICY={'enabled':False,'channels':['desktop']}
REGISTRY={'active':None,'previous':None,'packages':{}}
SOURCES={'wikipedia_zh':{'enabled':True},'wikidata':{'enabled':True}}
def read(conn,key,default):
    row=conn.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.'+key,)).fetchone()
    return json.loads(row[0]) if row else {'revision':0,'data':copy.deepcopy(default)}
def package_path(paths,record):return paths.home/'data/knowledge/packages'/record['directory']
def validate_used(conn,paths,pid,channel,knowledge):
    if not knowledge:return
    from .knowledge_general import PROFILE,validate_record
    if knowledge.get('mode')==PROFILE:validate_record(conn,pid,channel,knowledge);return
    meta=knowledge.get('processing')
    if meta:
        from .knowledge_processing import record_used
        record_used(conn,paths,pid,channel,meta)
    # Older conversations remain conversations; no retrospective source verdict.
def turn_usable(conn,turn_id):
    row=conn.execute('SELECT t.persona_id,t.channel,c.details_json FROM turns t LEFT JOIN context_uses c ON c.turn_id=t.id WHERE t.id=? ORDER BY c.rowid DESC LIMIT 1',(turn_id,)).fetchone()
    if not row:return False
    try:validate_used(conn,None,row[0],row[1],json.loads(row[2] or '{}').get('knowledge'));return True
    except RuntimeErrorBase:return False
def evidence_permitted(conn,pid,channel,item):return True
def check_batch(conn,paths,batch_id):return None
def should_retrieve(text):return bool(text.strip())

class KnowledgeService:
    def __init__(self,store,retriever_factory=None):
        self.store=store;self.paths=store.paths;self.processing=None;self.maintenance=None;self.closed=threading.Event()
        self.backend=None;self.readers=0;self.lock=threading.RLock()
    def _update(self,key,data,expected_revision,operation_id,*,request=None,apply=None):
        require_text(operation_id,'operation_id');token=fingerprint([key,expected_revision,data if request is None else request])
        with self.store.connection(write=True) as db:
            old=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.operation.'+operation_id,)).fetchone()
            if old:
                saved=json.loads(old[0])
                if saved['hash']!=token:raise RuntimeErrorBase('operation_id_conflict')
                return saved['result']
            current=read(db,key,{})
            if current['revision']!=expected_revision:raise RuntimeErrorBase('version_conflict')
            if apply:apply(db)
            result={'revision':expected_revision+1,'data':data}
            for name,value in (('knowledge.'+key,result),('knowledge.operation.'+operation_id,{'hash':token,'result':result})):
                db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(name,json.dumps(value,ensure_ascii=False)))
            return result
    def _receipt(self,key,expected_revision,operation_id,request):
        with self.store.connection() as db:row=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.operation.'+operation_id,)).fetchone()
        if not row:return None
        value=json.loads(row[0]);token=fingerprint([key,expected_revision,request])
        if value['hash']!=token:raise RuntimeErrorBase('operation_id_conflict')
        return value['result']
    def operation_show(self,operation_id):
        with self.store.connection() as db:row=db.execute('SELECT value FROM knowledge_control WHERE key=?',('knowledge.operation.'+operation_id,)).fetchone()
        return {'state':'completed','result':json.loads(row[0])['result']} if row else {'state':'not_found'}
    def current_policy(self,persona_id):
        from .persona_management import active
        with self.store.connection() as db:active(db,persona_id);return read(db,'policy.'+persona_id,POLICY)
    def update_policy(self,persona_id,data,expected_revision,operation_id):
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store,persona_id);self.current_policy(persona_id)
        if not isinstance(data,dict) or type(data.get('enabled')) is not bool or data.get('channels')!=['desktop']:raise RuntimeErrorBase('knowledge_policy_invalid')
        return self._update('policy.'+persona_id,data,expected_revision,operation_id)
    def sources(self):
        with self.store.connection() as db:return read(db,'sources',SOURCES)
    def update_sources(self,data,expected_revision,operation_id):return self._update('sources',data,expected_revision,operation_id)
    def invalidate(self,data,expected_revision,operation_id):return self._update('invalidations',data,expected_revision,operation_id)
    def status(self):
        return {'state':'stopped' if self.closed.is_set() else 'available','registry':REGISTRY,'sources':self.sources(),
            'model_ready':True,'installation':None,'preparation':{},'network_refresh':{},'p95_target_seconds':None,
            'mode':'conversational_web_v1','authority_gate_enabled':False}
    def prepare_turn(self,turn):
        if self.processing:return self.processing.general.prepare(turn)
        return {'state':'disabled','evidence':[]}
    def recheck(self,db,turn,result):
        from .knowledge_general import PROFILE,validate_record
        if (result or {}).get('mode')==PROFILE:validate_record(db,turn['persona_id'],turn['channel'],result)
        return result or {'state':'disabled','evidence':[]}
    def show_evidence(self,persona_id,turn_id,context_id=None):
        from .knowledge_general import PROFILE,validate_record
        with self.store.connection() as db:
            turn=db.execute('SELECT * FROM turns WHERE id=? AND persona_id=?',(turn_id,persona_id)).fetchone()
            if not turn:raise RuntimeErrorBase('knowledge_turn_not_found')
            row=db.execute("SELECT manifest_json FROM context_manifests WHERE turn_id=? AND task_type='dialogue' ORDER BY rowid DESC LIMIT 1",(turn_id,)).fetchone()
            meta=json.loads(row[0]).get('knowledge',{}) if row else {}
            if meta.get('mode')!=PROFILE:return {'state':'historical','evidence':meta.get('evidence',[])}
            record=validate_record(db,persona_id,turn['channel'],meta);ids=set(meta.get('source_ids',[]))
            return {'state':'ready','evidence':[{'kind':'conversation_source','article_title':s['title'],'url':s['url'],'source_name':s.get('kind'),
                'quote':s.get('content') or s.get('summary') or ''} for s in record['sources'] if s['id'] in ids]}
    def search(self,*args,**kwargs):return {'state':'use_conversation','evidence':[]}
    def prepare_package(self,*args,**kwargs):raise RuntimeErrorBase('legacy_retrieval_frozen')
    activate_package=prepare_package
    def close(self):self.closed.set()
