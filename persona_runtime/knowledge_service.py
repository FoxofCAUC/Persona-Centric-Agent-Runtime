"""Authority-backed package selection, explicit reader grants and live invalidation."""

from contextlib import closing
import copy
import hashlib
import json
import math
import re
import sqlite3
import threading

from .archive import fingerprint
from .character_embedding import safe_path
from .knowledge_package import DATA_REVISION, MODEL, PROGRAM_VERSION, PackageInstaller, file_digest
from .models import RuntimeErrorBase, require_text


PROGRAM_SHA256 = "e6d7406c317eec4155c8d3f03bd5591d226b9aa432b6026cd597d3a2f11b0c04"


REGISTRY = {"active": None, "previous": None, "packages": {}}
POLICY = {"enabled": False, "channels": ["desktop", "qq"]}
SOURCES = {"wikipedia_zh": {"enabled": True}, "wikidata": {"enabled": True}}


def read(conn, key, default):
    row = conn.execute("SELECT value FROM knowledge_control WHERE key=?", ("knowledge." + key,)).fetchone()
    return json.loads(row[0]) if row else {"revision": 0, "data": copy.deepcopy(default)}


def package_path(paths, record):
    root = paths.home / "data/knowledge/packages"
    return safe_path(root, root / record["directory"])


