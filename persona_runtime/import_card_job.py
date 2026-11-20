"""Durable, one-request composition of two private role-card drafts."""
from .call_limits import resume_on_limit, workflow
import asyncio
import json
from contextlib import nullcontext
from copy import deepcopy

from .archive import fingerprint
from .character_embedding import safe_path
from .corpus_files import digest
from .corpus_learning_transport import CorpusCallFailure, CorpusLearningHTTPClient
from .import_analysis_v2 import VERSION as PROFILE_VERSION, _unique_keys
from .import_card_fields import (WORKFLOW_POLICY, admit_cards, build_source, initial_workbench_card, normalize_workbench_reply,
                                 include_profile_conflicts, unresolved_conflicts, validate_edit, workflow_cards)
from .import_library import encode, require
from .import_prompts import contract_source, has_contract, has_release_permission, release_model
from .import_profile_workspace import projected_claims
from .models import timestamp
from .provider_compat import build_request, usage_fields
from .provider_policy import require_generation_provider

VERSION = "pcar-dual-card-v1"
ADMISSION_REVISION = "pcar-role-card-admission-v2"
ROLES = ("A", "B")


def _source(db, ident):
    return db.execute("""SELECT d.id,d.revision,d.status,d.draft,d.view IS NULL AS v2,c.plan
                         FROM documents d LEFT JOIN profile_current c ON c.document=d.id
                         WHERE d.id=?""", (ident,)).fetchone()


def _job(db, ident):
    return db.execute("""SELECT j.*,c.state,c.status AS http_status,c.raw IS NOT NULL AS received
                         FROM profile_card_jobs j JOIN calls c ON c.id=j.call_id AND c.document=j.document
                         WHERE j.document=?""", (ident,)).fetchone()


def _edited(db, ident):
    return db.execute("SELECT * FROM profile_card_drafts WHERE document=?", (ident,)).fetchone()


def _matches(row, source):
    return row and row["plan"] == source["plan"] and row["source_revision"] == source["revision"]


def _profiles_settled(db, src):
    if not (src["v2"] and src["plan"] and src["status"] in ("ready", "partial_ready")):
        return False
    waiting = db.execute("""SELECT 1 FROM profile_results WHERE plan=?
                            AND state IN ('awaiting_approval','unsent','unknown') LIMIT 1""", (src["plan"],)).fetchone()
    return not waiting


def _both_have_observations(drafts):
    return all(any(c.get("decision") != "reject" for c in drafts[role]["claims"]) for role in ROLES)


def _eligible(db, src, drafts):
    return _profiles_settled(db, src) and _both_have_observations(drafts)


def _source_hash(src, drafts, stats):
    return fingerprint([VERSION,src["id"],src["plan"],src["revision"],drafts,stats])


class CapacityError(ValueError):
    pass


def _checked_drafts(db, src, drafts):
    for role in ROLES:
        rows = db.execute("""SELECT * FROM profile_results WHERE plan=? AND role=?
                             AND state!='superseded' ORDER BY stage,batch""", (src["plan"], role))
        expected = projected_claims(rows, drafts[role].get("decisions", {}))
        def evidence(claims):
            return [{k: v for k, v in claim.items() if k != "needs_review"} for claim in claims]
        require(evidence(expected) == evidence(drafts[role]["claims"]), "草稿观察与获准分段的校验结果不一致。")


def _manual_paths(before, after, prefix=""):
    for key in after:
        if key in ("issues", "sources", "generated_fields"):
            continue
        path = prefix + key
        if after[key] == before[key]:
            continue
        if isinstance(after[key], dict) and path != "character.age":
            yield from _manual_paths(before[key], after[key], path + ".")
        else:
            yield path


