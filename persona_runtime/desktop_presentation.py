"""Local scheduling dependencies for replies that are waiting for the desktop."""
import asyncio

from .models import RuntimeErrorBase


class PresentationWakeup:
    def __init__(self):
        self.event = asyncio.Event()
        self.loop = None

    async def wait(self):
        self.loop = asyncio.get_running_loop()
        event = self.event
        try:
            await asyncio.wait_for(event.wait(),timeout=1)
        except asyncio.TimeoutError:
            pass

    def notify(self):
        def signal():
            self.event.set()
            self.event = asyncio.Event()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(signal)


def clear_wait(db, turn, dependency):
    db.execute("""DELETE FROM knowledge_control WHERE key=? AND json_extract(value,'$.revision')=?
                  AND json_extract(value,'$.dependency')=?""",
               ("desktop.wait."+turn["id"],turn["execution_revision"],dependency))


class PresentationPending(RuntimeErrorBase):
    def __init__(self, turn_id, task_id):
        super().__init__("desktop_presentation_pending")
        self.turn_id = turn_id
        self.task_id = task_id


def pending_speech(db, task_id):
    return db.execute("""SELECT u.*,p.state AS publication_state FROM expression_speech_units u
        JOIN expression_speech_publications p ON p.utterance_id=u.id
        WHERE u.task_id=? AND u.phase='search_notice'""", (task_id,)).fetchone()


async def wait_for_notice(engine, turn, task_id):
    from . import tk_notice
    from .migration import migration_pending
    next_validation = 0
    while not engine.knowledge.closed.is_set() and not engine.draining:
        if migration_pending(engine.store.paths):
            raise RuntimeErrorBase("migration_configuration_required")
        with engine.store.connection() as db:
            row = db.execute("SELECT * FROM turns WHERE id=?", (turn["id"],)).fetchone()
            if not row or row["execution_revision"] != turn["execution_revision"]:
                raise RuntimeErrorBase("knowledge_request_inactive")
            current = dict(row)
            tk_notice.check_root(db,current)
            stored = db.execute("SELECT status FROM organization_tasks WHERE id=?", (task_id,)).fetchone()
            if not stored or stored["status"] not in ("queued","running","completed","waiting_quota"):
                raise RuntimeErrorBase("search_notice_inactive")
            if tk_notice.already_presented(db, turn["id"]) and stored["status"] == "completed":
                return
            unit = pending_speech(db,task_id)
            validate_now = asyncio.get_running_loop().time() >= next_validation
            if unit:
                if unit["cancelled_at"] or (unit["closed_at"] and unit["publication_state"] != "complete"):
                    raise RuntimeErrorBase("search_notice_not_fully_presented")
                if not unit["closed_at"] and validate_now:
                    engine.expression.outlet.validate(db,dict(unit))
            elif validate_now:
                tk_notice.validate_sources(db,engine.store,current,task_id)
            if validate_now:
                next_validation = asyncio.get_running_loop().time()+2
        await engine.presentation_wakeup.wait()
    raise RuntimeErrorBase("knowledge_stopped")
