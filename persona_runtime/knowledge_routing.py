"""Jev decides only whether new web access is needed."""
import hashlib,json
from urllib.parse import urlsplit
from .models import RuntimeErrorBase
from .archive import fingerprint
from .knowledge_web_decision import VERSION,request,execute

def configured_model(paths):
    from .config import ConfigStore
    try:value=json.loads((paths.home/'private/knowledge-router/config.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):raise RuntimeErrorBase('jev_configuration_required') from None
    try:
        parsed=urlsplit(value['base_url'])
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not isinstance(value['name'],str) or not value['name'].strip()
                or not isinstance(value['encrypted_key'],str) or not value['encrypted_key']):raise ValueError()
    except (KeyError,ValueError,TypeError):raise RuntimeErrorBase('jev_configuration_required') from None
    main=ConfigStore(paths).load()['model']
    return {**{k:main[k] for k in ('timeout','max_input_chars')},**{k:value[k] for k in ('base_url','name','encrypted_key')}}

def binding(model):
    return fingerprint({'purpose':VERSION,'base_url':model['base_url'],'name':model['name'],
        'credential_sha256':hashlib.sha256(model['encrypted_key'].encode()).hexdigest(),
        'timeout':model['timeout'],'max_input_chars':model['max_input_chars']})

class KnowledgeRouting:
    def __init__(self,engine):self.engine=engine
    def model(self):return configured_model(self.engine.store.paths)
    def policy(self,pid):
        reader=self.engine.chat.knowledge.current_policy(pid)
        return {'revision':reader['revision'],'data':{'enabled':reader['data']['enabled'],'purpose':'need_web','model_source':'jev','channels':['desktop']}}
    def configuration(self):
        try:
            model=self.model();return {'base_url':model['base_url'],'name':model['name'],'key_configured':True,'purpose':'need_web','retired':False}
        except RuntimeErrorBase as error:return {'name':None,'key_configured':False,'purpose':'need_web','error_code':str(error),'retired':False}
    def grant(self,pid):
        from .private_personas import guard_cloud_dispatch
        model=self.model();guard_cloud_dispatch(self.engine.store,pid,'knowledge_router',model);return model
    def authorization(self,pid):
        try:self.grant(pid);return {'required':True,'authorized':True,'purpose':'need_web'}
        except RuntimeErrorBase as error:return {'required':True,'authorized':False,'purpose':'need_web','error_code':str(error)}
    def authorize(self,persona_id,expected_provider,operation_id):
        from .private_personas import PrivatePersonas
        return PrivatePersonas(self.engine.store.paths).authorize_model(self.engine.store,persona_id,'knowledge_router',expected_provider,operation_id)
    async def execute(self,model,payload):
        body=request(model,payload['knowledge_processing']['input'])
        if getattr(self,'transport',None):return await self.transport(model,body)
        return await execute(model,body)
    def retired(self,*args,**kwargs):raise RuntimeErrorBase('legacy_retrieval_frozen')
    update_policy=retired;prepare=retired;accept=retired
