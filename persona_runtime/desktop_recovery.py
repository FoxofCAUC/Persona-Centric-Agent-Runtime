"""Durable ownership and restart evidence for local desktop turns."""
import json

from .archive import binding, fingerprint, resolve, scope
from .config import atomic_json
from .models import RuntimeErrorBase, identifier
from .scene_policy import read, save


def model_binding(config):
    material = [config["instance_id"], config["model"], config["character_selector"]]
    return fingerprint(material)


def retry_remaining(attempts):
    from .generation_retry import MAX_RETRIES, retryable
    if not attempts:
        return True
    if attempts[-1]["index"] >= MAX_RETRIES:
        return False
    for item in attempts:
        diagnostics = item.get("diagnostics") or {}
        if (item.get("status") != "failed" or not retryable(item.get("error_code") or "")
                or diagnostics.get("effect_unknown") or (diagnostics.get("request_headers_sent") is not False
                and diagnostics.get("provider_terminal_error") is not True)):
            return False
    return True


def register(db, turn_id, config):
    save(db, "desktop.recovery." + turn_id, {
        "version": 1, "instance": config["instance_id"], "revision": 1,
        "phase": "ready", "owner": None, "generation": 0,
    })


def finish(db, turn_id):
    db.execute("DELETE FROM knowledge_control WHERE key=?", ("desktop.recovery." + turn_id,))
    db.execute("DELETE FROM knowledge_control WHERE key=?", ("desktop.network." + turn_id,))


def contract(db, turn):
    value = read(db, "desktop.recovery." + turn["id"])
    if value and value.get("version") == 1 and value.get("revision") == turn["execution_revision"]:
        return value
    return None


def claim(db, turn, config, owner):
    state = contract(db, turn)
    if not state:
        return True
    if state["instance"] != config["instance_id"]:
        raise RuntimeErrorBase("desktop_instance_changed")
    if state["owner"] is not None:
        return False
    validate_turn(db, turn)
    from .instance import current_process_identity
    state.update(owner=owner, process=current_process_identity(), phase="claimed", generation=state["generation"] + 1)
    save(db, "desktop.recovery." + turn["id"], state)
    return True


def validate_turn(db, turn):
    from .persona_management import active
    from .sessions import assert_writable
    active(db, turn["persona_id"])
    assert_writable(db, turn["session_id"])
    if db.execute("""SELECT 1 FROM source_exclusions WHERE event_id=?
                     OR event_id IN (SELECT event_id FROM input_batch_members WHERE batch_id=?)""",
                  (turn["user_event_id"], turn["input_batch_id"])).fetchone():
        raise RuntimeErrorBase("desktop_source_withdrawn")


def transition(db, turn, owner, phase, **evidence):
    state = contract(db, turn)
    if state:
        if state["owner"] != owner:
            raise RuntimeErrorBase("desktop_owner_changed")
        state.update(phase=phase, **evidence)
        save(db, "desktop.recovery." + turn["id"], state)


def release_for_continuation(db, turn, owner):
    state = contract(db, turn)
    if state:
        if state["owner"] not in (owner,None):
            raise RuntimeErrorBase("desktop_owner_changed")
        state.update(phase="ready",owner=None,process=None,recovered=True)
        save(db,"desktop.recovery."+turn["id"],state)


def can_resume(db, turn, config):
    state = contract(db, turn)
    if not state or state["instance"] != config["instance_id"] or turn["cancelled_at"]:
        return False
    if state["phase"] == "main_pending" and turn["status"] == "running":
        previous = db.execute("SELECT usage_json FROM attempts WHERE id=? AND turn_id=?",
                              (state["attempt"], turn["id"])).fetchone()
        if previous is None:
            return False
        audit = json.loads(previous["usage_json"] or "{}")
        history = audit.get("generation_retry", {}).get("attempts", [])
        offline=audit.get('generation_retry',{}).get('connection_wait',{})
        if previous["usage_json"] is not None and not history and not (offline.get('headers_sent') is False and offline.get('wire')):
            return False
        if not retry_remaining(history):
            return False
        db.execute("UPDATE turns SET status='queued' WHERE id=?", (turn["id"],))
    else:
        if state["phase"] not in ("ready", "claimed", "preparing") or turn["status"] != "queued":
            return False
        if not state.get("attempt") and db.execute("SELECT 1 FROM attempts WHERE turn_id=?", (turn["id"],)).fetchone():
            return False
    for table in ("character_selection_attempts", "situation_observation_attempts"):
        if db.execute(f"""SELECT 1 FROM {table} WHERE turn_id=? AND execution_revision=?
                          AND status NOT IN ('completed')""", (turn["id"], turn["execution_revision"])).fetchone():
            return False
    route = read(db, "scene.route." + turn["id"])
    if route and route.get("started") is not None and not route.get("raw"):
        return False
    tasks = db.execute("""SELECT t.id,t.status,c.data_json FROM organization_tasks t
                          LEFT JOIN organization_contracts c ON c.task_id=t.id
                          WHERE json_extract(t.payload_json,'$.knowledge_processing.root_turn_id')=?""",
                       (turn["id"],)).fetchall()
    for task in tasks:
        if task["status"] == "completed":
            continue
        if task["status"] not in ("queued","running","waiting_quota") or not task["data_json"]:
            return False
        attempt = db.execute("""SELECT r.attempt_id receipt,a.error_code FROM organization_attempts a
                                LEFT JOIN organization_receipts r ON r.attempt_id=a.id
                                WHERE a.task_id=? ORDER BY a.rowid DESC LIMIT 1""",(task["id"],)).fetchone()
        details=json.loads(task["data_json"])
        if details.get("cancel_requested") or (attempt and not attempt["receipt"] and attempt["error_code"] not in ('organization_network_wait','organization_storage_wait')):
            return False
        if not attempt and details.get("phase") in ("legacy","unknown","dispatching"):
            return False
    state.update(owner=None, process=None, phase="ready", recovered=True)
    save(db, "desktop.recovery." + turn["id"], state)
    return True