def _overlay(cards, previous, paths):
    for role, path in paths:
        old, new = previous[role], cards[role]
        keys = path.split(".")
        for key in keys[:-1]:
            old, new = old[key], new[key]
        new[keys[-1]] = deepcopy(old[keys[-1]])
        if "generated_fields" in cards[role]:
            cards[role]["generated_fields"] = [f for f in cards[role]["generated_fields"]
                                                 if f != path and not f.startswith(path + ".")]
        sources = cards[role]["sources"]
        for field in list(sources):
            if field == path or field.startswith(path + "."):
                del sources[field]
        cards[role]["issues"] = [item for item in cards[role]["issues"]
                                if item.get("field") != path and not item.get("field", "").startswith(path + ".")]


def _receipt_cards(response, http_status, frozen):
    choice = response["choices"][0]
    require(http_status == 200 and choice["finish_reason"] == "stop", "成卡回执未完整结束。")
    value = json.loads(choice["message"]["content"], object_pairs_hook=_unique_keys)
    material, lookup = build_source(frozen["drafts"], frozen["stats"])
    require(encode(material) == frozen["packet"]["wire"]["messages"][1]["content"], "成卡来源发生变化。")
    if frozen.get("card_policy") == WORKFLOW_POLICY:
        cards = admit_cards(normalize_workbench_reply(value,lookup), frozen["drafts"], lookup)
        return workflow_cards(cards, frozen["drafts"], value, lookup)
    return admit_cards(value, frozen["drafts"], lookup)


