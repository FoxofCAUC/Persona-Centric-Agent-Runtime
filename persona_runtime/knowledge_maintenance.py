"""Legacy learning is frozen. Current retrieval caches useful notes directly."""
from .models import RuntimeErrorBase
class KnowledgeMaintenance:
    def __init__(self,engine):self.engine=engine;self.service=engine.chat.knowledge;self.service.maintenance=self
    def defaults(self):return {'enabled':False,'daily_scan':False,'provider':None}
    def model(self,*args):return dict(self.engine.config.load()['model'])
    def policy(self,pid):return {'revision':0,'data':self.defaults()}
    def status(self):return {'state':'retired','automatic_learning':False}
    async def tick(self):return None
    async def drain(self):return None
    def close(self):pass
    def recover(self):pass
    def retired(self,*args,**kwargs):raise RuntimeErrorBase('legacy_retrieval_frozen')
    update_policy=retired;current_source=retired;update_source=retired;submit=retired;withdraw=retired;check=retired;published=retired