def continuity(db, paths, *, initialize=False):
    key = "desktop.submission.continuity"
    witness = paths.home / "runtime/desktop-submission-continuity.json"
    expected = read(db, key)
    try:
        external = json.loads(witness.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        external = None
    if expected and external == expected:
        return expected
    if not initialize:
        return None
    marker = {"epoch": identifier(), "last": None}
    atomic_json(witness, marker)
    save(db, key, marker)
    return marker


def intent_binding(db, store, config, payload):
    from .context_policy import current
    from .profile import snapshot
    from .character import head
    from .sessions import owned, assert_writable
    from .persona_management import active
    from .knowledge_routing import configured_model
    pid, sid = payload["persona_id"], payload["session_id"]
    selected = scope(pid, "desktop")
    active(db, pid)
    owned(db, sid, selected)
    assert_writable(db, sid)
    try:
        router = configured_model(config.paths)
    except RuntimeErrorBase:
        router = None
    reference = None
    if payload.get("reference"):
        result = resolve(db, selected, payload["reference"])
        if result["status"] != "found":
            raise RuntimeErrorBase("reference_unavailable")
        reference = binding(result)
        event_id = payload["reference"].get("id","")
        if payload["reference"].get("kind")=="event" and event_id.startswith("imported."):
            from .import_history import history_allowed
            source = db.execute("SELECT session_id FROM events WHERE id=? AND persona_id=?",
                                (event_id,pid)).fetchone()
            if not source or not history_allowed(db,source["session_id"],"dialogue"):
                raise RuntimeErrorBase("reference_unavailable")
    policy = current(db, pid, "desktop", store)
    sources = read(db, "persona.import_history." + sid)
    return fingerprint([model_binding(config.load()), router, policy, snapshot(db,pid),
                        head(db,pid)["id"], reference, sources, payload])


def issue_intent(store, config, payload, *, activate=False):
    from .migration import ensure_cloud_ready
    ensure_cloud_ready(store.paths)
    with store.connection(write=True) as db:
        from .local_repair import require_open
        require_open(store.paths)
        marker = continuity(db, store.paths, initialize=True)
        ticket={"epoch": marker["epoch"], "binding": intent_binding(db, store, config, payload),
                "instance": config.load()["instance_id"]}
        if activate and store.current(db,required=False)!=payload["persona_id"]:
            from .private_personas import guard_acceptance_write
            guard_acceptance_write(store,payload["persona_id"])
            db.execute("INSERT INTO settings VALUES ('current_persona',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (payload["persona_id"],))
        return ticket


def verify_intent(db, store, config, payload, ticket):
    marker = continuity(db, store.paths)
    if not marker or not isinstance(ticket, dict) or ticket.get("epoch") != marker["epoch"]:
        raise RuntimeErrorBase("desktop_continuity_unconfirmed")
    if ticket.get("instance") != config.load()["instance_id"] or ticket.get("binding") != intent_binding(db, store, config, payload):
        raise RuntimeErrorBase("desktop_intent_changed")
    return marker


def record_acceptance(db, paths, marker):
    witness = dict(marker, last=identifier())
    atomic_json(paths.home / "runtime/desktop-submission-continuity.json", witness)
    save(db, "desktop.submission.continuity", witness)


def lookup_intent(db, payload):
    rid = payload["request_id"]
    receipt = db.execute("""SELECT r.id receipt_id,r.request_id,r.event_id,r.batch_id,r.session_id,r.persona_id,
                            r.fingerprint,r.channel,b.state batch_state,t.id turn_id,t.status turn_state
                            FROM ingress_receipts r JOIN input_batches b ON b.id=r.batch_id
                            LEFT JOIN turns t ON t.input_batch_id=b.id WHERE r.channel='desktop' AND r.request_id=?""",
                         (rid,)).fetchone()
    old = db.execute("""SELECT t.id turn_id,t.session_id,t.persona_id,t.channel,t.status,e.text,e.id event_id
                        FROM turns t LEFT JOIN events e ON e.id=t.user_event_id WHERE t.source_key=?""",
                     ("desktop:" + rid,)).fetchone()
    row = receipt or old
    if row is None:
        return None
    if (receipt and old or any(payload.get(key) and payload[key] != row[key] for key in ("persona_id","session_id"))
            or row["channel"] != "desktop"):
        raise RuntimeErrorBase("submission_conflict")
    if receipt:
        expected = {k:v for k,v in payload.items() if k not in ("persona_id","session_id")}
        expected["channel"] = "desktop"
        if fingerprint(expected) != receipt["fingerprint"]:
            raise RuntimeErrorBase("submission_conflict")
        return {"receipt_id": receipt["receipt_id"], "batch_id": receipt["batch_id"], "event_id": receipt["event_id"],
                "turn_id": receipt["turn_id"], "status": receipt["turn_state"] or receipt["batch_state"]}
    from .knowledge_service import read as knowledge_read
    public = knowledge_read(db, "native.public_event." + (old["event_id"] or ""), {})["data"].get("task")
    if payload["text"] != old["text"] or payload.get("reference") or payload.get("public_task") != public:
        raise RuntimeErrorBase("submission_conflict")
    return {"turn_id": old["turn_id"], "event_id": old["event_id"], "status": old["status"]}
