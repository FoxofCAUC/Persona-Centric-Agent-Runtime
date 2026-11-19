"""Instance-owned, restartable local repair. Only the desktop owner executes actions."""
import asyncio
import hashlib
import json
import math
import time
from uuid import UUID

from .config import ConfigStore, atomic_json
from .instance import InstanceLock, process_identity_alive
from .models import RuntimeErrorBase, identifier

ACTIVE={"executing","verifying","read_only"}
ACTIONS={"restart_service","check_storage"}
PHASES=ACTIVE|{"waiting","resolved","stopped"}
REASONS=ACTIONS|{'healthy','closing','check_result','storage_unavailable','service_unavailable',
                 'cooldown','repair_state_unavailable','external_owner','verify_unavailable'}
BACKOFF=(2,5,15,30,60)
READ_OR_RECEIPT={
    'desktop.timeline.snapshot','desktop.retrieval.notes','desktop.browser.show','desktop.submission.status','chat.snapshot','persona.list','persona.show',
    'chat.notice.presented','chat.notice.release','chat.notice.unknown','expression.presentation.ack',
    'expression.presentation.pending_receipts','expression.v2.ack','expression.v2.pending_receipts','quota.current','quota.settings.current',
    'persona.import.status','persona.import.history','persona.import.history_status','delivery.pending',
}


def protected_request(method,path,action=None):
    if method!='POST' or path.startswith('/v1/control/'):return False
    if path=='/v1/manage':return action not in READ_OR_RECEIPT
    if path.startswith('/v2/replies/') and path.endswith('/result'):return False
    if path.startswith('/v1/requests/') and path.endswith('/delivery'):return False
    return True


class RepairInProgress(RuntimeErrorBase):
    def __init__(self):super().__init__("local_repair_active")


def _file(paths,action):
    return paths.home/"runtime"/("local-repair-"+action+".json")


def read(paths,instance=None,action=None):
    if action is None:
        records=[v for a in ACTIONS if (v:=read(paths,instance,a))]
        priority={'executing':0,'verifying':0,'read_only':1,'waiting':2,'stopped':3,'resolved':4}
        return min(records,key=lambda v:(priority[v['phase']],-v.get('updated',0)),default=None)
    file=_file(paths,action)
    try:
        value=json.loads(file.read_text(encoding="utf-8"))
        if value['instance']!=(instance or ConfigStore(paths).load()['instance_id']):return None
        if (value['version']!=1 or value['phase'] not in PHASES or value['action']!=action
                or str(UUID(value['id']))!=value['id'] or type(value['attempts']) is not int
                or not 0<=value['attempts']<=5 or value['reason'] not in REASONS
                or not isinstance(value['owner'],dict) or not isinstance(value['console'],str)
                or any(type(value[k]) not in (int,float) or not math.isfinite(value[k]) for k in ('next_epoch','updated','window_epoch'))):
            raise ValueError()
        return value
    except FileNotFoundError:return None
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):
        return {"phase":"read_only","action":action,"reason":"repair_state_unavailable"}


def public(value):
    if not value:return None
    result={key:value[key] for key in ('id','phase','action','reason','attempts','updated','next_epoch') if key in value}
    if result['phase'] in ('executing','verifying') and time.time()>value.get('updated',0)+75:
        result.update(phase='read_only',reason='verify_unavailable')
    return result


def require_open(paths,instance=None):
    from .maintenance import storage_fault_pending
    value=read(paths,instance)
    if (value and value['phase'] in ACTIVE) or storage_fault_pending(paths):raise RepairInProgress()


def require_dispatch(paths):
    from .maintenance import require_writable_storage
    from .lifecycle import closing_console
    if closing_console(paths):raise RepairInProgress()
    require_open(paths)
    try:require_writable_storage(paths)
    except RuntimeErrorBase as error:
        if str(error) in ('storage_write_fault','storage_low_pause_dispatch'):raise RepairInProgress() from None
        raise


async def wait_until_open(store):
    from .call_limits import check_draining
    while True:
        check_draining(store)
        try:require_dispatch(store.paths);return
        except RepairInProgress:await asyncio.sleep(.4)


