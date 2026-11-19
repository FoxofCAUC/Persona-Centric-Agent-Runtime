"""Exercise one logical dual-card composition with local, opaque records."""
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from aiohttp.payload import JsonPayload

from experiments.import_card_request_review import export_review
from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_learning_transport import CorpusCallFailure
from persona_runtime.import_analysis_v2 import VERSION as PROFILE_VERSION
from persona_runtime.import_card_job import CardJobs, VERSION
from persona_runtime.import_library import ImportLibrary, encode
from persona_runtime.import_profile_workspace import blank_drafts
from persona_runtime.models import timestamp
from persona_runtime.paths import RuntimePaths


class Double:
    def __init__(self):
        self.sent = []
        self.mode = "success"
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()

    async def complete(self, model, wire, *, save_raw):
        self.sent.append(deepcopy(wire))
        self.entered.set()
        await self.gate.wait()
        if self.mode in ("unknown","unsent"):
            raise CorpusCallFailure("001",{"request_headers_sent":self.mode=="unknown"})
        one = {"common":{"value":"301","sources":["A1"]}}
        result = {"A":one, "B":{"common":{"value":"302","sources":["B1"]}}}
        if self.mode=="partial":
            result["B"]["common"]["sources"] = ["A1"]
        if self.mode=="conflict":
            result["A"] = {"scenes":{"help":"401","daily":"405"},"scenes.help":"402"}

        raw = {"choices":[{"finish_reason":"stop","message":{"content":encode(result)}}],
               "usage":{"total_tokens":21}}
        if self.mode=="invalid":
            raw["choices"][0]["message"]["content"] = " "
        save_raw(encode(raw).encode(),http_status=200,diagnostics={})


class CardJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = RuntimePaths(self.root/"instance")
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model":{"name":"deepseek-flash","base_url":"https://api.deepseek.com","api_key":"123"}})
        self.lib = ImportLibrary(self.paths)
        status = self.lib.add(b"## X\n101\n## Y\n201\n")
        self.ident = status["id"]
        self.rev = status["revision"]
        drafts = blank_drafts({"A":"X","B":"Y"})
        for role in ("A","B"):
            drafts[role]["claims"] = [{"id":role+"9","kind":"pattern","scope":"111","field":"expression_style",
                "value":"221","decision":"pending","evidence":[{"id":"1","start":0,"end":1,"quote":"1","speaker":role}]}]
        self.plan = {"source_id":"11","roles":{"A":{"stats":{}},"B":{"stats":{}}}}
        self.pid = fingerprint([PROFILE_VERSION,self.ident,self.plan])
        self.plan["id"] = self.pid
        with self.lib.db() as db:
            for role in ("A", "B"):
                c = drafts[role]["claims"][0]
                c.update(role=role,batch=role+"7", needs_review=False)
                db.execute("INSERT INTO profile_results VALUES(?,?,?,?,?,?,?)",
                          (self.pid,c["batch"],role,"extract","accepted",encode([c]),"[]"))
            db.execute("INSERT INTO profile_plans VALUES(?,?,?,?,?)",
                       (self.pid,self.ident,encode(self.plan),"{}",timestamp()))
            db.execute("INSERT INTO profile_current VALUES(?,?)",(self.ident,self.pid))
            db.execute("UPDATE documents SET status='ready',draft=? WHERE id=?",(encode(drafts),self.ident))
        self.template={"version":VERSION,"model":"deepseek-flash","system":"100 json"}
        (self.lib.root/"contract-card.json").write_text(encode(self.template),encoding="utf8")
        self.double=Double()
        self.jobs=CardJobs(self.lib,self.config,self.double)

    def approve(self, **override):
        desc=self.jobs.preview(self.ident,self.rev,self.pid)
        base={k:desc[k] for k in ("document","plan_id","source_revision","source_sha256","contract_sha256","model","base_url")}
        grant=base|{"decision":"approved","purposes":["compose"],"request_hashes":[desc["wire_hash"]]}|override
        (self.lib.root/"approval-card.json").write_text(encode(grant),encoding="utf8")
        return desc

    async def dispatch(self, revision=None):
        self.jobs.start(self.ident, self.rev if revision is None else revision, self.pid, True)
        task=self.jobs.tasks.get(self.ident)
        if task:
            await task
        return self.jobs.status(self.ident)

    def test_sparse_finished_pair_can_be_edited_and_materialized_without_dispatch(self):
        with self.lib.db() as db:
            drafts = json.loads(db.execute("SELECT draft FROM documents WHERE id=?", (self.ident,)).fetchone()[0])
            drafts["B"]["claims"] = []
            db.execute("UPDATE documents SET draft=? WHERE id=?", (encode(drafts), self.ident))
            db.execute("UPDATE profile_results SET accepted='[]' WHERE plan=? AND role='B'", (self.pid,))
        data = self.jobs.status(self.ident)
        self.assertEqual(data["state"], "manual_only")
        self.assertEqual(data["cards"]["A"]["spec"]["expression_style"], "221")
        b = deepcopy(data["cards"]["B"])
        b["spec"]["introduction"] = "222"
        edited = self.jobs.edit(self.ident,self.rev,0,self.pid,"B",b)
        self.assertEqual(edited["cards"]["B"]["spec"]["introduction"], "222")
        self.assertEqual(self.jobs.material(self.ident,self.rev,self.pid,edited["revision"]), edited["cards"])
        with self.assertRaises(ValueError):
            self.jobs.start(self.ident,self.rev,self.pid,True)
        self.assertEqual(self.double.sent, [])

    def test_private_review_reproduces_wire_without_job_or_api(self):
        original = (self.lib.root/"imports.sqlite3").read_bytes()
        desc=self.jobs.preview(self.ident,self.rev,self.pid)
        folder=self.root/"review"
        manifest=export_review(self.paths.home,folder)
        self.assertEqual(manifest["wire_hash"],desc["wire_hash"])
        self.assertEqual((folder/"request.json").read_bytes(),JsonPayload(desc["wire"])._value)
        self.assertEqual(digest((folder/"request.json").read_bytes()),manifest["body_sha256"])
        self.assertIn("100 json",(folder/"review.html").read_text(encoding="utf8"))
        self.assertEqual(original,(self.lib.root/"imports.sqlite3").read_bytes())
        self.assertEqual(self.double.sent,[])

    async def test_exact_approval_once_and_reopen(self):
        self.approve(plan_id="0")
        self.assertEqual(self.jobs.start(self.ident,self.rev,self.pid,True)["state"],"awaiting_approval")
        desc=self.approve()
        self.double.gate.clear()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        self.jobs.start(self.ident,self.rev,self.pid,True)
        await self.double.entered.wait()
        self.assertEqual(self.jobs.status(self.ident)["state"],"sending")
        self.double.gate.set()
        await self.jobs.tasks[self.ident]
        self.assertEqual(self.double.sent,[desc["wire"]])
        self.jobs=CardJobs(self.lib,self.config,self.double)
        data=await self.dispatch()
        self.assertEqual(data["state"],"ready")
        self.assertEqual([data["cards"][role]["common"] for role in ("A","B")],["301","302"])
        self.assertEqual(len(self.double.sent),1)
        with self.lib.db() as db:
            self.assertEqual(self.jobs.material(self.ident,self.rev,self.pid,data["revision"],db=db),data["cards"])
            call,=db.execute("SELECT * FROM calls")
            self.assertEqual(json.loads(call["metadata"])["usage"],{"total_tokens":21})

    async def test_result_review_replays_one_first_response_and_uses_saved_sources(self):
        from experiments.import_card_result_review import export_result
        self.approve()
        status = await self.dispatch()
        self.assertEqual(status["state"], "ready")
        output = self.lib.root.parent / "result-proof"
        audit = export_result(self.lib.root.parent.parent, output, document=self.ident)
        self.assertEqual(audit["outcome"], "ready")
        self.assertEqual(audit["candidate_sha256"], fingerprint(status["cards"]))
        self.assertEqual(digest((output/"first-response.json").read_bytes()), audit["response_sha256"])
        self.assertEqual(len(self.double.sent), 1)

    async def test_invalid_response_preserves_first_and_manual_draft(self):
        self.approve()
        self.double.mode="invalid"
        state=await self.dispatch()
        self.assertEqual(state["state"],"failed")
        card=state["cards"]["B"]|{"common":"321"}
        result=self.jobs.edit(self.ident,self.rev,0,self.pid,"B",card)
        self.assertEqual(result["revision"],1)
        self.assertEqual((await self.dispatch())["cards"]["B"]["common"],"321")
        self.assertEqual(len(self.double.sent),1)
        with self.assertRaises(ValueError):
            self.jobs.material(self.ident,self.rev,self.pid,0)

    async def test_partial_response_keeps_valid_role(self):
        self.approve()
        self.double.mode="partial"
        result=await self.dispatch()
        self.assertEqual(result["state"],"partial")
        self.assertEqual(result["cards"]["A"]["common"],"301")
        self.assertEqual(result["cards"]["B"]["common"],"")
        self.assertTrue(result["cards"]["B"]["issues"])

    async def test_conflict_resolution_remembers_explicit_clear_through_readmission(self):
        from persona_runtime.import_card_fields import unresolved_conflicts
        self.approve()
        self.double.mode="conflict"
        data = await self.dispatch()
        self.assertEqual(data["state"], "partial")
        a = data["cards"]["A"]
        self.assertEqual(a["scenes"]["daily"], "405")
        self.assertEqual(a["scenes"]["help"], "")
        self.assertEqual([i["field"] for i in unresolved_conflicts(a)], ["scenes.help"])
        old_candidate = fingerprint(data["cards"])
        self.assertTrue(unresolved_conflicts(self.jobs.edit(self.ident,self.rev,data["revision"],self.pid,"A",
                                                          a | {"issues":[]})["cards"]["A"]))
        resolved = self.jobs.edit(self.ident,self.rev,data["revision"],self.pid,"A",a,
                                  resolved_fields=["scenes.help"])
        self.assertEqual(resolved["revision"], data["revision"]+1)
        self.assertFalse(unresolved_conflicts(resolved["cards"]["A"]))
        with self.assertRaises(ValueError):
            self.jobs.edit(self.ident,self.rev,data["revision"],self.pid,"A",a,resolved_fields=["scenes.help"])
        self.jobs.readmit(self.ident, old_candidate)
        reopened = CardJobs(self.lib,self.config,self.double).status(self.ident)
        self.assertFalse(unresolved_conflicts(reopened["cards"]["A"]))
        self.assertEqual(reopened["cards"]["A"]["scenes"]["daily"], "405")
        self.assertEqual(len(self.double.sent), 1)

    async def test_publish_blocks_during_composition_and_reserved_cancel_unlocks_manual_form(self):
        self.approve()
        data = self.jobs.start(self.ident,self.rev,self.pid,True)
        with self.assertRaisesRegex(ValueError, "正在生成"):
            self.jobs.material(self.ident,self.rev,self.pid,data["revision"])
        stopped = await self.jobs.cancel(self.ident)
        self.assertEqual(stopped["state"], "unsent")
        self.assertEqual(self.jobs.material(self.ident,self.rev,self.pid,stopped["revision"]),stopped["cards"])
        self.assertEqual(self.double.sent, [])

    async def test_startup_recovers_abandoned_local_reservation_without_repeating_dispatch(self):
        from persona_runtime.import_analysis import ImportAnalysis
        self.approve()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        # Simulate shutdown before _reserve crosses the physical dispatch boundary.
        task = self.jobs.tasks.pop(self.ident)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        with self.lib.db() as db:
            self.assertEqual(db.execute("SELECT state FROM calls").fetchone()[0],"reserved")
        ImportAnalysis(self.paths,self.config,self.double)
        self.jobs = CardJobs(self.lib,self.config,self.double)
        data = self.jobs.status(self.ident)
        self.assertEqual(data["state"],"unsent")
        self.assertEqual(self.jobs.material(self.ident,self.rev,self.pid,data["revision"]),data["cards"])
        self.assertEqual((await self.dispatch())["state"],"ready")
        self.assertEqual((await self.dispatch())["state"],"ready")
        self.assertEqual(len(self.double.sent),1)

    async def test_known_unsent_can_retry_unknown_cannot(self):
        self.approve()
        self.double.mode="unsent"
        self.assertEqual((await self.dispatch())["state"],"unsent")
        self.double.mode="unknown"
        self.assertEqual((await self.dispatch())["state"],"unknown")
        self.jobs=CardJobs(self.lib,self.config,self.double)
        self.assertEqual((await self.dispatch())["state"],"unknown")
        self.assertEqual(len(self.double.sent),2)

    async def test_inflight_edit_survives_response(self):
        self.approve()
        self.double.gate.clear()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        await self.double.entered.wait()
        card=self.jobs.status(self.ident)["cards"]["A"]|{"common":"333"}
        self.jobs.edit(self.ident,self.rev,0,self.pid,"A",card)
        self.double.gate.set()
        await self.jobs.tasks[self.ident]
        data=self.jobs.status(self.ident)
        self.assertEqual(data["state"],"stale")
        self.assertEqual(data["cards"]["A"]["common"],"333")
        self.assertEqual(data["stale_candidate"]["A"]["common"],"301")

    async def test_source_revision_revocation_and_consent(self):
        self.approve()
        with self.assertRaises(ValueError):
            self.jobs.start(self.ident,self.rev,self.pid,False)
        self.double.gate.clear()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        await self.double.entered.wait()
        with self.lib.db() as db:
            db.execute("UPDATE documents SET status='revoked' WHERE id=?",(self.ident,))
        self.double.gate.set()
        await self.jobs.tasks[self.ident]
        self.assertEqual(self.jobs.status(self.ident)["state"],"revoked")
        self.assertFalse(self.jobs.status(self.ident)["cards"])
        with self.lib.db() as db:
            row,=db.execute("SELECT candidate FROM profile_card_jobs")
            self.assertIsNone(row[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calls WHERE raw IS NOT NULL").fetchone()[0],1)

    async def test_existing_manual_edit_and_clear_are_preserved_on_first_compose(self):
        self.approve()
        initial = self.jobs.status(self.ident)
        self.assertEqual(initial["state"],"available")
        data = self.jobs.edit(self.ident,self.rev,0,self.pid,"A",initial["cards"]["A"] | {"common":"901"})
        self.jobs.edit(self.ident,self.rev,data["revision"],self.pid,"A",data["cards"]["A"] | {"common":""})
        data = await self.dispatch()
        self.assertEqual(data["state"],"ready")
        self.assertEqual(data["cards"]["A"]["common"],"")
        self.assertNotIn("common",data["cards"]["A"]["sources"])
        self.assertEqual(data["cards"]["B"]["common"],"302")
        self.assertIn("common",data["cards"]["B"]["sources"])

    async def test_source_drift_without_revision_blocks_preview(self):
        self.approve()
        with self.lib.db() as db:
            row=db.execute("SELECT draft FROM documents WHERE id=?", (self.ident,)).fetchone()
            data=json.loads(row[0])
            data["A"]["claims"][0]["value"]="903"
            db.execute("UPDATE documents SET draft=? WHERE id=?",(encode(data),self.ident))
        with self.assertRaisesRegex(ValueError,"校验结果"):
            self.jobs.preview(self.ident,self.rev,self.pid)
        self.assertEqual(self.jobs.status(self.ident)["state"],"invalid_config")
        self.assertEqual(self.double.sent,[])

    async def test_deployment_approval_is_template_and_provider_scoped(self):
        desc = self.jobs.preview(self.ident,self.rev,self.pid)
        path = self.lib.root/"approval-card.json"
        grant = {"decision":"approved","purposes":["compose"],"scope":"user_confirmed_import",
                 "contract_sha256":desc["contract_sha256"],"model":desc["model"],"base_url":desc["base_url"]}
        path.write_text(encode(grant | {"request_hashes":[]}),encoding="utf8")
        self.assertEqual((await self.dispatch())["state"],"awaiting_approval")
        path.write_text(encode(grant),encoding="utf8")
        self.assertEqual((await self.dispatch())["state"],"ready")
        self.assertEqual(len(self.double.sent),1)

    async def test_receipt_recovery_after_inflight_approval_change(self):
        self.approve()
        self.double.gate.clear()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        await self.double.entered.wait()
        (self.lib.root/"approval-card.json").unlink()
        self.double.gate.set()
        await self.jobs.tasks[self.ident]
        self.assertEqual(self.jobs.status(self.ident)["state"],"received")
        self.jobs=CardJobs(self.lib,self.config,self.double)
        self.approve(dispatch_closed=True)
        self.assertEqual((await self.dispatch())["state"],"ready")
        self.assertEqual(len(self.double.sent),1)

    async def test_capacity_and_source_changes_before_completion(self):
        self.approve()
        self.config.update({"model":{"max_input_chars":1000}})
        path=self.lib.root/"contract-card.json"
        path.write_text(encode(self.template | {"system": "json " + "9"*1200}),encoding="utf8")
        self.assertEqual(self.jobs.status(self.ident)["state"],"capacity")
        self.assertFalse(self.double.sent)
        path.write_text(encode(self.template),encoding="utf8")
        self.approve()
        self.double.gate.clear()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        await self.double.entered.wait()
        with self.lib.db() as db:
            db.execute("UPDATE documents SET revision=revision+1 WHERE id=?",(self.ident,))
        self.double.gate.set()
        await self.jobs.tasks[self.ident]
        data=self.jobs.status(self.ident)
        self.assertEqual(data["state"],"stale")
        self.assertEqual(data["stale_candidate"]["A"]["common"],"301")
        self.assertEqual(data["cards"]["A"]["common"],"")
        await self.dispatch(revision=self.rev+1)
        self.assertEqual(len(self.double.sent),1)
        with self.assertRaises(ValueError):
            self.jobs.material(self.ident,self.rev,self.pid,0)

    async def test_explicit_local_readmission_freezes_old_projection_and_preserves_manual_edits(self):
        self.approve()
        data = await self.dispatch()
        old_candidate = deepcopy(data["cards"])
        old_hash = fingerprint(old_candidate)
        a = data["cards"]["A"] | {"common": "901"}
        self.jobs.edit(self.ident, self.rev, data["revision"], self.pid, "A", a)
        self.approve(dispatch_closed=True)
        approval_path = self.lib.root / "approval-card.json"
        authority = approval_path.read_bytes()
        with self.lib.db() as db:
            call_before = tuple(db.execute("SELECT * FROM calls").fetchone())
        from persona_runtime.import_card_fields import admit_cards as original
        def revised(value, drafts, lookup):
            cards = original(value, drafts, lookup)
            cards["A"]["common"] = "1010"
            cards["B"]["common"] = "1020"
            return cards
        with patch("persona_runtime.import_card_job.admit_cards", revised):
            with self.assertRaises(ValueError):
                self.jobs.readmit(self.ident, "0"*64)
            result = self.jobs.readmit(self.ident, old_hash)
            data = self.jobs.status(self.ident)
            self.assertEqual(result["previous_card_revision"] + 1, data["revision"])
            self.assertEqual(data["cards"]["A"]["common"], "901")
            self.assertEqual(data["cards"]["B"]["common"], "1020")
            self.assertEqual(self.jobs.readmit(self.ident, old_hash), result)
            self.assertEqual(self.jobs.status(self.ident)["revision"], data["revision"])
            with self.lib.db() as db:
                self.assertEqual(tuple(db.execute("SELECT * FROM calls").fetchone()), call_before)
                row, = db.execute("SELECT * FROM profile_card_admissions")
                self.assertEqual(json.loads(row["prior_candidate"]), old_candidate)
                self.assertEqual(json.loads(row["prior_draft"])["A"]["common"], "901")
                self.assertEqual(json.loads(row["audit"]), result)
            self.assertEqual(approval_path.read_bytes(), authority)
            self.assertEqual(len(self.double.sent), 1)
            approval_path.unlink()
            with self.assertRaises(ValueError):
                self.jobs.readmit(self.ident, old_hash)

    async def test_readmission_refuses_changed_source_and_corrupt_receipt(self):
        self.approve()
        data = await self.dispatch()
        previous = fingerprint(data["cards"])
        with self.lib.db() as db:
            db.execute("UPDATE documents SET status='revoked' WHERE id=?", (self.ident,))
        with self.assertRaises(ValueError):
            self.jobs.readmit(self.ident, previous)
        with self.lib.db() as db:
            db.execute("UPDATE documents SET status='ready' WHERE id=?", (self.ident,))
            db.execute("UPDATE calls SET raw=?", (b"{}",))
        with self.assertRaises(ValueError):
            self.jobs.readmit(self.ident, previous)
        with self.lib.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM profile_card_admissions").fetchone()[0], 0)

    async def test_cancel_after_dispatch_is_unknown_and_not_reissued(self):
        self.approve()
        self.double.gate.clear()
        self.jobs.start(self.ident,self.rev,self.pid,True)
        await self.double.entered.wait()
        await self.jobs.cancel(self.ident)
        self.assertEqual((await self.dispatch())["state"],"unknown")
        self.assertEqual(len(self.double.sent),1)
