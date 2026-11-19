"""Private, revisioned plans and evidence-backed decisions for imported profiles."""
import asyncio
from copy import deepcopy
import json

from .archive import fingerprint
from .import_library import encode, require
from .import_analysis_v2 import ImportAnalysisV2, VERSION
from .models import timestamp


def plan_status(db, ident):
    row = db.execute("""SELECT p.summary FROM profile_current c JOIN profile_plans p ON p.id=c.plan
                        JOIN documents d ON d.id=c.document WHERE c.document=? AND d.view IS NULL""", (ident,)).fetchone()
    if not row:
        return None
    plan = json.loads(row[0])
    results = db.execute("""SELECT batch,role,stage,state,rejected FROM profile_results
                            WHERE plan=? AND state!='superseded' ORDER BY role,stage,batch""", (plan["id"],)).fetchall()
    for role, details in plan["roles"].items():
        batches = {b["id"]: b for b in details["batches"]}
        for item in results:
            if item["role"] != role:
                continue
            reasons = [record["reason"] for record in json.loads(item["rejected"])]
            audit = {"state": item["state"], "rejected_count": len(reasons),
                     "reasons": sorted(set(reasons))}
            if item["stage"] == "extract" and item["batch"] in batches:
                batches[item["batch"]].update(audit)
            elif item["stage"] == "merge":
                details.setdefault("merges", []).append({"id": item["batch"], **audit})
    plan["archived"] = [dict(r) for r in db.execute(
        "SELECT id,created FROM profile_archives WHERE document=? ORDER BY rowid DESC", (ident,))]
    return plan


def blank_drafts(names):
    return {role: {"spec": {"format_version": 1, "name": names[role], "introduction": "",
                            "core_traits": [], "expression_style": "", "values_and_boundaries": [],
                            "initial_background": ""}, "relationship": "", "claims": [], "samples": [],
                   "decisions": {}, "adopted": {}, "edited_fields": []} for role in ("A", "B")}


def projected_claims(rows, decisions):
    extracted, merged = {}, {}
    for result in rows:
        if result["state"] not in ("accepted", "partial"):
            continue
        destination = extracted if result["stage"] == "extract" else merged
        for claim in json.loads(result["accepted"]):
            destination[claim["id"]] = claim
    merged = {key: claim for key, claim in merged.items()
              if set(claim["support"] + claim["counter"]) <= extracted.keys()}
    field_batches = {}
    for claim in extracted.values():
        field_batches.setdefault(claim["field"], set()).add(claim["batch"])
    superseded = {obs for claim in merged.values()
                  for obs in (*claim.get("support", []), *claim.get("counter", []))}
    claims = [c | {"needs_review": c["needs_review"] or len(field_batches[c["field"]]) > 1}
              for ident, c in extracted.items() if ident not in superseded]
    return [c | {"decision": decisions.get(c["id"], "pending")} for c in (*claims, *merged.values())]


def requires_profile_review(claim):
    return bool(claim.get("counter_evidence"))


def apply_decision(draft, claim, decision, plan_id):
    ident, field, value = claim["id"], claim["field"], claim["value"]
    adopted = draft["adopted"]
    if decision == "accept":
        require(not requires_profile_review(claim), "这项建议有相反证据，请核对后编辑字段。")
        adopted[ident] = {key: deepcopy(claim[key]) for key in ("field", "value", "scope", "evidence")}
        adopted[ident]["plan"] = plan_id
    else:
        previous = adopted.pop(ident, None)
        if not previous or field in draft["edited_fields"] or any(
                a["field"] == field and a["value"] == value for a in adopted.values()):
            draft["decisions"][ident] = decision
            return
    is_list = field in ("core_traits", "values_and_boundaries")
    old = draft["spec"][field]
    if decision == "accept":
        if is_list:
            draft["spec"][field] = list(dict.fromkeys([*old, value]))
        elif old != value and value not in old.split("\n"):
            draft["spec"][field] = "\n".join(part for part in (old, value) if part)
    elif is_list:
        draft["spec"][field] = [part for part in old if part != value]
    else:
        # A non-manually-edited text field consists solely of accepted contributions.
        draft["spec"][field] = "\n".join(dict.fromkeys(
            a["value"] for a in adopted.values() if a["field"] == field))
    draft["decisions"][ident] = decision


