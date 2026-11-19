"""Run frozen private import batches using separately reviewed extraction and merge requests."""
from .call_limits import resume_on_limit, external_gate, workflow
import asyncio
import json

from .archive import fingerprint
from .character_embedding import safe_path
from .corpus_files import digest
from .corpus_learning_transport import CorpusCallFailure, CorpusLearningHTTPClient
from .import_admission_v2 import InvalidObservation, admit_extraction, admit_merge
from .import_library import FIELDS, encode, require
from .import_prompts import contract_source, has_release_permission, release_model
from .models import timestamp
from .provider_compat import build_request, describe_request, usage_fields
from .provider_policy import require_generation_provider

VERSION = "pcar-dyadic-import-v2"


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _packet(role, batch_id, records):
    return {"target": role, "batch": batch_id, "records": records}


def _wire(model, system, payload):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": encode(payload)}]
    require(sum(len(m["content"]) for m in messages) <= model["max_input_chars"],
            "当前批次超出已冻结的输入容量。")
    return build_request(model, messages)[0]


def _descriptor(model, system, role, batch_id, stage, records):
    wire = _wire(model, system, _packet(role, batch_id, records))
    return {"id": batch_id, "role": role, "stage": stage, "wire_hash": fingerprint(wire), "wire": wire}


def _merge_input(observation):
    item = {key: observation[key] for key in ("id", "field", "value", "scope", "kind")}
    item["evidence"] = [
        {k: ref[k] for k in ("id", "start", "end", "quote")} for ref in observation["evidence"]
    ]
    return item


def _merge_batches(observations, capacity):
    # Keep distinct fields in stable form order. Only fields observed in multiple
    # extraction batches require reconciliation.
    for role in ("A", "B"):
        chunk = []
        used = 2
        selected = [o for o in observations if o["role"] == role]
        for field in FIELDS:
            same_field = [o for o in selected if o["field"] == field]
            if len({o["batch"] for o in same_field}) < 2:
                continue
            for observation in same_field:
                size = len(encode(_merge_input(observation)))
                if chunk and used + 1 + size > capacity:
                    yield role, chunk
                    chunk = []
                    used = 2
                # Oversized individual observations stay visible for review. A
                # blocked packet never displaces the other field batches.
                used += size + bool(chunk)
                chunk.append(observation)
        if chunk:
            yield role, chunk


