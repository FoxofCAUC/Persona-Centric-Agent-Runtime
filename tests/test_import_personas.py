"""Private import, archive continuity and one-pass style selection. No external calls."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from persona_runtime.archive import read_exact, scope
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.corpus_files import digest
from persona_runtime.import_analysis import checked_result
from persona_runtime.import_history import chronological, page
from persona_runtime.import_library import CONTRACT
from persona_runtime.import_markdown import parse,assign
from persona_runtime.import_personas import PersonaImports
from persona_runtime.import_style import attach_references, compatible_samples, rank_samples
from persona_runtime.models import identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class MarkerTransport:
    def __init__(self):
        self.calls=[]
    async def complete(self,model,wire,*,save_raw):
        payload=json.loads(wire["messages"][1]["content"])
        rows=[r for r in payload["records"] if r["speaker"]==payload["target"]]
        self.calls.append(wire)
        claim={"field":"core_traits","value":"101","evidence":[{"id":rows[0]["id"],"quote":rows[0]["text"]}]}
        body={"claims":[claim],"samples":[{"id":r["id"],"act":"share","polarity":"neutral"} for r in rows]}
        raw={"choices":[{"finish_reason":"stop","message":{"content":json.dumps(body)}}],"usage":{"total_tokens":7}}
        save_raw(json.dumps(raw).encode(),http_status=200,diagnostics={})


class Vector:
    def identity(self):
        return {"fingerprint":"100"}
    def embed_documents(self,texts):
        return [[1.,0.] for _ in texts]
    def embed_query(self,texts):
        return [{"member_index":i} for i, _ in enumerate(texts)], self.embed_documents(texts)


class Main:
    def __init__(self):
        self.calls=[]
    async def complete(self,model,messages):
        self.calls.append(messages)
        return ModelCompletion("301",{"total_tokens":7},{"finish_reason":"stop"})


class ImportCases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths=RuntimePaths(Path(self.temp.name))
        self.config=ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model":{"name":"deepseek-v4-pro","base_url":"https://api.deepseek.com/v1","api_key":"100"}})
        self.store=Store(self.paths)
        self.store.initialize()
        self.transport=MarkerTransport()
        self.imp=PersonaImports(self.store,self.config,self.transport)
        self.lib=self.imp.library
        self.raw=b"## X\n100\n## Y\n200\n## X\n101\n## X\n102\n## Y\n201\n## Y\n202\n## X\n103\n"
        self.data=self.lib.add(self.raw)
        self.ident=self.data["id"]

    def prepare(self):
        return self.lib.prepare(self.ident,1,self.data["mapping"],{})

    def approve_markers(self):
        # Only opaque identifiers and the wire-format keyword reach in-process doubles.
        contract={"version":CONTRACT,"analysis_system":"101 json","model":"deepseek-flash","style_prefix":"102",
                  "reply_model":"deepseek-v4-pro","similarity_min":.8}
        path=self.lib.root/"contract.json"
        atomic_json(path,contract)
        atomic_json(self.lib.root/"approval.json",{"decision":"approved","purposes":["analysis","style"],
                    "contract_sha256":digest(path.read_bytes())})

    def fill(self,data):
        for role in ("A","B"):
            spec=data["draft"][role]["spec"]
            spec.update(introduction="101",core_traits=["102"],values_and_boundaries=["103"],
                        expression_style="104",initial_background="105")
            data=self.lib.edit(self.ident,data["revision"],role,spec,"")
        return data

    def test_parser_nesting_and_identity_correction(self):
        raw=("# X\n由 Moect 导出\n\n[2026-01-01 12:03:01] X\n100\n> Y: 102\n\n"
             "[2026-01-01 12:04:01] X\n200\n【X】\n[2026-01-01 11:01:01] Z\n203\n").encode()
        parsed=parse(raw)
        self.assertEqual(parsed["counts"],{"X":2})
        self.assertEqual([r["text"] for r in parsed["rows"]],["100","200"])
        with self.assertRaises(ValueError):assign(parsed,{"X":"A"},{})
        self.assertEqual(assign(parsed,{"X":"A"},{"2":"B"})[-1]["role"],"B")
        ticks=chr(96)*3
        self.assertEqual(parse(("## X\n100\n"+ticks+"\n## Z\n"+ticks+"\n## Y\n200").encode())["counts"],{"X":1,"Y":1})

    def test_empty_session_and_atomic_restore_both_perspectives(self):
        d=self.fill(self.prepare())
        opts=(self.ident,d["revision"],["A","B"],False,"201",True)
        ids=self.imp.create(*opts)
        self.assertEqual(ids,self.imp.create(*opts))
        for pid,expected in zip(ids,["persona","user"]):
            service=RuntimeService(self.store)
            service.use_persona(pid)
            with self.store.connection() as db:
                sid=db.execute("SELECT session_id FROM channel_sessions WHERE persona_id=?",(pid,)).fetchone()[0]
                turns=[dict(t) for t in db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY rowid",(sid,))]
                canonical=[e for t in turns for group in [read_exact(db,scope(pid,"desktop"),{"kind":"turn","id":t["id"]})]
                           for e in chronological(group)]
                from types import SimpleNamespace
                from persona_runtime.automatic_memory import AutomaticMemory
                automation=AutomaticMemory(SimpleNamespace(store=self.store,model=lambda:{"max_input_chars":24000}))
                payload=automation.payload(db,pid,sid,scope(pid,"desktop"),
                    [dict(t)|{"ordinal":i+1} for i,t in enumerate(turns)])
                self.assertEqual([e["id"] for e in payload["events"]],[e["id"] for e in canonical])
                self.assertEqual(len(canonical),7)
                self.assertEqual([r["text"] for r in canonical],["100","200","101","102","201","202","103"])
                self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0],0)
            recent=page(self.store,pid,sid,limit=2)
            earlier=page(self.store,pid,sid,recent["before"])
            self.assertEqual((earlier["rows"]+recent["rows"])[0]["role"],expected)
        self.assertEqual(len(set(ids)),2)

    async def test_dispatch_proof_and_withdrawal_before_offer(self):
        d=self.prepare()
        with self.assertRaises(ValueError):self.imp.analysis.start(self.ident,d["revision"],True)
        self.approve_markers()
        self.imp.analysis.start(self.ident,d["revision"],True)
        await self.imp.analysis.tasks[self.ident]
        d=self.lib.status(self.ident)
        self.assertEqual(d["status"],"ready")
        self.assertEqual(len(self.transport.calls),2)
        d=self.fill(d)
        with patch("persona_runtime.import_style.provider_for",return_value=Vector()):
            pid=self.imp.create(self.ident,d["revision"],["B"],True,"301",True)[0]
            service=RuntimeService(self.store)
            service.use_persona(pid)
            client=Main()
            engine=ChatEngine(self.store,self.config,client)
            try:
                sid=engine.session("desktop")["session_id"]
                from persona_runtime.ingress import IngressService
                from persona_runtime.tk_notice import CONTRACT as VISIBLE
                ingress=IngressService(engine)
                with self.store.connection() as db:
                    policy=ingress.policy(db,pid,"desktop")
                ingress.update_policy(pid,"desktop","legacy_per_message",policy["id"],identifier(),
                    companion_mode="minimal_im",reply_mode="context_full",context_mode="working_set",expression_policy_version=3)
                receipt=ingress.accept({"channel":"desktop","persona_id":pid,"session_id":sid,"request_id":identifier(),
                                        "text":"501","desktop_presentation":VISIBLE})
                turn={"id":receipt["turn_id"]}
                await engine.wait()
                result=engine.get(turn["id"])
                self.assertTrue(result.get("scene_speech"),result.get("error_code"))
                self.assertEqual(len(client.calls),1)
                messages=client.calls[0]
                self.assertIn("103",[m["content"] for m in messages])
                self.assertTrue(any(m["content"].startswith("102[") for m in messages))
                speech=result["scene_speech"]
                await self.imp.revoke(self.ident)
                offer=engine.scene_publication.publisher.offer(pid,sid,speech["utterance_id"],
                    speech["publication_id"],speech["revision"],0,"301")
                self.assertEqual(offer["state"],"stopped")
                self.assertIsNone(engine.get(turn["id"])["reply_event_id"])
            finally:
                await engine.scene.close()
                engine.cognitive.characters.close()

    def test_other_uses_only_ordinary_context(self):
        sample = {"id":"1","act":"other","cue":"100","text":"201","polarity":"neutral"}
        self.assertEqual(compatible_samples([sample], "101", []), [sample])
        self.assertEqual(compatible_samples([sample], "102?", []), [])
        self.assertEqual(compatible_samples([sample], "101", ["1"]), [])
        self.assertEqual(compatible_samples([sample | {"cue":"100?"}], "101", []), [])

    def test_rank_uses_current_chunks_and_requires_current_match(self):
        sample = {"id":"1","text":"201"}
        vectors = {"1":[1.,0.]}
        expected = [(1., "1", "201")]
        self.assertEqual(rank_samples([sample], vectors, [[1.,0.],[0.,1.]], [], .82), expected)
        self.assertEqual(rank_samples([sample], vectors, [[0.,1.]], [[1.,0.]], .82), [])
        self.assertEqual(rank_samples([sample], vectors, [[1.,0.]], [], .82, ["1"]), [])

    def test_reference_condition_inserts_only_one_message(self):
        messages = [{"role":"system", "content":"100"}, {"role":"assistant", "content":"101"},
                    {"role":"user", "content":"102"}]
        details = {"prompt_assembly":{"blocks":[{"message_index":i} for i in range(3)]}}
        kwargs = dict(prefix="103", budget=300, version="104", checksum="105", act="share")
        baseline, untreated = attach_references(messages, details, ranked=[], **kwargs)
        candidate, treated = attach_references(messages, details, ranked=[(1., "1", "201")], **kwargs)
        self.assertEqual(baseline, messages)
        self.assertEqual(candidate[:-2] + candidate[-1:], baseline)
        self.assertEqual(candidate[-2], {"role":"user", "content":'103["201"]'})
        self.assertEqual(untreated["import_style"]["ids"], [])
        self.assertEqual(treated["import_style"]["ids"], ["1"])
        self.assertEqual(details["prompt_assembly"]["blocks"][-1]["message_index"], 2)

    def test_samples_bind_through_a_contiguous_reply_burst(self):
        records = [
            {"id":"1","speaker":"B","text":"100","group":"11"},
            {"id":"2","speaker":"A","text":"101","group":"11"},
            {"id":"3","speaker":"A","text":"102","group":"11"},
            {"id":"5","speaker":"A","text":"103","group":"11"},
            {"id":"6","speaker":"B","text":"104","group":"12"},
            {"id":"7","speaker":"A","text":"105","group":"13"},
            {"id":"8","speaker":"A","text":"106","group":"13"},
        ]
        result = {"claims":[], "samples":[{"id":i,"act":"share","polarity":"neutral"}
                                        for i in ["2","3","5","8"]]}
        _, samples = checked_result(result, "A", records)
        self.assertEqual([(s["id"],s["cue_id"],s["text"]) for s in samples],
                         [("2","1","101"),("3","1","102")])

    def test_wrong_speaker_rejected(self):
        with self.assertRaises(ValueError):
            checked_result({"claims":[{"field":"core_traits","value":"101","evidence":[{"id":"2","quote":"101"}]}],
                            "samples":[]},"A",[{"id":"2","speaker":"B","text":"101","group":"1"}])

    def test_one_invalid_draft_rolls_back_pair(self):
        d=self.prepare()
        spec=d["draft"]["A"]["spec"]
        spec.update(introduction="101",core_traits=["102"],values_and_boundaries=["103"],expression_style="104",initial_background="105")
        d=self.lib.edit(self.ident,d["revision"],"A",spec,"")
        before=len(RuntimeService(self.store).list_personas())
        with self.assertRaises(ValueError):
            self.imp.create(self.ident,d["revision"],["A","B"],False,"301",True)
        self.assertEqual(len(RuntimeService(self.store).list_personas()),before)
        self.assertEqual(self.lib.add(self.raw)["revision"],d["revision"])


if __name__=="__main__":
    unittest.main()