class CardJobs:
    def __init__(self, library, config, transport=None):
        self.library = library
        self.config = config
        self.transport = transport or CorpusLearningHTTPClient()
        self.tasks = {}

    def _path(self, name):
        root = self.library.root
        return safe_path(root.parent.parent,root/name)

    def _contract(self):
        raw, release = contract_source(self.library, "card")
        if raw is None:
            return None, None, None
        data = json.loads(raw,object_pairs_hook=_unique_keys)
        require(isinstance(data,dict) and data.get("version") == VERSION
                and isinstance(data.get("model"),str) and isinstance(data.get("system"),str)
                and data["system"].strip(), "角色卡合成模板无效。")
        return data, digest(raw), release

    def _model(self, contract, release):
        model = self.config.load()["model"] | {"_json_output":True,"name":contract["model"]}
        if release:
            model = release_model(model)
        require_generation_provider(model)
        return model

    def _approved(self, packet, *, sending):
        if packet.get("prompt_release"):
            return packet["stage"] == "compose" and has_release_permission(
                self.library, "card", packet, packet["contract_sha256"])
        try:
            grant = json.loads(self._path("approval-card.json").read_bytes(),object_pairs_hook=_unique_keys)
        except (OSError, ValueError):
            return False
        if not isinstance(grant,dict):
            return False
        if (grant.get("decision") != "approved" or not isinstance(grant.get("purposes"),list)
                or "compose" not in grant["purposes"] or (sending and grant.get("dispatch_closed",False) is not False)):
            return False
        if any(grant.get(k) != packet[k] for k in ("contract_sha256","base_url","model")):
            return False
        if grant.get("scope") == "user_confirmed_import":
            return not any(k in grant for k in ("document","plan_id","source_revision","source_sha256","request_hashes"))
        hashes = grant.get("request_hashes")
        return (isinstance(hashes,list) and packet["wire_hash"] in hashes
                and all(grant.get(k) == packet[k] for k in ("document","plan_id","source_revision","source_sha256")))

    def _freeze(self, db, ident, revision, plan_id, *, card_policy=WORKFLOW_POLICY):
        src = _source(db,ident)
        require(src and src["revision"] == revision and src["plan"] == plan_id, "草稿版本已变化。")
        drafts = json.loads(src["draft"])
        require(_eligible(db,src,drafts), "请先取得两位说话人的有效画像并完成本轮提炼。")
        row = db.execute("SELECT payload FROM profile_plans WHERE id=? AND document=?", (plan_id,ident)).fetchone()
        plan = json.loads(row[0]) if row else {}
        require(plan.get("id") == plan_id == fingerprint([PROFILE_VERSION,ident,{k:v for k,v in plan.items() if k!="id"}]),
                "分析计划与存档不一致。")
        _checked_drafts(db, src, drafts)
        contract,checksum,release = self._contract()
        require(contract is not None, "请配置本实例的角色卡合成模板。")
        model = self._model(contract, release)
        stats = {r:plan["roles"][r].get("stats",{}) for r in ROLES}
        payload,_ = build_source(drafts,stats)
        messages = [{"role":"system","content":contract["system"]},{"role":"user","content":encode(payload)}]
        if sum(len(m["content"]) for m in messages) > model["max_input_chars"]:
            raise CapacityError("双人画像及模板超出输入容量，请缩小来源范围。")
        wire = build_request(model,messages)[0]
        packet = {"version":VERSION,"stage":"compose","document":ident,"plan_id":plan_id,
                  "source_revision":revision,"source_sha256":_source_hash(src,drafts,stats),
                  "contract_sha256":checksum,"model":model["name"],"base_url":model["base_url"],
                  "max_input_chars":model["max_input_chars"],"wire":wire,"wire_hash":fingerprint(wire)}
        if release:
            packet["prompt_release"] = release
        frozen = {"packet":packet,"drafts":drafts,"stats":stats}
        if card_policy is not None:
            require(card_policy == WORKFLOW_POLICY, "未知的成卡校验版本。")
            frozen["card_policy"] = card_policy
        return packet,frozen

    def preview(self, ident, revision, plan_id, *, db=None):
        with (self.library.db() if db is None else nullcontext(db)) as conn:
            return self._freeze(conn,ident,revision,plan_id)[0]

    def _checked_model(self, packet):
        contract,checksum,release = self._contract()
        require(contract and checksum == packet["contract_sha256"] and release == packet.get("prompt_release"),
                "成卡模板已变化。")
        model = self._model(contract, release)
        wire = packet["wire"]
        require(wire["messages"][0]["content"] == contract["system"]
                and packet["wire_hash"] == fingerprint(wire) and build_request(model,wire["messages"])[0] == wire
                and model["base_url"] == packet["base_url"] and model["max_input_chars"] == packet["max_input_chars"],
                "成卡模型或请求内容已变化。")
        return model

    def status(self, ident):
        with self.library.db() as db:
            src = _source(db,ident)
            require(src,"未找到导入记录。")
            result = {"plan_id":src["plan"],"source_revision":src["revision"],"revision":0,"cards":{},
                      "stale":False,"configured":has_contract(self.library, "card"),"state":"none"}
            if src["status"] == "revoked":
                return result | {"state":"revoked"}
            if not src["plan"] or not src["v2"]:
                return result
            drafts = json.loads(src["draft"])
            if not _profiles_settled(db, src):
                return result | {"state":"not_ready"}
            edited = _edited(db,ident)
            card_revision = edited["revision"] if _matches(edited,src) else 0
            try:
                result.update(cards=self._current_cards(db, ident, src["revision"], src["plan"], card_revision),
                              revision=card_revision, state="available")
            except (ValueError, TypeError, KeyError):
                return result | {"state":"invalid_config"}
            job = _job(db,ident)
            if not job:
                if not _both_have_observations(drafts):
                    return result | {"state": "manual_only"}
                if not result["configured"]:
                    return result | {"state": "unconfigured"}
                try:
                    packet, _ = self._freeze(db, ident, src["revision"], src["plan"])
                    if not self._approved(packet, sending=True):
                        result["state"] = "awaiting_approval"
                    elif not self._checked_model(packet).get("encrypted_key"):
                        result["state"] = "no_key"
                except CapacityError:
                    result["state"] = "capacity"
                except (ValueError, TypeError, KeyError, OSError):
                    result["state"] = "invalid_config"
                return result
            stale = not _matches(job,src)
            state = job["outcome"] or job["state"]
            if state == "reserved":
                state = "sending" if ident in self.tasks else "unsent"
            elif state == "sending" and ident not in self.tasks:
                state = "unknown"
            result.update(state="stale" if stale else state,stale=stale,error=job["error"])
            if job["candidate"] and (stale or job["outcome"] == "stale") and job["plan"] == src["plan"]:
                result.update(stale=True,stale_candidate=json.loads(job["candidate"]))
            return result

    def start(self, ident, revision, plan_id, confirmed):
        require(confirmed is True, "请确认使用当前两份画像生成角色卡。")
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            src = _source(db,ident)
            require(src and src["plan"] == plan_id and src["revision"] == revision
                    and src["status"] != "revoked", "草稿已变化或来源已撤回。")
            job = _job(db,ident)
            if ident in self.tasks or (job and (job["candidate"] or job["outcome"] == "failed"
                    or job["state"] in ("unknown","sending"))):
                action = "existing"
            elif job and job["received"]:
                call_id = job["call_id"]
                action = "recover"
            else:
                packet,frozen = self._freeze(db,ident,revision,plan_id)
                if not self._approved(packet,sending=True):
                    action = "awaiting_approval"
                else:
                    require(self._checked_model(packet).get("encrypted_key"), "请配置官方 DeepSeek 连接。")
                    call_id = fingerprint([VERSION,ident,plan_id,revision,packet["base_url"],packet["contract_sha256"],packet["wire"]])
                    edited = _edited(db,ident)
                    base_revision = edited["revision"] if _matches(edited,src) else 0
                    changed = db.execute("""INSERT INTO calls VALUES(?,?,'reserved',NULL,NULL,NULL,?)
                        ON CONFLICT(id) DO UPDATE SET state='reserved'
                        WHERE calls.raw IS NULL AND calls.state IN ('unsent','reserved')""",
                        (call_id,ident,timestamp())).rowcount
                    require(changed, "该请求已有派发记录。")
                    db.execute("""INSERT OR REPLACE INTO profile_card_jobs
                        (document,plan,source_revision,call_id,snapshot,base_revision) VALUES(?,?,?,?,?,?)""",
                        (ident,plan_id,revision,call_id,encode(frozen),base_revision))
                    action = "send"
        if action in ("send","recover"):
            self.tasks[ident] = asyncio.create_task(self._run(ident,action,call_id))
        status = self.status(ident)
        if action == "awaiting_approval":
            status["state"] = action
        return status

    def _reserve(self, ident, call_id):
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = _job(db,ident)
            src = _source(db,ident)
            require(job and job["call_id"] == call_id and _matches(job,src) and src["v2"] and src["status"] in ("partial_ready","ready"),
                    "来源已变化或撤回。")
            frozen = json.loads(job["snapshot"])
            packet = frozen["packet"]
            current_packet, _ = self._freeze(db, ident, job["source_revision"], job["plan"])
            require(current_packet == packet and fingerprint(
                    [VERSION, ident, job["plan"], job["source_revision"], packet["base_url"],
                     packet["contract_sha256"], packet["wire"]]) == call_id, "成卡请求与存档不一致。")
            model = self._checked_model(packet)
            require(self._approved(packet,sending=True), "本次成卡请求尚未批准。")
            require(_source_hash(src,json.loads(src["draft"]),frozen["stats"]) == packet["source_sha256"],
                    "两份画像草稿发生变化。")
            from .call_limits import external_gate
            external_gate(self,'import','card/'+call_id)
            changed = db.execute("UPDATE calls SET state='sending' WHERE id=? AND state='reserved' AND raw IS NULL",
                                 (job["call_id"],)).rowcount
            return (job["call_id"],packet,model) if changed else None

    @resume_on_limit
    async def _reserve_call(self,ident,call_id):
        return self._reserve(ident,call_id)

    @workflow
    async def _run(self, ident, action, call_id):
        claimed = None
        try:
            if action == "send":
                claimed = await self._reserve_call(ident,call_id)
                if not claimed:
                    return
                key,packet,model = claimed
                def save(raw, *, http_status, diagnostics):
                    meta = diagnostics | {"stage":"compose","wire_hash":packet["wire_hash"],"response_sha256":digest(raw)}
                    with self.library.db() as db:
                        db.execute("""UPDATE calls SET state='received',raw=?,status=?,metadata=?
                                      WHERE id=? AND raw IS NULL""",(raw,http_status,encode(meta),key))
                await self.transport.complete(model,packet["wire"],save_raw=save)
            self._publish(ident,call_id)
        except (CorpusCallFailure,asyncio.CancelledError) as exc:
            if claimed:
                diagnostics = getattr(exc,"diagnostics",{})
                self._mark_missing(claimed[0], "unknown" if diagnostics.get("request_headers_sent",True) else "unsent",
                                   diagnostics | {"stage":"compose","wire_hash":claimed[1]["wire_hash"]})
            if isinstance(exc,asyncio.CancelledError):
                raise
        except (ValueError, OSError, KeyError, TypeError, IndexError, RuntimeError):
            # A saved first receipt remains recoverable once gates are restored.
            if claimed:
                self._mark_missing(claimed[0],"unknown",{"stage":"compose","wire_hash":claimed[1]["wire_hash"]})
        finally:
            if claimed:
                self._mark_missing(claimed[0],"unknown",{"stage":"compose","wire_hash":claimed[1]["wire_hash"]})
            if self.tasks.get(ident) is asyncio.current_task():
                self.tasks.pop(ident,None)

    def _mark_missing(self,key,state,metadata):
        with self.library.db() as db:
            db.execute("""UPDATE calls SET state=?,metadata=? WHERE id=? AND raw IS NULL AND state='sending'""",
                       (state,encode(metadata),key))

    def _publish(self, ident, call_id):
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            src = _source(db,ident)
            job = _job(db,ident)
            if not job or job["call_id"] != call_id or not job["received"] or job["candidate"] or job["outcome"] or src["status"] == "revoked":
                return
            frozen = json.loads(job["snapshot"])
            packet = frozen["packet"]
            self._checked_model(packet)
            require(self._approved(packet,sending=False),"成卡用途已撤回。")
            row = db.execute("SELECT raw,status,metadata FROM calls WHERE id=?", (job["call_id"],)).fetchone()
            meta = json.loads(row["metadata"])
            require(digest(row["raw"]) == meta["response_sha256"] and meta["wire_hash"] == packet["wire_hash"],
                    "成卡回执指纹不一致。")
            try:
                response = json.loads(row["raw"],object_pairs_hook=_unique_keys)
                meta["usage"] = usage_fields(response.get("usage"),deepseek=True)
                db.execute("UPDATE calls SET metadata=? WHERE id=?", (encode(meta),job["call_id"]))
                cards = _receipt_cards(response, row["status"], frozen)
            except (ValueError,TypeError,KeyError,IndexError,AttributeError,RecursionError):
                db.execute("UPDATE profile_card_jobs SET outcome='failed',error='invalid_receipt' WHERE document=?",(ident,))
                return
            edited = _edited(db,ident)
            current_revision = edited["revision"] if _matches(edited,src) else 0
            state = "partial" if any(cards[r].get("issues") for r in ROLES) else "ready"
            candidate = encode(cards)
            if (not _matches(job,src) or not src["v2"] or src["status"] not in ("partial_ready","ready")
                    or current_revision != job["base_revision"]
                    or _source_hash(src,json.loads(src["draft"]),frozen["stats"]) != packet["source_sha256"]):
                state = "stale"
            else:
                if edited and current_revision:
                    paths = db.execute("""SELECT role,path FROM profile_card_edits
                                          WHERE document=? AND plan=? AND source_revision=?""",
                                       (ident,job["plan"],job["source_revision"]))
                    _overlay(cards,json.loads(edited["cards"]),paths)
                    state = "partial" if any(cards[r]["issues"] for r in ROLES) else "ready"
                db.execute("INSERT OR REPLACE INTO profile_card_drafts VALUES(?,?,?,?,?)",
                           (ident,src["plan"],src["revision"],current_revision+1,encode(cards)))
            db.execute("UPDATE profile_card_jobs SET candidate=?,outcome=?,error=NULL WHERE document=?",
                       (candidate,state,ident))

    def readmit(self, ident, previous_sha256):
        """Explicit local admission upgrade. Keep the provider call and its receipt immutable."""
        require(ident not in self.tasks, "请等待当前成卡任务结束。")
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            src = _source(db, ident)
            job = _job(db, ident)
            require(src and job and _matches(job, src) and src["v2"] and
                    src["status"] in ("partial_ready", "ready"), "来源变更或已撤回。")
            require(job["state"] == "received" and job["received"] and job["outcome"] in
                    ("ready", "partial", "stale", "failed"), "需要已结束且尚可核对的首次回执。")
            old_candidate = json.loads(job["candidate"]) if job["candidate"] else None
            old_hash = fingerprint(old_candidate)
            frozen = json.loads(job["snapshot"])
            packet, current = self._freeze(db, ident, job["source_revision"], job["plan"],
                                          card_policy=frozen.get("card_policy"))
            require(current == frozen and packet == frozen["packet"] and fingerprint([
                    VERSION, ident, job["plan"], job["source_revision"], packet["base_url"],
                    packet["contract_sha256"], packet["wire"]]) == job["call_id"], "首次请求与归档来源不符。")
            require(self._approved(packet, sending=False), "成卡用途已撤回。")
            first = db.execute("SELECT raw,status,metadata FROM calls WHERE id=? AND document=?",
                               (job["call_id"], ident)).fetchone()
            meta = json.loads(first["metadata"])
            receipt_hash = digest(first["raw"])
            require(meta.get("stage") == "compose" and meta.get("wire_hash") == packet["wire_hash"]
                    and receipt_hash == meta.get("response_sha256"), "成卡首次回执指纹不一致。")
            cards = _receipt_cards(json.loads(first["raw"], object_pairs_hook=_unique_keys),
                                   first["status"], frozen)
            new_hash = fingerprint(cards)
            prior = db.execute("""SELECT audit FROM profile_card_admissions WHERE call_id=? AND algorithm=?""",
                               (job["call_id"], ADMISSION_REVISION)).fetchone()
            if prior:
                audit = json.loads(prior["audit"])
                require(old_hash == new_hash == audit["candidate_sha256"] and
                        previous_sha256 in (old_hash, audit["prior_candidate_sha256"]) and
                        audit["receipt_sha256"] == receipt_hash and audit["wire_hash"] == packet["wire_hash"],
                        "既有离线校验记录与当前候选不符。")
                return audit
            require(previous_sha256 == old_hash, "请核对原候选指纹后重新评定。")
            candidate = encode(cards)
            edited = _edited(db, ident)
            require(edited is None or _matches(edited, src), "现有角色卡绑定了其他来源。")
            old_revision = edited["revision"] if edited else 0
            previous = json.loads(edited["cards"]) if edited else None
            if previous is not None:
                paths = db.execute("""SELECT role,path FROM profile_card_edits
                                      WHERE document=? AND plan=? AND source_revision=?""",
                                   (ident, job["plan"], job["source_revision"]))
                _overlay(cards, previous, paths)
            revision = old_revision
            if previous != cards:
                revision += 1
                db.execute("INSERT OR REPLACE INTO profile_card_drafts VALUES(?,?,?,?,?)",
                           (ident, job["plan"], job["source_revision"], revision, encode(cards)))
            outcome = "partial" if any(cards[r]["issues"] for r in ROLES) else "ready"
            audit = {
                "algorithm": ADMISSION_REVISION, "document": ident, "plan_id": job["plan"],
                "source_revision": job["source_revision"], "source_sha256": packet["source_sha256"],
                "call_id": job["call_id"], "wire_hash": packet["wire_hash"], "receipt_sha256": receipt_hash,
                "prior_outcome": job["outcome"], "outcome": outcome, "prior_candidate_sha256": old_hash,
                "candidate_sha256": new_hash, "previous_card_revision": old_revision,
                "card_revision": revision, "prior_draft_sha256": fingerprint(previous),
                "draft_sha256": fingerprint(cards), "recorded_at": timestamp(), "additional_dispatches": 0,
            }
            db.execute("INSERT INTO profile_card_admissions VALUES(?,?,?,?,?,?)",
                       (job["call_id"], ADMISSION_REVISION, ident, job["candidate"],
                        edited["cards"] if edited else None, encode(audit)))
            db.execute("UPDATE profile_card_jobs SET candidate=?,outcome=?,error=NULL WHERE document=?",
                       (candidate, outcome, ident))
            return audit

    def _current_cards(self, db, ident, revision, plan_id, card_revision):
        src = _source(db,ident)
        require(src and src["revision"] == revision and src["plan"] == plan_id and src["v2"]
                and src["status"] in ("partial_ready","ready"),"草稿已变化或来源不可用。")
        drafts = json.loads(src["draft"])
        _checked_drafts(db, src, drafts)
        edited = _edited(db,ident)
        if _matches(edited,src):
            require(edited["revision"] == card_revision,"角色卡已变化，请重新读取。")
            cards = json.loads(edited["cards"])
            manual = set(db.execute("""SELECT role,path FROM profile_card_edits
                                       WHERE document=? AND plan=? AND source_revision=?""",
                                    (ident,plan_id,revision)))
            for role, card in cards.items():
                paths = {path for owner,path in manual if owner == role}
                include_profile_conflicts(card, drafts[role], paths)
                for issue in unresolved_conflicts(card):
                    issue["severity"] = "conflict"
            return cards
        require(card_revision == 0, "角色卡已变化，请重新读取。")
        require(_profiles_settled(db,src),"画像任务尚未结束。")
        return {role:initial_workbench_card(drafts[role]) for role in ROLES}

    def material(self, ident, revision, plan_id, card_revision, *, db=None):
        with (self.library.db() if db is None else nullcontext(db)) as conn:
            job = _job(conn, ident)
            require(not job or job["outcome"] or job["state"] not in ("reserved","sending","received"),
                    "角色卡正在生成或首次回执尚待恢复，请稍后保存。")
            return self._current_cards(conn,ident,revision,plan_id,card_revision)

    def edit(self, ident, revision, card_revision, plan_id, role, card, resolved_fields=()):
        require(role in ROLES,"请选择要编辑的角色。")
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            cards = self._current_cards(db,ident,revision,plan_id,card_revision)
            updated = validate_edit(card,cards[role],resolved_fields)
            if updated != cards[role]:
                db.executemany("INSERT OR IGNORE INTO profile_card_edits VALUES(?,?,?,?,?)",
                               ((ident,plan_id,revision,role,path) for path in set(_manual_paths(cards[role],updated)) | set(resolved_fields)))
                cards[role] = updated
                db.execute("INSERT OR REPLACE INTO profile_card_drafts VALUES(?,?,?,?,?)",
                           (ident,plan_id,revision,card_revision+1,encode(cards)))
        return self.status(ident)

    async def cancel(self, ident):
        task = self.tasks.get(ident)
        if task:
            with self.library.db() as db:
                job = _job(db, ident)
                call_id = job["call_id"] if job else None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with self.library.db() as db:
                db.execute("UPDATE calls SET state='unsent' WHERE id=? AND state='reserved' AND raw IS NULL", (call_id,))
            if self.tasks.get(ident) is task:
                self.tasks.pop(ident,None)
        return self.status(ident)

    async def drain(self):
        await asyncio.gather(*(self.cancel(ident) for ident in tuple(self.tasks)))
