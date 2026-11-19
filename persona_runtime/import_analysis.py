"""Receipt-backed analysis of an explicitly confirmed, redacted import view."""
import asyncio
import json
from collections import defaultdict

from .archive import fingerprint
from .corpus_learning_transport import CorpusCallFailure, CorpusLearningHTTPClient
from .import_library import CONTRACT, FIELDS, ImportLibrary, encode, require
from .models import RuntimeErrorBase, timestamp
from .provider_compat import build_request, usage_fields
from .provider_policy import require_generation_provider

ACTS = {"share", "request", "question", "comfort", "close", "other"}
POLARITIES = {"positive","negative","neutral"}


def make_wire(model,role,material,system):
    return build_request(model,[{"role":"system","content":system},
        {"role":"user","content":encode({"target":role,"records":material})}])[0]


def batches(rows, capacity):
    batch = []
    length = 0
    for row in rows:
        size = len(encode(row))+1
        require(size <= capacity, "单条消息超出模型容量，请缩小分析范围。")
        if batch and length+size > capacity:
            yield batch
            batch, length = [], 0
        batch.append(row)
        length += size
    if batch:
        yield batch


def checked_result(value, role, rows):
    available = {r["id"]:r for r in rows if r["speaker"] == role}
    require(isinstance(value,dict) and set(value)=={"claims","samples"}, "模型返回字段无效。")
    claims, samples = value["claims"], value["samples"]
    require(isinstance(claims,list) and isinstance(samples,list), "模型返回列表无效。")
    for claim in claims:
        require(isinstance(claim,dict) and set(claim)=={"field","value","evidence"}
                and claim["field"] in FIELDS and isinstance(claim["value"],str)
                and 0<len(claim["value"].strip())<=2000 and isinstance(claim["evidence"],list)
                and claim["evidence"], "人设建议缺少可核对的证据。")
        for ref in claim["evidence"]:
            require(isinstance(ref,dict) and set(ref)=={"id","quote"} and ref["id"] in available
                    and isinstance(ref["quote"],str) and ref["quote"].strip()
                    and ref["quote"] in available[ref["id"]]["text"], "证据与目标人物的原话不符。")
    positions={r["id"]:i for i,r in enumerate(rows)}
    examples = []
    for sample in samples:
        require(isinstance(sample,dict) and set(sample)=={"id","act","polarity"} and sample["id"] in available
                and sample["act"] in ACTS and sample["polarity"] in POLARITIES, "表达样例与目标人物不符。")
        target = available[sample["id"]]
        if len(target["text"]) > 600:
            continue
        for index in range(positions[sample["id"]]-1, -1, -1):
            previous = rows[index]
            if previous["group"] != target["group"] or int(previous["id"])+1 != int(rows[index+1]["id"]):
                break
            if previous["speaker"] != role:
                examples.append(sample | {"text":target["text"], "cue":previous["text"], "cue_id":previous["id"]})
                break
    return claims, examples