def seal(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def intact(paths, record):
    try:
        directory = package_path(paths, record)
        return all(seal(safe_path(directory, directory / name)) == expected
                   for name, expected in record["files"].items())
    except (OSError, ValueError):
        return False


def permitted(conn, persona_id, channel, package_sha256, article_id):
    from .knowledge_store import enabled
    packages = read(conn, "registry", REGISTRY)["data"]["packages"]
    invalid = read(conn, "invalidations", {})["data"].get(package_sha256, {})
    return bool(enabled(conn, persona_id, channel, 'wikipedia_zh')
                and package_sha256 in packages and "0" not in invalid
                and str(article_id) not in invalid)


def evidence_permitted(conn, persona_id, channel, item):
    if item.get('kind') == 'knowledge_claim':
        from .knowledge_store import evidence_usable
        return evidence_usable(conn, persona_id, channel, item)
    return permitted(conn, persona_id, channel, item['package_sha256'], item['article_id'])


def validate_used(conn, paths, persona_id, channel, knowledge):
    if (knowledge or {}).get('processing'):
        from .knowledge_processing import record_used
        record_used(conn,paths,persona_id,channel,knowledge['processing'])
    for item in (knowledge or {}).get("evidence", []):
        if item.get('kind') == 'knowledge_claim':
            if not evidence_permitted(conn, persona_id, channel, item):
                raise RuntimeErrorBase('knowledge_evidence_changed')
            continue
        identity = item["package_sha256"]
        record = read(conn, "registry", REGISTRY)["data"]["packages"].get(identity)
        if (not permitted(conn, persona_id, channel, identity, item["article_id"])
                or not record or not intact(paths, record)):
            raise RuntimeErrorBase("knowledge_evidence_changed")


def check_batch(conn, paths, batch_id):
    row = conn.execute("SELECT b.persona_id,t.channel,u.details_json FROM reply_batches b "
        "JOIN turns t ON t.id=b.turn_id JOIN context_uses u ON u.attempt_id=b.attempt_id "
        "WHERE b.id=?", (batch_id,)).fetchone()
    if row:
        validate_used(conn, paths, row["persona_id"], row["channel"],
                      json.loads(row["details_json"]).get("knowledge"))


def turn_usable(conn, turn_id):
    row = conn.execute("SELECT t.persona_id,t.channel,u.details_json FROM turns t "
        "JOIN context_uses u ON u.turn_id=t.id WHERE t.id=? ORDER BY u.rowid DESC LIMIT 1",
        (turn_id,)).fetchone()
    if not row:
        return True
    knowledge=json.loads(row['details_json']).get('knowledge',{})
    if knowledge.get('processing'):
        try:
            # Path ownership is checked by the normal caller's Store. These
            # readonly metadata checks use the connection's current authority.
            record=read(conn,'processing.record.'+knowledge['processing']['record_id'],{})['data']
            policy=read(conn,'processing.'+row['persona_id'],{})
            if (not record or record['persona_id']!=row['persona_id'] or record['channel']!=row['channel']
                    or record['policy']!=policy or not policy['data'].get('enabled')
                    or fingerprint(record['result'])!=knowledge['processing']['result_hash']):return False
        except (KeyError,ValueError):return False
    return all(evidence_permitted(conn,row['persona_id'],row['channel'],item) for item in knowledge.get('evidence',[]))


def should_retrieve(text):
    """Local public-topic cues. No input leaves the machine through this rule."""
    personal = r"^(?:你|我|咱们|我們|我们)(?:是誰|是谁|叫什|喜欢|喜歡|爱不爱|愛不愛|记得|記得)"
    question = (r"什[么麼麽]|为什么|為什麼|为何|為何|如何|怎样|怎樣|怎么|怎麼|哪[里裡个個些]|"
                r"谁|誰|多少|多大|何时|何時|几点|幾點|区别|區別|原理|解释|解釋|介绍|介紹|科普|"
                r"查[询詢证證]|查一下|讲讲|講講|聊聊.{2,20}(?:历史|歷史|原理|科学|科學)")
    topic = r"(?:在看|在读|在讀|研究|学习|學習|讨论|討論|说到|說到).{2,60}"
    return bool(not re.search(personal, text.strip()) and re.search(question + "|" + topic, text))


class KnowledgeService:
    def __init__(self, store, retriever_factory=None):
        self.store = store
        self.paths = store.paths
        self.factory = retriever_factory
        self.lock = threading.RLock()
        self.backend = None
        self.readers = 0
        self.maintenance = None
        self.processing = None
        self.closed = threading.Event()

    def _update(self, key, data, expected_revision, operation_id, *, request=None, apply=None):
        require_text(operation_id, "operation_id")
        if len(operation_id) > 128 or type(expected_revision) is not int or expected_revision < 0:
            raise RuntimeErrorBase("knowledge_operation_invalid")
        request_hash = fingerprint([key, expected_revision, data if request is None else request])
        with self.store.connection(write=True) as conn:
            operation = conn.execute("SELECT value FROM knowledge_control WHERE key=?",
                                     ("knowledge.operation." + operation_id,)).fetchone()
            if operation:
                saved = json.loads(operation[0])
                if saved["hash"] != request_hash:
                    raise RuntimeErrorBase("operation_id_conflict")
                return saved["result"]
            current = read(conn, key, {})
            if current["revision"] != expected_revision:
                raise RuntimeErrorBase("version_conflict")
            if apply:
                apply(conn)
            result = {"revision": expected_revision + 1, "data": data}
            for name, value in (("knowledge." + key, result),
                    ("knowledge.operation." + operation_id, {"hash": request_hash, "result": result})):
                conn.execute("INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) "
                             "DO UPDATE SET value=excluded.value", (name, json.dumps(value)))
            return result

    def operation_show(self, operation_id):
        require_text(operation_id, 'operation_id')
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM knowledge_control WHERE key=?',
                             ('knowledge.operation.' + operation_id,)).fetchone()
        return {'state': 'completed', 'result': json.loads(row[0])['result']} if row else {'state': 'not_found'}

    def _receipt(self, key, expected_revision, operation_id, request):
        require_text(operation_id, 'operation_id')
        if type(expected_revision) is not int or expected_revision < 0 or len(operation_id) > 128:
            raise RuntimeErrorBase('knowledge_operation_invalid')
        with self.store.connection() as db:
            row = db.execute('SELECT value FROM knowledge_control WHERE key=?',
                             ('knowledge.operation.' + operation_id,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if saved['hash'] != fingerprint([key, expected_revision, request]):
                    raise RuntimeErrorBase('operation_id_conflict')
                return saved['result']
            if read(db, key, {})['revision'] != expected_revision:
                raise RuntimeErrorBase('version_conflict')
        return None

    def current_policy(self, persona_id):
        from .service import RuntimeService
        with self.store.connection() as conn:
            RuntimeService._persona(conn, persona_id)
            return read(conn, "policy." + persona_id, POLICY)

    def update_policy(self, persona_id, data, expected_revision, operation_id):
        from .private_personas import guard_acceptance_write
        guard_acceptance_write(self.store, persona_id)
        self.current_policy(persona_id)
        if (not isinstance(data, dict) or set(data) != set(POLICY)
                or type(data["enabled"]) is not bool or not isinstance(data["channels"], list)
                or not data["channels"] or len(data["channels"]) != len(set(data["channels"]))
                or not set(data["channels"]) <= {"desktop", "qq"}):
            raise RuntimeErrorBase("knowledge_policy_invalid")
        return self._update("policy." + persona_id, data, expected_revision, operation_id)

    def sources(self):
        with self.store.connection() as conn:
            return read(conn, "sources", SOURCES)

    def update_sources(self, data, expected_revision, operation_id):
        if (not isinstance(data, dict) or set(data) != set(SOURCES)
                or any(not isinstance(value, dict) or set(value) != {"enabled"}
                       or type(value["enabled"]) is not bool for value in data.values())):
            raise RuntimeErrorBase("knowledge_sources_invalid")
        return self._update("sources", data, expected_revision, operation_id)

    def invalidate(self, package_sha256, article_id, state, reason, expected_revision, operation_id):
        if (not re.fullmatch(r"[a-f0-9]{64}", package_sha256) or type(article_id) is not int
                or article_id < 0 or state not in {"withdrawn", "corrected", "expired", "conflict"}):
            raise RuntimeErrorBase("knowledge_invalidation_invalid")
        require_text(reason, "reason")
        with self.store.connection() as conn:
            data = read(conn, "invalidations", {})["data"]
        data.setdefault(package_sha256, {})[str(article_id)] = {"state": state, "reason": reason[:500]}
        return self._update("invalidations", data, expected_revision, operation_id,
                            request=[package_sha256, article_id, state, reason])

    def prepare_package(self, expected_revision, operation_id):
        saved = self._receipt('preparation', expected_revision, operation_id, 'pinned_package')
        if saved is not None:
            return saved
        from .knowledge_admin import dependencies
        if not dependencies()['ready']:
            raise RuntimeErrorBase('knowledge_dependencies_missing')
        with self.store.connection() as conn:
            registry = read(conn, "registry", REGISTRY)["data"]
        active = registry["packages"].get(registry["active"])
        if active and intact(self.paths, active):
            manifest = active["manifest"]
        else:
            def progress(value):
                if self.closed.is_set():
                    raise RuntimeErrorBase("knowledge_stopped")
            manifest = PackageInstaller(self.paths, progress=progress).prepare()
            package = self.paths.home / "data/knowledge/packages" / ("staging-" + DATA_REVISION[:12])
            from .knowledge_lexical import ChineseLexicalIndex
            from .knowledge_titles import ChineseTitleIndex
            lexical = ChineseLexicalIndex(self.paths)
            lexical.prepare(package)
            ChineseTitleIndex(self.paths, package, lexical.text).prepare()
        return self._update("preparation", manifest, expected_revision, operation_id,
                            request="pinned_package")

    def activate_package(self, directory, expected_revision, operation_id):
        return self._activate_package(directory, expected_revision, operation_id, directory)

    def _activate_package(self, directory, expected_revision, operation_id, request):
        saved = self._receipt('registry', expected_revision, operation_id, request)
        if saved is not None:
            return saved
        require_text(directory, "directory")
        root = self.paths.home / "data/knowledge/packages"
        package = safe_path(root, root / directory)
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        source = safe_path(package, package / manifest["database"]["file"])
        executable = package / "wikilite.exe"
        identity = manifest["database"]["sha256"]
        if (manifest["state"] != "staged" or manifest["data_revision"] != DATA_REVISION
                or manifest["program_version"] != PROGRAM_VERSION
                or manifest["program_sha256"] != PROGRAM_SHA256
                or manifest["database"]["embedded_model"]["sha256"] != MODEL.sha256
                or file_digest(source) != identity
                or file_digest(executable) != manifest["program_sha256"]):
            raise RuntimeErrorBase("knowledge_package_verification_failed")
        with self.store.connection() as conn:
            registry = read(conn, "registry", REGISTRY)["data"]
        record = {"directory": package.relative_to(root.resolve()).as_posix(), "manifest": manifest,
                  "files": {name: seal(package / name) for name in
                            (source.name, "wikilite.exe", "manifest.json")}}
        registry["packages"][identity] = record
        if registry["active"] != identity:
            registry["previous"], registry["active"] = registry["active"], identity
        return self._update("registry", registry, expected_revision, operation_id, request=request)

    def status(self):
        with self.store.connection() as conn:
            registry = read(conn, "registry", REGISTRY)
            invalid = read(conn, "invalidations", {})
            preparation = read(conn, "preparation", {})
        with self.lock:
            backend = self.backend
            ready = bool(backend and backend.ready)
        progress = self.paths.home / 'runtime/knowledge-install.json'
        try:
            installation = json.loads(progress.read_text(encoding='utf-8')) if progress.exists() else None
        except (OSError, ValueError):
            installation = {'phase': 'unavailable'}
        return {"registry": registry, "sources": self.sources(), "invalidations": invalid,
                "preparation": preparation,
                "model_ready": ready, "state": "stopped" if self.closed.is_set() else "available",
                "installation": installation, "network_refresh": self.maintenance.status() if self.maintenance else None,
                "p95_target_seconds": None}

    def search(self, persona_id, query, channel="desktop", timeout=None, package_sha256=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise RuntimeErrorBase("knowledge_query_invalid")
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60):
            raise RuntimeErrorBase("knowledge_timeout_invalid")
        with self.store.connection() as conn:
            from .knowledge_store import search as search_overlay, merge
            overlay = search_overlay(conn, persona_id, channel, query)
            registry = read(conn, "registry", REGISTRY)["data"]
            identity = package_sha256 or registry["active"]
            if not identity or not permitted(conn, persona_id, channel, identity, -1):
                return merge({"state": "disabled", "evidence": []}, overlay)
            record = registry["packages"][identity]
            if not intact(self.paths, record):
                return merge({"state": "quarantined", "reason": "package_changed", "evidence": []}, overlay)
            invalid = read(conn, "invalidations", {})
            policy = read(conn, "policy." + persona_id, POLICY)
            sources = read(conn, "sources", SOURCES)
            denied = set(invalid["data"].get(identity, {}))
            scope_key = fingerprint([persona_id, channel, policy, sources, invalid])
        with self.lock:
            if self.closed.is_set():
                return {"state": "stopped", "evidence": []}
            if self.backend and self.backend.identity != identity:
                with self.backend.lock:
                    if self.readers or any(not future.done() for _, future in self.backend.cache.values()):
                        return {"state": "pending", "reason": "package_switch_wait", "package_sha256": identity, "evidence": []}
                self.backend.close()
                self.backend = None
            if self.backend is None:
                package = package_path(self.paths, record)
                if self.factory:
                    self.backend = self.factory(self.paths, package)
                else:
                    from .knowledge_lexical import ChineseLexicalIndex
                    from .knowledge_retrieval import KnowledgeRetriever
                    lexical = ChineseLexicalIndex(self.paths).prepare(package)
                    self.backend = KnowledgeRetriever(self.paths, package, lexical)
            backend = self.backend
            self.readers += 1
        try:
            result = backend.search(query, scope_key=scope_key,
                allowed=lambda row: str(row["article_id"]) not in denied, timeout=timeout)
        finally:
            with self.lock:
                self.readers -= 1
        result.setdefault("package_sha256", identity)
        with self.store.connection() as conn:
            result = self.recheck(conn, {"persona_id": persona_id, "channel": channel}, result)
            return merge(result, search_overlay(conn, persona_id, channel, query))

    def recheck(self, conn, turn, result):
        if result is None:
            return {"state": "disabled", "evidence": []}
        result = copy.deepcopy(result)
        identity = result.get("package_sha256")
        evidence = result.get("evidence", [])
        result["evidence"] = [item for item in evidence if evidence_permitted(conn, turn["persona_id"],
                              turn["channel"], item)]
        if identity and not permitted(conn, turn["persona_id"], turn["channel"], identity, -1):
            result = {'state': 'ready' if result['evidence'] else 'disabled', 'evidence': result['evidence'],
                      'candidates': [row for row in result.get('candidates',[]) if row.get('kind') == 'knowledge_claim']}
        try:
            validate_used(conn, self.paths, turn["persona_id"], turn["channel"], result)
        except RuntimeErrorBase:
            return {"state": "quarantined", "evidence": []}
        for key in ("candidates",):
            if key in result:
                result[key] = [row for row in result[key] if evidence_permitted(conn, turn["persona_id"],
                    turn["channel"], dict(row,package_sha256=identity))]
        if "channels" in result:
            result["channels"] = {name: [row for row in rows if permitted(conn, turn["persona_id"],
                turn["channel"], identity, row["article_id"])] for name, rows in result["channels"].items()}
        result.pop("lexical_details", None)
        return result

    def prepare_turn(self, turn):
        if turn["channel"] not in ("desktop", "qq"):
            return {"state": "disabled", "evidence": []}
        from .turn_input import load_turn_input
        with self.store.connection() as conn:
            policy = read(conn, "policy." + turn["persona_id"], POLICY)["data"]
            if not policy["enabled"] or turn["channel"] not in policy["channels"]:
                return {"state": "disabled", "evidence": []}
            query = "\n".join(item["text"] for item in load_turn_input(conn, turn))[-500:]
            key = "last." + fingerprint([turn["persona_id"], turn["channel"], turn["session_id"]])
            last = read(conn, key, {})
            followup = re.fullmatch(r"(?:继续|繼續|再讲讲|再講講|展开|展開|查到[了了吗嗎]*|"
                                    r"结果[呢嗎吗]*|結果[呢嗎]*|再详细[点些]|再詳細[點些])[吧呢吗嗎？?。！!\s]*", query)
            source_request = re.fullmatch(r'(?:给我|給我|发我|發我|给我看下|給我看看|看看)?(?:出处|出處|来源|來源)(?:是哪里|在哪|在哪裡|是什么|是什麼)?[呢吗嗎？?。！!\s]*',query)
            if followup and last["data"].get("query"):
                query = last["data"]["query"]
        if self.processing and self.processing.enabled(turn):
            return self.processing.prepare(turn,query,last['data'].get('turn_id') if (followup or source_request) else None)
        if source_request and last['data'].get('turn_id'):
            try:
                shown = self.show_evidence(turn['persona_id'],last['data']['turn_id'])
                if shown['evidence']:
                    return shown
                return {'state':'refresh_unavailable','reason':'referenced_evidence_changed','evidence':[]}
            except RuntimeErrorBase:
                return {'state':'refresh_unavailable','reason':'referenced_evidence_changed','evidence':[]}
        if not should_retrieve(query):
            return {"state": "not_triggered", "evidence": []}
        try:
            from .knowledge_wait import search_turn, refresh_turn
            result = search_turn(self, turn, query)
            if self.maintenance:
                latest = bool(re.search(r'最新|目前|现在|現在|今天|今日|刚刚|剛剛|查证|查證|核实|核實', query))
                refresh = self.maintenance.on_result(turn, result, latest)
                if latest and refresh.get('job_ids'):
                    refresh = refresh_turn(self, turn, refresh)
                if latest:
                    from .knowledge_store import search as search_overlay, merge
                    with self.store.connection() as conn:
                        current = search_overlay(conn, turn['persona_id'], turn['channel'], query, current=True)
                    if refresh['state'] == 'checked' and current:
                        result = merge({'state': 'ready', 'evidence': []}, current)
                    else:
                        result = {'state': 'refresh_pending' if refresh['state'] != 'disabled' else 'refresh_unavailable',
                                  'evidence': [], 'reason': 'current_evidence_not_ready'}
            if result["state"] in {"ready", "pending", "busy", "refresh_pending", "refresh_unavailable"}:
                with self.store.connection(write=True) as conn:
                    value = {"revision": last["revision"] + 1, "data": {
                        "query": query, "state": result["state"], "turn_id": turn["id"]}}
                    conn.execute("INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 ("knowledge." + key, json.dumps(value)))
            return {key: result[key] for key in ("state", "package_sha256", "request_key", "evidence",
                    "source_snapshot_at", "reason") if key in result}
        except RuntimeErrorBase:
            raise
        except (ValueError, OSError, sqlite3.Error, ImportError) as error:
            return {"state": "unavailable", "reason": type(error).__name__, "evidence": []}

    def show_evidence(self, persona_id, turn_id, context_id=None):
        with self.store.connection() as conn:
            turn = conn.execute("SELECT * FROM turns WHERE id=? AND persona_id=?",
                                (turn_id, persona_id)).fetchone()
            if not turn:
                raise RuntimeErrorBase("knowledge_turn_not_found")
            sql = "SELECT manifest_json FROM context_manifests WHERE turn_id=? AND persona_id=?"
            args = [turn_id, persona_id]
            if context_id:
                sql += " AND id=?"
                args.append(context_id)
            row = conn.execute(sql + " ORDER BY rowid DESC LIMIT 1", args).fetchone()
            knowledge = json.loads(row[0]).get("knowledge", {}) if row else {}
            validate_used(conn, self.paths, persona_id, turn["channel"], knowledge)
            registry = read(conn, "registry", REGISTRY)["data"]
            evidence = []
            for item in knowledge.get("evidence", []):
                if item.get('kind') == 'knowledge_claim':
                    document = conn.execute('SELECT body FROM knowledge_documents WHERE id=?',
                                            (item['document_id'],)).fetchone()
                    if not document or hashlib.sha256(document[0].encode()).hexdigest() != item['document_sha256']:
                        raise RuntimeErrorBase('knowledge_evidence_changed')
                    evidence.append({**item, 'quote': document[0][item['start']:item['end']]})
                    continue
                record = registry["packages"][item["package_sha256"]]
                directory = package_path(self.paths, record)
                database = safe_path(directory, directory / record["manifest"]["database"]["file"])
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
                    section = source.execute("SELECT article_id,content FROM sections WHERE id=?",
                                             (item["section_id"],)).fetchone()
                if (not section or section[0] != item["article_id"] or hashlib.sha256(
                        section[1].encode("utf-8")).hexdigest() != item["section_sha256"]):
                    raise RuntimeErrorBase("knowledge_evidence_changed")
                evidence.append({**item, "quote": section[1][item["start"]:item["end"]]})
            return {"state": "ready", "evidence": evidence}

    def close(self):
        self.closed.set()
        if self.maintenance:
            self.maintenance.close()
        with self.lock:
            if self.backend:
                self.backend.close()
