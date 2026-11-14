"""General public-web preferences and data codecs; no website authority whitelist."""
import copy,hashlib,json
from urllib.parse import urlsplit
from .knowledge_service import read
from .models import RuntimeErrorBase
DEFAULT={'enabled':False,'domains':[],'topics':{},'freshness_seconds':86400,'wikimedia_fallback':False,'discover_public_pages':True}
def public_url(value,domains=None):
    from .knowledge_general import public_url as normalize
    return normalize(value)
def search_result_url(value,domains=None):return public_url(value)
def document_identity(row):
    from .archive import fingerprint
    return fingerprint([row['owner_persona'],row['url'],row['sha256'],row.get('published_at')])
def source_allowed(*args,**kwargs):return True
def document(db,pid,channel,item):
    row=read(db,'web.document.'+item.get('document_id',''),{})['data']
    if not row or row['owner_persona']!=pid:raise RuntimeErrorBase('knowledge_scope_changed')
    return row
def snapshot(db,persona_id=None):
    rows=[json.loads(r[0])['data'] for r in db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.web.document.%'")]
    rows=[r for r in rows if persona_id is None or r['owner_persona']==persona_id]
    return {'version':3 if any(r.get('provider')=='primary_public_html' for r in rows) else 2 if any(r.get('provider')=='wikimedia_api' for r in rows) else 1,'documents':rows,
        'invalidations':{k:v for k,v in read(db,'web.invalidations',{})['data'].items() if k in {r['id'] for r in rows}}}
def validate_snapshot(data):
    if not isinstance(data,dict) or data.get('version') not in (1,2,3) or not isinstance(data.get('documents'),list):raise RuntimeErrorBase('web_snapshot_invalid')
    for row in data['documents']:
        if not isinstance(row.get('body'),str) or hashlib.sha256(row['body'].encode()).hexdigest()!=row.get('sha256'):raise RuntimeErrorBase('web_snapshot_invalid')
        public_url(row['url'])
    return data
def merge_snapshot(db,data,imported_persona=None):
    validate_snapshot(data)
    for row in data['documents']:
        value=copy.deepcopy(row)
        if imported_persona:value.update(owner_persona=imported_persona,publication_origin='imported_snapshot',acquisition_origin='imported_snapshot');value['id']=document_identity(value)
        db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',('knowledge.web.document.'+value['id'],json.dumps({'revision':0,'data':value},ensure_ascii=False)))
class WebKnowledge:
    def __init__(self,engine,transport=None):self.engine=engine;self.store=engine.store;self.transport=transport
    def policy(self,pid):
        with self.store.connection() as db:return read(db,'web.policy.'+pid,DEFAULT)
    def update_policy(self,pid,data,expected_revision,operation_id):
        self.engine.chat.knowledge.current_policy(pid)
        if not isinstance(data,dict) or type(data.get('enabled')) is not bool:raise RuntimeErrorBase('web_policy_invalid')
        return self.engine.chat.knowledge._update('web.policy.'+pid,data,expected_revision,operation_id)
    def invalidate(self,pid,document_id,reason,expected_revision,operation_id):
        with self.store.connection() as db:values=read(db,'web.invalidations',{})['data']
        values[document_id]={'state':'withdrawn','reason':reason}
        return self.engine.chat.knowledge._update('web.invalidations',values,expected_revision,operation_id)
    async def execute(self,model,payload):return await self.engine.processing.general.execute(model,payload)