class ImportAnalysis:
    def __init__(self, paths, config, transport=None):
        self.library = ImportLibrary(paths)
        self.config = config
        self.transport = transport or CorpusLearningHTTPClient()
        self.tasks = {}
        if (self.library.root/"imports.sqlite3").exists():
            with self.library.db() as db:
                db.execute("UPDATE documents SET status='interrupted' WHERE status='running'")
                db.execute("UPDATE calls SET state='unknown' WHERE state='sending' AND raw IS NULL")

    def start(self, ident, revision, confirmed, *, cached_only=False):
        require(confirmed is True, "需要确认脱敏视图和云端分析用途。")
        contract, checksum = self.library.contract("analysis")
        model = self.config.load()["model"] | {"name":contract["model"], "_json_output":True}
        require_generation_provider(model)
        require(cached_only or model["encrypted_key"], "请先配置官方 DeepSeek 连接。")
        with self.library.db() as db:
            changed = db.execute("""UPDATE documents SET status='running',approved=? WHERE id=? AND revision=?
                AND view IS NOT NULL AND status IN ('prepared','interrupted','cancelled','failed','unknown','partial_ready')""",
                (checksum,ident,revision)).rowcount
            require(changed and ident not in self.tasks, "该任务已受理或草稿已变化。")
        self.tasks[ident] = asyncio.create_task(self.run(ident,revision,contract,checksum,model,cached_only=cached_only))
        return self.library.status(ident)

    async def cancel(self, ident):
        task = self.tasks.get(ident)
        if task:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.tasks.pop(ident,None)
        with self.library.db() as db:
            db.execute("UPDATE documents SET status='cancelled' WHERE id=? AND status='running'",(ident,))
        return self.library.status(ident)

    async def drain(self):
        await asyncio.gather(*(self.cancel(i) for i in tuple(self.tasks)))

    async def call(self, ident, role, material, system, model, checksum, *, cached_only=False):
        require(self.library.contract("analysis")[1]==checksum, "提炼模板授权已变化。")
        wire=make_wire(model,role,material,system)
        approval=json.loads((self.library.root/"approval.json").read_bytes())
        if "request_hashes" in approval:
            require(fingerprint(wire) in approval["request_hashes"],"本请求超出已批准的调用范围。")
        key = fingerprint([CONTRACT,ident,checksum,model["base_url"],wire])
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old=db.execute("SELECT * FROM calls WHERE id=?",(key,)).fetchone()
            if cached_only and (not old or old["raw"] is None):
                return None
            if old and old["raw"] is None and old["state"] in ("sending","unknown"):
                raise RuntimeErrorBase("unknown")
            if not old or old["state"]=="unsent":
                require(not approval.get("dispatch_closed",False),"本次调用范围已结束。")
                db.execute("INSERT OR REPLACE INTO calls VALUES(?,?,'sending',NULL,NULL,NULL,?)",(key,ident,timestamp()))
        if not old or old["state"]=="unsent":
            def save(raw, *, http_status, diagnostics):
                with self.library.db() as db:
                    db.execute("UPDATE calls SET state='received',raw=?,status=?,metadata=? WHERE id=?",
                               (raw,http_status,encode(diagnostics),key))
            try:
                await self.transport.complete(model,wire,save_raw=save)
            except (CorpusCallFailure,asyncio.CancelledError) as exc:
                sent = getattr(exc,"diagnostics",{}).get("request_headers_sent",True)
                with self.library.db() as db:
                    db.execute("UPDATE calls SET state=? WHERE id=? AND raw IS NULL",("unknown" if sent else "unsent",key))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeErrorBase("unknown" if sent else "failed") from None
        with self.library.db() as db:
            receipt=db.execute("SELECT * FROM calls WHERE id=?",(key,)).fetchone()
            require(receipt["status"]==200, "模型请求失败，首次回执已保存。")
            reply=json.loads(receipt["raw"])
            metadata=json.loads(receipt["metadata"])
            metadata["usage"]=usage_fields(reply.get("usage"),deepseek=True)
            db.execute("UPDATE calls SET metadata=? WHERE id=?",(encode(metadata),key))
        choice=reply["choices"][0]
        require(choice.get("finish_reason")=="stop", "提炼回执未完整结束。")
        return json.loads(choice["message"]["content"])

    async def run(self, ident, revision, contract, checksum, model, *, cached_only=False):
        state = "failed"
        try:
            row = self.library.row(ident)
            view = json.loads(row["view"])
            drafts = json.loads(row["draft"])
            capacity = model["max_input_chars"] - len(contract["analysis_system"]) - 128
            approval=json.loads((self.library.root/"approval.json").read_bytes())
            allowed=set(approval.get("request_hashes",[])) if "request_hashes" in approval else None
            partial=False
            for role, draft in drafts.items():
                claims, samples = {}, {}
                eligible=received=0
                for group in batches(view,capacity):
                    if not any(r["speaker"]==role for r in group):
                        continue
                    eligible+=1
                    if allowed is not None and fingerprint(make_wire(model,role,group,contract["analysis_system"])) not in allowed:
                        partial=True
                        continue
                    result = await self.call(ident,role,group,contract["analysis_system"],model,checksum,cached_only=cached_only)
                    if result is None:
                        partial=True
                        continue
                    items, examples = checked_result(result,role,group)
                    received+=1
                    for item in items:
                        key=(item["field"],item["value"].strip())
                        refs=claims.setdefault(key,{})
                        refs.update({(r["id"],r["quote"]):r for r in item["evidence"]})
                    samples.update({item["id"]:item for item in examples})
                require(received>0,"此人物还没有适用的已审回执。")
                draft["coverage"]={"received":received,"eligible":eligible}
                suggestions=[{"field":k,"value":v,"evidence":list(refs.values())} for (k,v),refs in claims.items()]
                if contract.get("merge_system") and len(suggestions)>1 and allowed is None:
                    merged=[]
                    lookup={r["id"]:r for r in view if r["speaker"]==role}
                    for batch in batches(suggestions,model["max_input_chars"]-len(contract["merge_system"])-128):
                        allowed_refs={(ref["id"],ref["quote"]) for c in batch for ref in c["evidence"]}
                        inputs=[lookup[i] for i in dict.fromkeys(ref["id"] for c in batch for ref in c["evidence"])]
                        result=await self.call(ident,role,batch,contract["merge_system"],model,checksum,cached_only=cached_only)
                        if result is None:
                            partial=True
                            merged.extend(batch)
                            continue
                        items,_=checked_result(result,role,inputs)
                        require(all((ref["id"],ref["quote"]) in allowed_refs for item in items for ref in item["evidence"]),
                                "汇总引用超出已提炼的证据。")
                        merged.extend(items)
                    suggestions=merged
                groups={r["id"]:r["group"] for r in view}
                for item in suggestions:
                    count=len({groups[ref["id"]] for ref in item["evidence"]})
                    item["needs_review"]=item["field"]=="core_traits" and count<2
                draft["claims"]=suggestions
                draft["samples"]=list(samples.values())
                fields=defaultdict(list)
                for item in sorted(draft["claims"],key=lambda c:-len({r["id"] for r in c["evidence"]})):
                    if not item["needs_review"] and item["value"] not in fields[item["field"]]:
                        fields[item["field"]].append(item["value"])
                for field, values in fields.items():
                    if field not in draft.get("edited_fields",[]):
                        picked=[]
                        for value in values:
                            if len(picked)>=8 or (picked and sum(map(len,picked))+len(value)>1200):
                                break
                            picked.append(value)
                        draft["spec"][field]=picked if field in ("core_traits","values_and_boundaries") else "\n".join(picked)
            require(self.library.contract("analysis")[1]==checksum,"提炼模板已撤回。")
            with self.library.db() as db:
                state="partial_ready" if partial else "ready"
                db.execute("UPDATE documents SET draft=?,revision=revision+1,status=? WHERE id=? AND revision=? AND status='running'",
                           (encode(drafts),state,ident,revision))
        except asyncio.CancelledError:
            state="cancelled"
            raise
        except (ValueError,TypeError,KeyError,IndexError,OSError) as exc:
            state="unknown" if str(exc)=="unknown" else "failed"
        finally:
            with self.library.db() as db:
                db.execute("UPDATE documents SET status=? WHERE id=? AND status='running'",(state,ident))
            self.tasks.pop(ident,None)
