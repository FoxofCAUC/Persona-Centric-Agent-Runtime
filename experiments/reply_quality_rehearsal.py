"""Exercise the no-fallback boundary using the isolated D3 transports."""

from contextvars import ContextVar

from persona_runtime.archive import fingerprint
from persona_runtime.models import RuntimeErrorBase

from .en5_d3_desktop import D3Engine


DECISION_REQUIRED = "reply_decision_required"


class PlannerRequiredEngine(D3Engine):
    """Local protocol rehearsal. The inherited transport check requires test doubles."""

    def __init__(self, store, config, client, *, ledger, flash_client, notes, **kwargs):
        if any(session["branch"] != "candidate" for session in ledger.binding["sessions"].values()):
            raise RuntimeErrorBase(DECISION_REQUIRED)
        super().__init__(store, config, client, ledger=ledger, flash_client=flash_client, notes=notes, **kwargs)
        self.reply_wire = ContextVar("reply_quality_rehearsal_wire", default=None)

    def bind_stage(self, turn, stage, model, messages_or_wire, recheck):
        if stage != "main":
            return
        with self.ledger.db() as db:
            record = db.execute("SELECT reason,choice FROM decisions WHERE turn=?", (turn["id"],)).fetchone()
        if not record or record["reason"] != "selected" or not record["choice"]:
            raise RuntimeErrorBase(DECISION_REQUIRED)
        recheck()
        self.reply_wire.set(fingerprint(messages_or_wire))

    async def first_generation(self, client, model, messages, *, before_attempt, **kwargs):
        if self.reply_wire.get() != fingerprint(messages):
            raise RuntimeErrorBase(DECISION_REQUIRED)
        self.turn_guard.get()()
        try:
            return await super().first_generation(client, model, messages, before_attempt=before_attempt, **kwargs)
        finally:
            self.reply_wire.set(None)
