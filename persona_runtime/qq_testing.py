"""Local QQ acceptance identity policy. Account identifiers never belong in Git.

Switch only while all services are stopped; persist recovery before config writes.
Ordinary operation keeps the daily account. Tests use a separate peer and persona.
"""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from .config import ConfigStore, atomic_json
from .instance import InstanceLock
from .models import RuntimeErrorBase
from .paths import RuntimePaths


class QQTesting:
    def __init__(self, paths):
        self.paths=paths
        self.file=paths.home/'private/qq-testing.json'

    def load(self, required=False):
        if not self.file.exists():
            if required:raise RuntimeErrorBase('qq_test_policy_missing')
            return None
        from .private_personas import safe
        safe(self.file,self.paths.home)
        try:
            value=json.loads(self.file.read_text(encoding='utf-8'))
            assert value['version']==1 and value['state'] in ('inactive','entering','active','restoring')
            assert all(isinstance(value[k],str) and value[k].isascii() and value[k].isdigit()
                       for k in ('daily_peer','test_peer'))
            assert value['daily_peer']!=value['test_peer']
        except (OSError,ValueError,KeyError,TypeError,AssertionError):
            raise RuntimeErrorBase('qq_test_policy_invalid') from None
        return value

    def configure(self, daily_peer, test_peer):
        if self.load() is not None:raise RuntimeErrorBase('qq_test_policy_already_configured')
        from .private_personas import safe
        safe(self.file,self.paths.home)
        value={'version':1,'state':'inactive','daily_peer':daily_peer,'test_peer':test_peer}
        if any(not isinstance(v,str) or not v.isascii() or not v.isdigit() for v in (daily_peer,test_peer)) or daily_peer==test_peer:
            raise RuntimeErrorBase('qq_test_policy_invalid')
        atomic_json(self.file,value)
        return {'configured':True,'active':False}

    def active(self):
        value=self.load()
        return value is not None and value['state']=='active'

    def require_ready(self, peer=None):
        value=self.load(True);cfg=ConfigStore(self.paths).load()
        if value['state']!='active' or cfg['owner_qq']!=value['test_peer']:
            raise RuntimeErrorBase('qq_test_account_not_ready')
        if peer is not None and str(peer)!=value['test_peer']:
            raise RuntimeErrorBase('qq_test_account_forbidden')
        return value

    def check_runtime(self, peer):
        value=self.load()
        journal=self.paths.home/'runtime/acceptance-quotas.json'
        try:acceptance=journal.exists() and json.loads(journal.read_text(encoding='utf-8')).get('status')=='active'
        except (OSError,ValueError):raise RuntimeErrorBase('acceptance_state_unavailable') from None
        if value is None:
            if acceptance:raise RuntimeErrorBase('qq_test_policy_missing')
            return
        if value['state']!='inactive' or acceptance:self.require_ready(peer)

    def _stopped(self, stack):
        from .lifecycle import ServiceController
        for name in ('desktop','console','service'):
            lock=InstanceLock(self.paths.home/'runtime'/f'{name}.lock');stack.callback(lock.close)
        controller=ServiceController(self.paths)
        if any(controller.health(role)['state']!='已停止' for role in controller.adapters):
            raise RuntimeErrorBase('qq_test_stop_services_first')

    def _idle(self, store):
        with store.connection() as db:
            for table,query in (
                ('turns',"status IN ('queued','running')"),
                ('input_batches',"state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')"),
                ('reply_delivery_parts',"state IN ('pending','sending','failed','uncertain')")):
                if db.execute('SELECT count(*) FROM '+table+' WHERE '+query).fetchone()[0]:
                    raise RuntimeErrorBase('qq_test_unresolved_work')

    def enter(self, persona_id):
        from .private_personas import PrivatePersonas
        from .service import RuntimeService
        from .storage import Store
        with ExitStack() as stack:
            self._stopped(stack);store=Store(self.paths);self._idle(store)
            private=PrivatePersonas(self.paths);private.assert_acceptance(persona_id)
            service=RuntimeService(store);service.show_persona(persona_id)
            value=self.load(True);cfg=ConfigStore(self.paths)
            if value['state']=='active':
                self.require_ready()
                if service.show_persona()['id']!=persona_id:raise RuntimeErrorBase('qq_test_persona_conflict')
                return {'active':True,'already_applied':True}
            if value['state']=='restoring':raise RuntimeErrorBase('qq_test_restore_required')
            if value['state']=='inactive':
                if cfg.load()['owner_qq']!=value['daily_peer']:raise RuntimeErrorBase('qq_test_daily_account_changed')
                value.update(state='entering',restore_persona_id=service.show_persona()['id'],test_persona_id=persona_id)
                atomic_json(self.file,value)
            elif value['test_persona_id']!=persona_id:raise RuntimeErrorBase('qq_test_persona_conflict')
            cfg.update({'owner_qq':value['test_peer']});service.use_persona(persona_id)
            value['state']='active';atomic_json(self.file,value)
            return {'active':True,'services_started':False}

    def restore(self):
        from .service import RuntimeService
        from .storage import Store
        from .quotas import QuotaService
        with ExitStack() as stack:
            self._stopped(stack);store=Store(self.paths);self._idle(store)
            value=self.load(True)
            if value['state']=='inactive':return {'active':False,'already_restored':True}
            value['state']='restoring';atomic_json(self.file,value)
            cfg=ConfigStore(self.paths);QuotaService(store,cfg).end_acceptance()
            cfg.update({'owner_qq':value['daily_peer']})
            RuntimeService(store).use_persona(value['restore_persona_id'])
            value['state']='inactive';atomic_json(self.file,value)
            return {'active':False,'daily_account_restored':True,'persona_restored':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','enter','restore'));parser.add_argument('--persona-id')
    args=parser.parse_args();policy=QQTesting(RuntimePaths.from_environment())
    if args.action=='enter':
        if not args.persona_id:parser.error('--persona-id required')
        result=policy.enter(args.persona_id)
    elif args.action=='restore':result=policy.restore()
    else:
        value=policy.load(True);result={'configured':True,'state':value['state'],
            'configured_peer_is_test':ConfigStore(policy.paths).load()['owner_qq']==value['test_peer']}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