class LocalRepair:
    def __init__(self,controller,clock=time.time):
        self.controller=controller
        self.paths=controller.paths
        self.clock=clock
        self.last=None
        self.on_recovered=None

    def status(self):
        if self.last and self.last.get('reason')=='repair_state_unavailable':return public(self.last)
        return public(read(self.paths))

    def outstanding(self,action):
        if self.last and self.last.get('action')==action and self.last.get('reason')=='repair_state_unavailable':
            return self.last
        return read(self.paths,action=action)

    def _save(self,record,phase,reason,**fields):
        record.update(phase=phase,reason=reason,updated=self.clock(),**fields)
        try:atomic_json(_file(self.paths,record['action']),record,durable=True)
        except OSError:
            self.last=dict(record,phase='read_only',reason='repair_state_unavailable')
            self._notify()
            raise
        self.last=dict(record)
        self._notify()
        if phase=='resolved' and self.on_recovered:
            try:self.on_recovered()
            except Exception:pass

    def _notify(self):
        notify=getattr(self.controller,'repair_notice',None)
        if notify:
            try:notify(self.status())
            except Exception:pass

    def external_service(self,stopping):
        """Record foreign ownership without sending a request to that listener."""
        from .lifecycle import require_console
        if stopping():return
        old=read(self.paths,action='restart_service')
        if old and old.get('reason') in ('external_owner','repair_state_unavailable'):return
        try:lock=InstanceLock(self.paths.home/'runtime/local-repair.lock')
        except RuntimeErrorBase:return
        try:
            if stopping():return
            lease=require_console(self.paths,self.controller.console_lease['token'])
            old=read(self.paths,lease['instance_id'],'restart_service')
            if old and old.get('reason')=='repair_state_unavailable':return
            record=old if old and old['phase']!='resolved' else {'version':1,'id':identifier(),'action':'restart_service',
                    'attempts':0,'window_epoch':self.clock(),'instance':lease['instance_id']}
            record.update(console=lease['token'],owner=lease['process'])
            self._save(record,'waiting','external_owner',next_epoch=self.clock()+2)
        finally:lock.close()

    def run(self,action,verify,perform,stopping,*,admit=None):
        """Revalidate the actual condition before repeating an interrupted action."""
        if action not in ACTIONS:raise RuntimeErrorBase("unsupported_repair")
        from .lifecycle import require_console
        try:lock=InstanceLock(self.paths.home/"runtime/local-repair.lock")
        except RuntimeErrorBase:return False
        try:
            if stopping():return False
            lease=require_console(self.paths,self.controller.console_lease['token'])
            instance=lease['instance_id']
            record=read(self.paths,instance,action)
            if not record and self.last and self.last.get('action')==action and self.last.get('instance')==instance and self.last.get('reason')=='repair_state_unavailable':
                record=dict(self.last)
            if record and 'id' not in record:
                # The OS lock excludes a live executor. Keep the unreadable receipt
                # and assume the whole action budget was spent until its window expires.
                from .character_embedding import safe_path
                file=safe_path(self.paths.home,_file(self.paths,action))
                try:
                    raw=file.read_bytes()
                    saved=safe_path(self.paths.home,file.with_name(file.stem+'-unverified-'+hashlib.sha256(raw).hexdigest()+'.json'))
                    if not saved.exists():
                        with saved.open('xb') as out:out.write(raw)
                except OSError:
                    self.last=record;return False
                record={'version':1,'id':identifier(),'instance':instance,'action':action,'attempts':5,
                        'window_epoch':self.clock(),'next_epoch':0,'prior_receipt_unverified':True,'phase':'waiting'}
            if record and record['phase'] in ACTIVE and record['console']!=lease['token'] and process_identity_alive(record.get('owner')):
                return False
            if not record or record['phase']=='resolved':
                record={'version':1,'id':identifier(),'instance':instance,'action':action,
                        'attempts':0,'window_epoch':self.clock(),'next_epoch':0}
            record.update(console=lease['token'],owner=lease['process'])
            try:healthy=verify()
            except Exception as error:
                reason='external_owner' if str(error)=='component_owner_conflict' else 'verify_unavailable'
                self._save(record,'read_only' if action=='check_storage' else 'waiting',reason,next_epoch=self.clock()+5)
                return False
            if healthy:
                require_console(self.paths,lease['token'])
                self._save(record,'resolved','healthy',next_epoch=0)
                return True
            now=self.clock()
            if now<record.get('next_epoch',0) and record.get('phase') in ('waiting','read_only'):return False
            if now-record['window_epoch']>=1800:record.update(window_epoch=now,attempts=0)
            if record['attempts']>=5:
                self._save(record,'read_only' if action=='check_storage' else 'waiting','cooldown',next_epoch=record['window_epoch']+1800)
                return False
            if admit and not admit():
                self._save(record,'waiting','cooldown',next_epoch=self.clock()+2)
                return False
            record['attempts']+=1
            self._save(record,'executing',action,next_epoch=0)
            if stopping():self._save(record,'stopped','closing');return False
            require_console(self.paths,lease['token'])
            try:
                perform()
                if stopping():self._save(record,'stopped','closing');return False
                require_console(self.paths,lease['token'])
                self._save(record,'verifying','check_result')
                if verify():
                    self._save(record,'resolved','healthy')
                    return True
            except Exception as error:
                if stopping():self._save(record,'stopped','closing');return False
                if str(error)=='component_owner_conflict':
                    self._save(record,'waiting','external_owner',next_epoch=self.clock()+5);return False
            self._save(record,'read_only' if action=='check_storage' else 'waiting',
                       'storage_unavailable' if action=='check_storage' else 'service_unavailable',
                       next_epoch=self.clock()+BACKOFF[min(record['attempts']-1,4)])
            return False
        finally:lock.close()