class ImportAnalysisV2:
    def __init__(self, library, config, transport=None):
        self.library = library
        self.config = config
        self.transport = transport or CorpusLearningHTTPClient()

    def _private_file(self, name):
        root = self.library.root
        return safe_path(root.parent.parent, root / name)

    def _configuration(self):
        raw, release = contract_source(self.library, "profile")
        if raw is None:
            contract, checksum = {}, None
        else:
            contract = json.loads(raw, object_pairs_hook=_unique_keys)
            require(isinstance(contract, dict) and contract.get("version") == VERSION
                    and isinstance(contract.get("model"), str)
                    and isinstance(contract.get("extract_system"), str) and contract["extract_system"].strip()
                    and isinstance(contract.get("merge_system", ""), str), "提炼合同版本或用途无效。")
            checksum = digest(raw)
        model = self.config.load()["model"] | {"_json_output": True}
        if contract:
            model["name"] = contract["model"]
            if release:
                model = release_model(model)
            require_generation_provider(model)
        identity = {
            "version": VERSION, "configured": bool(contract), "contract_sha256": checksum,
            "model": model.get("name"), "base_url": model["base_url"], "max_tokens": model["max_tokens"],
            "max_input_chars": model["max_input_chars"], "thinking": describe_request(model)["thinking"],
        }
        if release:
            identity["prompt_release"] = release
        return contract, model, identity

    @staticmethod
    def _capacity(model, system):
        overhead = len(encode(_packet("A", "0" * 64, []))) - 2
        return model["max_input_chars"] - len(system) - overhead

    def settings(self):
        contract, model, identity = self._configuration()
        capacity = self._capacity(model, contract.get("extract_system", ""))
        require(capacity >= 2, "模型输入容量不足。")
        return identity, capacity

    def _frozen(self, plan):
        contract, model, current = self._configuration()
        require(plan.get("settings") == current, "模型参数或提炼合同已变化，请重新规划。")
        return contract, model, current["contract_sha256"]

    def _permission(self, ident, plan, checksum, desc):
        if plan["settings"].get("prompt_release"):
            allowed = desc["stage"] in ("extract", "merge") and has_release_permission(
                self.library, "profile", plan["settings"], checksum)
            return allowed, allowed
        try:
            approval = json.loads(self._private_file("approval-v2.json").read_bytes(), object_pairs_hook=_unique_keys)
        except (OSError, ValueError):
            return False, False
        if not isinstance(approval, dict):
            return False, False
        scope = {"document": ident, "plan": plan["id"], "source_id": plan["source_id"],
                 "base_url": plan["settings"]["base_url"], "model": plan["settings"]["model"]}
        if any(name in approval and approval[name] != value for name, value in scope.items()):
            return False, False
        hashes = approval.get("request_hashes")
        purposes = approval.get("purposes")
        reviewed_contract = (bool(checksum) and approval.get("decision") == "approved"
                             and approval.get("contract_sha256") == checksum and isinstance(purposes, list)
                             and desc["stage"] in purposes)
        if approval.get("scope") == "user_confirmed_import":
            allowed = (reviewed_contract and all(approval.get(key) == scope[key] for key in ("base_url", "model"))
                       and not any(key in approval for key in ("document", "plan", "source_id", "request_hashes")))
        else:
            allowed = reviewed_contract and isinstance(hashes, list) and desc["wire_hash"] in hashes
        return allowed, allowed and approval.get("dispatch_closed", False) is False

    def preview(self, ident, plan):
        from .import_plan import wire_records

        contract, model, _ = self._frozen(plan)
        if not contract:
            return []
        return [_descriptor(model, contract["extract_system"], b["role"], b["id"], "extract", wire_records(b))
                for b in plan["batches"]]

    def _merges(self, plan, observations):
        contract, model, _ = self._frozen(plan)
        system = contract.get("merge_system", "")
        capacity = self._capacity(model, system)
        for role, chunk in _merge_batches(observations, capacity):
            batch_id = fingerprint([VERSION, plan["id"], "merge", role, [o["id"] for o in chunk]])
            data = [_merge_input(o) for o in chunk]
            if not system.strip() or len(encode(data)) > capacity:
                reason = "missing_template" if not system.strip() else "capacity"
                desc = {"id": batch_id, "role": role, "stage": "merge", "reason": reason}
            else:
                desc = _descriptor(model, system, role, batch_id, "merge", data)
            yield desc, chunk

    def preview_merge(self, ident, plan, observations):
        return [descriptor for descriptor, _ in self._merges(plan, observations)]

    def _request_identity(self, plan, desc):
        _, model, checksum = self._frozen(plan)
        wire = desc["wire"]
        rebuilt, _ = build_request(model, wire["messages"])
        require(rebuilt == wire and fingerprint(rebuilt) == desc["wire_hash"],
                "模型请求与冻结的内容不一致。")
        return model, checksum

    @resume_on_limit
    async def _receive(self, ident, plan, desc, cached_only, guard):
        model, checksum = self._request_identity(plan, desc)
        allowed, dispatchable = self._permission(ident, plan, checksum, desc)
        if not allowed:
            return "awaiting_approval", None
        guard()
        key = fingerprint([VERSION, ident, plan["source_id"], checksum, model["base_url"], desc["stage"], desc["wire"]])
        with self.library.db() as db:
            db.execute("BEGIN IMMEDIATE")
            doc = db.execute("SELECT status FROM documents WHERE id=?", (ident,)).fetchone()
            require(doc and doc[0] == "running", "提炼任务已停止。")
            old = db.execute("SELECT state,raw FROM calls WHERE id=?", (key,)).fetchone()
            sending = old is None or (old["raw"] is None and old["state"] == "unsent")
            if sending:
                if not dispatchable or cached_only or not model.get("encrypted_key"):
                    return "unsent" if cached_only else "awaiting_approval", None
                external_gate(self,'import','import/'+ident)
                db.execute("INSERT OR REPLACE INTO calls VALUES(?,?,'sending',NULL,NULL,NULL,?)",
                           (key, ident, timestamp()))
            elif old["raw"] is None:
                return "unknown", None

        if sending:
            dispatch_metadata = {"stage": desc["stage"], "role": desc["role"], "batch": desc["id"],
                                 "wire_hash": desc["wire_hash"], "model": model["name"],
                                 "max_tokens": model["max_tokens"], "timeout": model["timeout"]}

            def save(raw, *, http_status, diagnostics):
                metadata = diagnostics | dispatch_metadata
                with self.library.db() as db:
                    db.execute("""UPDATE calls SET state='received',raw=?,status=?,metadata=?
                                  WHERE id=? AND raw IS NULL""", (raw, http_status, encode(metadata), key))

            # The guard checks the active source snapshot again after reservation.
            try:
                guard()
                self._request_identity(plan, desc)
                allowed, dispatchable = self._permission(ident, plan, checksum, desc)
                require(allowed and dispatchable, "本批次派发范围已变化。")
            except (ValueError, OSError):
                self._mark_unreceived(key, "unsent", dispatch_metadata | {"request_headers_sent": False})
                raise
            try:
                await self.transport.complete(model, desc["wire"], save_raw=save)
            except (CorpusCallFailure, asyncio.CancelledError) as exc:
                diagnostics = getattr(exc, "diagnostics", {})
                sent = diagnostics.get("request_headers_sent", True)
                self._mark_unreceived(key, "unknown" if sent else "unsent", diagnostics | dispatch_metadata)
                if isinstance(exc, asyncio.CancelledError):
                    raise

        guard()
        self._request_identity(plan, desc)
        if not self._permission(ident, plan, checksum, desc)[0]:
            return "awaiting_approval", None
        with self.library.db() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (key,)).fetchone()
            if row["raw"] is None:
                return ("unsent" if row["state"] == "unsent" else "unknown"), None
            try:
                response = json.loads(row["raw"], object_pairs_hook=_unique_keys)
                usage = usage_fields(response.get("usage"), deepseek=True)
                metadata = json.loads(row["metadata"])
                db.execute("UPDATE calls SET metadata=? WHERE id=?", (encode(metadata | {"usage": usage}), key))
            except (ValueError, TypeError, AttributeError, RecursionError):
                raise InvalidObservation("http_status" if row["status"] != 200 else "receipt") from None
        # Commit any provider-reported usage even when the body cannot be used.
        if row["status"] != 200:
            raise InvalidObservation("http_status")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InvalidObservation("receipt")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise InvalidObservation("incomplete")
        try:
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise InvalidObservation("empty")
            value = json.loads(content, object_pairs_hook=_unique_keys)
        except InvalidObservation:
            raise
        except (ValueError, KeyError, TypeError, RecursionError):
            raise InvalidObservation("json") from None
        return "received", value

    def _mark_unreceived(self, key, state, metadata):
        with self.library.db() as db:
            db.execute("UPDATE calls SET state=?,metadata=? WHERE id=? AND raw IS NULL",
                       (state, encode(metadata), key))

    async def _process(self, ident, plan, desc, admit, cached_only, publish, guard):
        guard()
        accepted, rejected = [], []
        try:
            state, value = await self._receive(ident, plan, desc, cached_only, guard)
            if state == "received":
                accepted, rejected = admit(value)
                state = "accepted" if not rejected else "partial" if accepted else "rejected"
        except InvalidObservation as exc:
            state = "rejected"
            rejected = [{"reason": str(exc)}]
        guard()
        _, checksum = self._request_identity(plan, desc)
        if state in ("accepted", "partial", "rejected") and not self._permission(ident, plan, checksum, desc)[0]:
            accepted, rejected, state = [], [], "awaiting_approval"
        publish(desc["id"], desc["role"], desc["stage"], state, accepted, rejected)
        return accepted

    @workflow
    async def run(self, ident, plan, *, cached_only, publish, guard):
        guard()
        descriptors = self.preview(ident, plan)
        require(descriptors, "请准备本实例的新版提炼合同及审定记录。")
        observations = []
        for batch, desc in zip(plan["batches"], descriptors, strict=True):
            await asyncio.sleep(0)
            admitted = await self._process(ident, plan, desc, lambda data: admit_extraction(data, batch),
                                           cached_only, publish, guard)
            observations.extend(admitted)
        for desc, chunk in self._merges(plan, observations):
            guard()
            if "reason" in desc:
                state = "awaiting_approval" if desc["reason"] == "missing_template" else "rejected"
                publish(desc["id"], desc["role"], "merge", state, [], [{"reason": desc["reason"]}])
                continue
            await self._process(ident, plan, desc,
                                lambda data: admit_merge(data, desc["role"], desc["id"], chunk),
                                cached_only, publish, guard)
