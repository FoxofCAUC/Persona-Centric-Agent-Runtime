"""Check that an unresolved D3 decision cannot fall through to reply generation."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from experiments.en5_d3_desktop import DesktopLedger
from experiments.en5_decision_tool import MODE
from experiments.reply_quality_rehearsal import PlannerRequiredEngine
from persona_runtime.archive import fingerprint
from persona_runtime.character import CharacterService
from persona_runtime.config import ConfigStore
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import snapshot
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from tests.test_en5_d3_desktop import LocalDecision, LocalMain


class PlannerRequired(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        paths = RuntimePaths(Path(temporary.name))
        config = ConfigStore(paths)
        config.initialize()
        config.update({"model": {"base_url": "https://api.deepseek.com", "name": "deepseek-v4-pro"}})
        store = Store(paths)
        store.initialize()
        service = RuntimeService(store)
        self.pid = service.create_persona(generic_persona())["id"]
        service.use_persona(self.pid)
        with store.connection(write=True) as db:
            self.sid = select(db, self.pid, "desktop", None)
            profile = fingerprint(snapshot(db, self.pid))
        self.input = "U1"
        binding = {
            "version": "en5-d3-local-v1", "mode": "local_fixture", "home": str(paths.home.resolve()),
            "instance_id": config.load()["instance_id"], "persona_id": self.pid, "profile_hash": profile,
            "character_version": CharacterService(store).current(self.pid)["id"], "decision_mode": MODE,
            "sessions": {self.sid: {"branch": "candidate", "display": "bubbles", "inputs": [[fingerprint(self.input)]]}},
        }
        self.planner = LocalDecision()
        self.replyer = LocalMain("R1")
        self.engine = PlannerRequiredEngine(store, config, self.replyer, ledger=DesktopLedger(paths, binding),
                                           flash_client=self.planner, notes={"flash": "F-ID", "pro": "P-ID"})
        self.addAsyncCleanup(self.engine.wait)

    async def test_valid_decision_reaches_replyer_once(self):
        turn = await self.engine.submit("desktop", self.input, identifier(), session_id=self.sid)
        await self.engine.wait()
        self.assertEqual(self.engine.get(turn["id"])["reply"], "R1")
        self.assertEqual(len(self.planner.calls), 1)
        self.assertEqual(len(self.replyer.calls), 1)

    async def test_invalid_decision_blocks_replyer(self):
        self.planner.invalid = True
        turn = await self.engine.submit("desktop", self.input, identifier(), session_id=self.sid)
        await self.engine.wait()
        self.assertEqual(self.engine.get(turn["id"])["error_code"], "reply_decision_required")
        self.assertEqual(len(self.planner.calls), 1)
        self.assertEqual(self.replyer.calls, [])

    async def test_expired_decision_blocks_replyer(self):
        with patch("experiments.en5_decision_contract.DEADLINE_SECONDS", 0):
            turn = await self.engine.submit("desktop", self.input, identifier(), session_id=self.sid)
            await self.engine.wait()
        self.assertEqual(self.engine.get(turn["id"])["error_code"], "reply_decision_required")
        self.assertEqual(self.replyer.calls, [])

    async def test_new_desktop_reply_keeps_original_and_records_bounded_bubbles(self):
        from persona_runtime.workbench_timeline import turn_card

        self.replyer.reply = "R" * 226
        turn = await self.engine.submit("desktop", self.input, identifier(), session_id=self.sid)
        await self.engine.wait()
        view = self.engine.get(turn["id"])
        self.assertEqual(view["reply"], self.replyer.reply)
        self.assertEqual([len(part) for part in view["display_bubbles"]], [75, 75, 75, 1])
        self.assertEqual("".join(view["display_bubbles"]), view["display_reply"])
        with self.engine.store.connection() as db:
            row = db.execute("SELECT *,rowid AS position FROM turns WHERE id=?", (turn["id"],)).fetchone()
            self.assertEqual(turn_card(db, row)["display_bubbles"], view["display_bubbles"])
