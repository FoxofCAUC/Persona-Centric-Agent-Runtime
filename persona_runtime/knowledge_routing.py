"""Historical Jev configuration remains readable; no routing veto is executed."""
import json
from .knowledge_service import read
from .models import RuntimeErrorBase
def configured_model(paths):
    from .config import ConfigStore
    value=dict(ConfigStore(paths).load()['model']);file=paths.home/'private/knowledge-router/config.json'
    if file.exists():value.update(json.loads(file.read_text(encoding='utf-8')))
    return value
class KnowledgeRouting:
    def __init__(self,engine):self.engine=engine
    def model(self):return configured_model(self.engine.store.paths)
    def policy(self,pid):return {'revision':0,'data':{'enabled':False,'variant':'web_only','retired':True}}
    def configuration(self):
        model=self.model();return {'base_url':model.get('base_url'),'name':model.get('name'),'key_configured':bool(model.get('encrypted_key')),'gate_passed':False,'retired':True}
    def authorization(self,pid):return {'required':False,'authorized':False,'retired':True}
    def enabled(self,turn):return False
    def retired(self,*args,**kwargs):raise RuntimeErrorBase('legacy_retrieval_frozen')
    update_policy=retired;authorize=retired;prepare=retired;execute=retired;accept=retired;grant=retired