class ProfileWorkspace:
    def __init__(self, library, config, transport=None):
        self.library = library
        self.engine = ImportAnalysisV2(library, config, transport)
        self.tasks = {}

    def plan(self, ident, revision, mapping, overrides, selection=None):
        from .import_plan import build_plan, parse_source
        from .import_markdown import assign
        row = self.library.row(ident)
        require(row["revision"] == revision and row["status"] not in ("running", "revoked"),
                "草稿已变化或当前状态无法重新计划。")
        same_mapping = json.loads(row["mapping"]) == mapping and json.loads(row["overrides"]) == overrides
        prepared = row | {"mapping": encode(mapping), "overrides": encode(overrides)}
        prepared.update(legacy_mapping=json.loads(row["mapping"]), legacy_overrides=json.loads(row["overrides"]))
        with self.library.db() as db:
            previous_plan = db.execute("""SELECT plan FROM profile_current WHERE document=?""", (ident,)).fetchone()
        if previous_plan:
            prepared["reserved_ids"] = self.load(ident, previous_plan[0], current=False)["reserved_ids"]
        settings, capacity = self.engine.settings()
        plan = build_plan(prepared, capacity, selection=selection)
        plan["settings"] = settings
        plan_id = fingerprint([VERSION, ident, plan])
        plan["id"] = plan_id
        summary = {k: deepcopy(v) for k, v in plan.items() if k in ("id", "roles", "choices", "source_id")}
        summary["requests"] = len(plan["batches"])
        summary["prompt_release"] = settings.get("prompt_release")
        summary["max_tokens"] = settings["max_tokens"]
        summary["configured"] = bool(settings.get("configured"))
        for role, details in summary["roles"].items():
            details["batches"] = [{"id": b["id"], "state": "planned"} for b in plan["batches"] if b["role"] == role]
        rows = assign(parse_source(row["raw"]), mapping, overrides)
        names = {role: next(r["speaker"] for r in rows if r["role"] == role) for role in ("A", "B")}
        drafts = blank_drafts(names)
        old = json.loads(row["draft"])
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,revision FROM documents WHERE id=?", (ident,)).fetchone()
            require(current["revision"] == revision and current["status"] not in ("running", "revoked"),
                    "草稿已变化，请重新读取。")
            active = db.execute("SELECT plan FROM profile_current WHERE document=?", (ident,)).fetchone()
            if not active or active[0] != plan_id or row["view"] is not None:
                if old:
                    snapshot = {"revision": revision, "mapping": json.loads(row["mapping"]),
                                "overrides": json.loads(row["overrides"]), "draft": old}
                    stamp = timestamp()
                    db.execute("INSERT INTO profile_archives VALUES(?,?,?,?,?)", (
                        fingerprint([ident, revision, snapshot]), ident, active[0] if active and row["view"] is None else None,
                        encode(snapshot), stamp))
                    if same_mapping:
                        for role, draft in drafts.items():
                            prior = old[role]
                            for field in prior.get("edited_fields", []):
                                draft["spec"][field] = deepcopy(prior["spec"][field])
                                draft["edited_fields"].append(field)
                            draft["spec"]["name"] = prior["spec"]["name"]
                            draft["relationship"] = prior["relationship"]
                db.execute("INSERT OR IGNORE INTO profile_plans VALUES(?,?,?,?,?)",
                           (plan_id, ident, encode(plan), encode(summary), timestamp()))
                db.execute("INSERT OR REPLACE INTO profile_current VALUES(?,?)", (ident, plan_id))
                db.execute("""UPDATE documents SET mapping=?,overrides=?,draft=?,view=NULL,approved=NULL,
                              revision=revision+1,status='prepared' WHERE id=?""",
                           (encode(mapping), encode(overrides), encode(drafts), ident))
        return self.library.status(ident)

    def archive(self, ident, plan_id):
        with self.library.db() as db:
            row = db.execute("""SELECT a.id,a.created,a.plan,a.payload FROM profile_archives a JOIN documents d
                                ON d.id=a.document WHERE a.document=? AND a.id=? AND d.status!='revoked'""",
                             (ident, plan_id)).fetchone()
        require(row, "找不到可查看的草稿版本。")
        return json.loads(row["payload"]) | {"id": row["id"], "source_plan": row["plan"], "created": row["created"]}

    def load(self, ident, plan_id, *, current=True):
        with self.library.db() as db:
            row = db.execute("""SELECT p.payload,c.plan,d.view IS NULL AS current_layout FROM profile_plans p JOIN documents d ON d.id=p.document
                                LEFT JOIN profile_current c ON c.document=p.document
                                WHERE p.document=? AND p.id=? AND d.status!='revoked'""",
                             (ident, plan_id)).fetchone()
        require(row and (not current or (row["current_layout"] and row["plan"] == plan_id)), "分析计划已变化或来源已撤回。")
        plan = json.loads(row["payload"])
        checksum = fingerprint([VERSION, ident, {k: v for k, v in plan.items() if k != "id"}])
        require(plan.get("id") == plan_id == checksum, "计划内容与存档指纹不符。")
        return plan

    def evidence(self, ident, plan_id, role, batch_id, start=0):
        require(type(start) is int and start >= 0 and role in ("A", "B"), "证据位置无效。")
        plan = self.load(ident, plan_id, current=False)
        batch = next((b for b in plan["batches"] if b["id"] == batch_id and b["role"] == role), None)
        require(batch is not None, "批次不属于所选人物。")
        rows = batch["records"]
        keys = ("id", "speaker", "text", "start", "end", "kind", "date")
        return {"rows": [{k: r[k] for k in keys if k in r} for r in rows[start:start+50]], "total": len(rows)}

    def review(self, ident, revision, plan_id, role, suggestion_id, decision):
        require(isinstance(suggestion_id, str) and decision in ("accept", "reject"), "采纳操作无效。")
        return self._review(ident, revision, plan_id, role, decision, suggestion_id)

    def review_all(self, ident, revision, plan_id, role):
        return self._review(ident, revision, plan_id, role, "accept", None)

    def _review(self, ident, revision, plan_id, role, decision, suggestion_id):
        require(role in ("A", "B"), "请选择目标人物。")
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT draft FROM documents d JOIN profile_current c ON d.id=c.document
                                WHERE d.id=? AND c.plan=? AND d.revision=? AND d.view IS NULL AND d.status NOT IN ('running','revoked')""",
                             (ident, plan_id, revision)).fetchone()
            require(row, "草稿已变化，请重新读取。")
            drafts = json.loads(row["draft"])
            draft = drafts[role]
            if suggestion_id is None:
                selected = [c for c in draft["claims"] if not requires_profile_review(c) and
                            c["field"] != "values_and_boundaries" and c["decision"] == "pending"]
            else:
                selected = [c for c in draft["claims"] if c["id"] == suggestion_id]
                require(selected, "建议不在当前人物草稿中。")
            changed = False
            for claim in selected:
                if claim["decision"] != decision:
                    apply_decision(draft, claim, decision, plan_id)
                    claim["decision"] = decision
                    changed = True
            if changed:
                db.execute("UPDATE documents SET draft=?,revision=revision+1 WHERE id=?", (encode(drafts), ident))
        return self.library.status(ident)

    def start(self, ident, revision, plan_id, confirmed, *, cached_only=False):
        require(confirmed is True, "需要确认所选计划及其分析用途。")
        plan = self.load(ident, plan_id)
        require(ident not in self.tasks, "任务已受理。")
        with self.library.db() as db:
            changed = db.execute("""UPDATE documents SET status='running',revision=revision+1 WHERE id=? AND revision=?
                AND view IS NULL AND status NOT IN ('running','revoked') AND EXISTS(
                SELECT 1 FROM profile_current WHERE document=? AND plan=?)""",
                (ident, revision, ident, plan_id)).rowcount
            require(changed, "草稿已变化或任务已受理。")
        self.tasks[ident] = asyncio.create_task(self._run(ident, plan, cached_only))
        return self.library.status(ident)

    async def _run(self, ident, plan, cached_only):
        plan_id = plan["id"]

        def active(db):
            return db.execute("""SELECT draft FROM documents d JOIN profile_current c ON c.document=d.id
                                 WHERE d.id=? AND c.plan=? AND d.view IS NULL AND d.status='running'""",
                              (ident, plan_id)).fetchone()

        def guard():
            with self.library.db() as db:
                require(active(db), "任务已停止或来源已撤回。")
            return True

        def publish(batch_id, role, stage, state, accepted, rejected):
            with self.library.db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = active(db)
                require(row, "任务已停止或来源已撤回。")
                if stage == "extract":
                    previous = db.execute("""SELECT accepted FROM profile_results
                                             WHERE plan=? AND role=? AND batch=? AND stage='extract'""",
                                          (plan_id, role, batch_id)).fetchone()
                    old_ids = {c["id"] for c in json.loads(previous[0])} if previous else set()
                    if old_ids != {c["id"] for c in accepted}:
                        db.execute("""UPDATE profile_results SET state='superseded'
                                      WHERE plan=? AND role=? AND stage='merge'""", (plan_id, role))
                db.execute("INSERT OR REPLACE INTO profile_results VALUES(?,?,?,?,?,?,?)",
                           (plan_id, batch_id, role, stage, state, encode(accepted), encode(rejected)))
                drafts = json.loads(row["draft"])
                draft = drafts[role]
                rows = db.execute("""SELECT * FROM profile_results WHERE plan=? AND role=?
                                     ORDER BY stage,batch""", (plan_id, role)).fetchall()
                draft["claims"] = projected_claims(rows, draft["decisions"])
                draft["coverage"] = {
                    "eligible": plan["roles"][role]["eligible"],
                    "received": sum(r["stage"] == "extract" and r["state"] in ("accepted", "partial") for r in rows),
                }
                db.execute("UPDATE documents SET draft=? WHERE id=?", (encode(drafts), ident))

        final = "failed"
        try:
            await self.engine.run(ident, plan, cached_only=cached_only, publish=publish, guard=guard)
            final = "ready"
        except asyncio.CancelledError:
            final = "cancelled"
            raise
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            final = "failed"
        finally:
            with self.library.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if active(db):
                    states = db.execute("SELECT stage,state FROM profile_results WHERE plan=? AND state!='superseded'", (plan_id,)).fetchall()
                    any_good = any(s["state"] in ("accepted", "partial") for s in states)
                    all_done = (sum(s["stage"] == "extract" for s in states) == len(plan["batches"])
                                and all(s["state"] == "accepted" for s in states))
                    if final != "cancelled":
                        if any_good and (not all_done or final != "ready"):
                            final = "partial_ready"
                        elif not any_good:
                            dispositions = {s["state"] for s in states}
                            if "unknown" in dispositions:
                                final = "unknown"
                            elif dispositions and dispositions <= {"unsent", "awaiting_approval"}:
                                final = "prepared"
                            else:
                                final = "failed"
                    db.execute("UPDATE documents SET status=?,revision=revision+1 WHERE id=?", (final, ident))
            self.tasks.pop(ident, None)

    async def cancel(self, ident):
        task = self.tasks.get(ident)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(ident, None)
            with self.library.db() as db:
                db.execute("""UPDATE documents SET status='cancelled', revision=revision+1
                              WHERE id=? AND status='running'""", (ident,))
        return self.library.status(ident)

    async def drain(self):
        await asyncio.gather(*(self.cancel(ident) for ident in tuple(self.tasks)))
