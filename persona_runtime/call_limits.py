"""Local admission for physical model calls and bounded workflow exceptions."""
import asyncio
from contextvars import ContextVar
from functools import wraps
import time
import json
from weakref import WeakKeyDictionary

from .models import RuntimeErrorBase, identifier

flow = ContextVar("pcar_call_workflow", default=None)
_stopping=WeakKeyDictionary()


class QuotaShutdown(asyncio.CancelledError):
    pass


def draining(store,enabled):
    homes=_stopping.setdefault(asyncio.get_running_loop(),set())
    if enabled:homes.add(store.paths.home)
    else:homes.discard(store.paths.home)


def check_draining(store):
    if store.paths.home in _stopping.get(asyncio.get_running_loop(),()):
        raise QuotaShutdown()


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS model_call_meter (
        id INTEGER PRIMARY KEY, started REAL NOT NULL, category TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS model_call_meter_time ON model_call_meter(started)")
    db.execute("""CREATE TABLE IF NOT EXISTS model_call_waits (
        scope TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, reason TEXT NOT NULL, next_epoch REAL NOT NULL,
        state TEXT NOT NULL, created REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS model_call_wait_owners(scope TEXT PRIMARY KEY
        REFERENCES model_call_waits(scope) ON DELETE CASCADE, identity TEXT NOT NULL)""")


def turn_scope(turn):
    return "turn/"+turn["id"]+"/"+str(turn["execution_revision"])


class CallLimitPending(RuntimeErrorBase):
    def __init__(self,scope,reason,until):
        super().__init__("model_call_limit")
        self.scope,self.reason,self.until=scope,reason,until


def granted(db,scope):
    return bool(db.execute("SELECT 1 FROM model_call_waits WHERE scope=? AND state='granted'",(scope,)).fetchone())


def park(db,scope,reason,until):
    db.execute("""INSERT INTO model_call_waits VALUES(?,?,?,?,'waiting',?)
        ON CONFLICT(scope) DO UPDATE SET reason=excluded.reason,next_epoch=excluded.next_epoch
        WHERE model_call_waits.state!='granted'""",(scope,identifier(),reason,until,time.time()))
    if scope.startswith('job/'):
        from .instance import current_process_identity
        db.execute("INSERT OR IGNORE INTO model_call_wait_owners VALUES(?,?)",(scope,json.dumps(current_process_identity())))


def recover(store):
    from .instance import process_identity_alive
    with store.connection(write=True) as db:
        for scope,owner in db.execute("SELECT scope,identity FROM model_call_wait_owners").fetchall():
            if not process_identity_alive(json.loads(owner)):finish(db,scope)


def capacity(db,config,scope,now):
    cfg=config.load()
    policy=cfg['call_limits']
    if not policy['enabled'] or granted(db,scope):return None
    override=config.quota_override()
    if override.get('status')=='active' and override.get('instance_id',cfg['instance_id'])==cfg['instance_id']:
        return None
    for key,seconds in (('hourly',3600),('daily',86400)):
        cap=policy[key]
        if cap:
            # The oldest of the most recent cap calls is the next free position,
            # including after a user lowers the limit below current usage.
            row=db.execute("SELECT started FROM model_call_meter WHERE started>? ORDER BY started DESC LIMIT 1 OFFSET ?",
                           (now-seconds,cap-1)).fetchone()
            if row:return key,row[0]+seconds+.01
    return None


def take(db,config,scope,category,now=None):
    from .local_repair import require_dispatch
    require_dispatch(config.paths)
    now=time.time() if now is None else now
    reason=capacity(db,config,scope,now)
    if reason:raise CallLimitPending(scope,*reason)
    return record_call(db,scope,category,now)


def record_call(db,scope,category,now):
    entry=db.execute("INSERT INTO model_call_meter(started,category) VALUES(?,?)",(now,category)).lastrowid
    db.execute("DELETE FROM model_call_waits WHERE scope=? AND state!='granted'",(scope,))
    db.execute("DELETE FROM model_call_meter WHERE started<?",(now-86400,))
    return entry,now


async def acquire(store,scope,category,*,check=None):
    from .config import ConfigStore
    from .local_repair import RepairInProgress,wait_until_open
    config=ConfigStore(store.paths)
    if not config.file.exists():config.initialize()
    while True:
        check_draining(store)
        try:
            with store.connection(write=True) as db:
                if check:check(db)
                take(db,config,scope,category)
                return
        except CallLimitPending as wait:
            await wait_for_budget(store,wait)
        except RepairInProgress:
            await wait_until_open(store)


def pending(db):
    rows=db.execute("""SELECT id,reason,state,next_epoch FROM model_call_waits
        WHERE state IN ('waiting','deferred') AND
        (scope NOT LIKE 'turn/%' OR EXISTS (SELECT 1 FROM turns WHERE
         id=substr(scope,6,instr(substr(scope,6),'/')-1) AND scope='turn/'||turns.id||'/'||execution_revision
         AND status IN ('queued','running') AND cancelled_at IS NULL))
        AND (scope NOT LIKE 'organization/%' OR EXISTS (SELECT 1 FROM organization_tasks WHERE id=substr(scope,14)
        AND status IN ('queued','running','waiting_quota')))
        AND (scope NOT LIKE 'memory/%' OR EXISTS (SELECT 1 FROM memory_batches WHERE id=substr(scope,8)
        AND state IN ('extracting','waiting','verifying'))) ORDER BY created""")
    return [dict(r) for r in rows]


def decide(store,ids,allow):
    if type(allow) is not bool or not isinstance(ids,list) or not ids or not all(isinstance(i,str) for i in ids):
        raise RuntimeErrorBase("invalid_quota_decision")
    with store.connection(write=True) as db:
        db.executemany("""UPDATE model_call_waits SET state=? WHERE id=? AND state IN ('waiting','deferred')""",
                       [('granted' if allow else 'deferred',i) for i in ids])
    return True


def finish(db,scope):
    db.execute("DELETE FROM model_call_waits WHERE scope=?",(scope,))


def gate(db,store,category,fallback):
    from .config import ConfigStore
    config=ConfigStore(store.paths)
    if not config.file.exists():config.initialize()
    return take(db,config,flow.get() or fallback,category)


async def wait_for_budget(store,wait):
    from .config import ConfigStore
    config=ConfigStore(store.paths)
    with store.connection(write=True) as db:
        park(db,wait.scope,wait.reason,wait.until)
    try:
        while True:
            check_draining(store)
            with store.connection() as db:
                if granted(db,wait.scope):return
                if wait.reason in ('hourly','daily'):
                    if not capacity(db,config,wait.scope,time.time()):return
                else:
                    if time.time()>=wait.until:return
            await asyncio.sleep(.5)
    except QuotaShutdown:raise
    except asyncio.CancelledError:
        with store.connection(write=True) as db:finish(db,wait.scope)
        raise


def resume_on_limit(method):
    from .local_repair import RepairInProgress,wait_until_open
    @wraps(method)
    async def resume(self,*args,**kwargs):
        while True:
            try:return await method(self,*args,**kwargs)
            except CallLimitPending as wait:
                await wait_for_budget(owner_store(self),wait)
            except RepairInProgress:
                await wait_until_open(owner_store(self))
    return resume


def owner_store(owner):
    from .storage import Store
    from .paths import RuntimePaths
    store=getattr(owner,'store',None)
    if isinstance(store,Store):return store
    paths=getattr(getattr(owner,'config',None),'paths',None) or getattr(owner,'paths',None) or RuntimePaths.from_environment()
    store=Store(paths)
    ready=False
    if store.path.is_file():
        with store.connection() as db:
            ready=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='model_call_wait_owners'").fetchone())
    if not ready:store.initialize()
    return store


def external_gate(owner,category,fallback):
    store=owner_store(owner)
    with store.connection(write=True) as db:gate(db,store,category,fallback)


def workflow(method):
    @wraps(method)
    async def run(self,*args,**kwargs):
        if flow.get():return await method(self,*args,**kwargs)
        scope='job/'+identifier()
        token=flow.set(scope)
        try:return await method(self,*args,**kwargs)
        finally:
            flow.reset(token)
            with owner_store(self).connection(write=True) as db:finish(db,scope)
    return run
