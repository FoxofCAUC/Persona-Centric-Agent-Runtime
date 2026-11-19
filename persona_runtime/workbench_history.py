"""Read-only desktop history with explicit persona and session scope."""

from contextlib import contextmanager, closing
import sqlite3

from .chat import ChatEngine
from .models import LOCAL_USER_ID, RuntimeErrorBase
from .persona_management import active
from .storage import Store

PAGE_SIZE = 60
DESKTOP_SESSION = """
    s.user_id=? AND l.deleted_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.channel='qq')
    AND NOT EXISTS (SELECT 1 FROM channel_sessions c WHERE c.session_id=s.id AND c.channel='qq')
    AND NOT EXISTS (SELECT 1 FROM session_lifecycle sl WHERE sl.session_id=s.id AND sl.channel='qq')
"""
VISIBLE_TURN = "persona_id=? AND session_id=? AND channel='desktop' AND source_key NOT LIKE 'imported.%'"
UNLINKED_BATCH = """
    b.persona_id=? AND b.session_id=? AND b.user_id=? AND b.channel='desktop'
    AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.input_batch_id=b.id)
"""


@contextmanager
def open_history(store):
    """Open a dedicated read transaction on the existing database."""
    with closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        Store._check(db)
        yield db


def display_messages(turn):
    messages = []

    def add(side, row, fallback, default_at):
        if row.get("text"):
            marker = row.get("event_id") or row.get("id") or fallback
            messages.append({
                "id": marker, "side": side, "text": row["text"],
                "at": row.get("created_at") or default_at, "turn": turn["id"],
            })

    created = turn["created_at"]
    finished = turn["updated_at"]
    for index, member in enumerate(turn.get("input_members") or [{"text": turn["text"], "id": turn.get("user_event_id")}]):
        add("user", member, (turn["id"], "user", index), created)
    if turn.get("search_notice"):
        add("persona", turn["search_notice"], (turn["id"], "notice"), created)
    speech = turn.get("scene_speech")
    if speech is not None:
        for index, fragment in enumerate(speech["visible"]):
            add("persona", fragment, (turn["id"], "scene", index), fragment.get("presented_at") or finished)
    elif turn["status"] == "completed":
        for index, message in enumerate(turn.get("display_messages") or [{"text": turn["display_reply"]}]):
            add("persona", message, (turn["id"], "reply", index), finished)
    return messages


def require_session(db, persona_id, session_id):
    row = db.execute(
        "SELECT s.id, sl.state, sh.session_id AS current FROM sessions s "
        "LEFT JOIN persona_lifecycle l ON l.persona_id=s.persona_id "
        "LEFT JOIN session_lifecycle sl ON sl.session_id=s.id "
        "LEFT JOIN session_heads sh ON sh.persona_id=s.persona_id AND sh.user_id=s.user_id "
        "AND sh.channel='desktop' AND sh.bot='' AND sh.peer='' "
        "WHERE s.id=? AND s.persona_id=? AND " + DESKTOP_SESSION,
        (session_id, persona_id, LOCAL_USER_ID),
    ).fetchone()
    if row is None:
        raise RuntimeErrorBase("未找到所选人物的桌面会话。")
    return row


class WorkbenchHistory:
    def __init__(self, paths):
        self._store = Store(paths)

    def people(self):
        if not self._store.path.is_file():
            return {"people": [], "selected": None, "waiting": True}
        with open_history(self._store) as db:
            current = Store.current(db, required=False)
            people = [dict(row) for row in db.execute(
                "SELECT p.id, p.name FROM personas p LEFT JOIN persona_lifecycle l ON l.persona_id=p.id "
                "WHERE l.deleted_at IS NULL ORDER BY p.created_at, p.id"
            )]
            selected = next((person["id"] for person in people if person["id"] == current), None)
            return {"people": people, "selected": selected, "waiting": False}

    def sessions(self, persona_id):
        with open_history(self._store) as db:
            active(db, persona_id)
            sessions = [dict(row) for row in db.execute(
                "SELECT s.id, s.created_at, EXISTS (SELECT 1 FROM channel_sessions c "
                "WHERE c.session_id=s.id AND c.persona_id=s.persona_id AND c.user_id=s.user_id "
                "AND c.channel='desktop') AS selected "
                "FROM sessions s LEFT JOIN persona_lifecycle l ON l.persona_id=s.persona_id "
                "WHERE s.persona_id=? AND " + DESKTOP_SESSION +
                " ORDER BY selected DESC, s.created_at DESC, s.rowid DESC",
                (persona_id, LOCAL_USER_ID),
            )]
            return sessions

    def page(self, persona_id, session_id, before=None):
        with open_history(self._store) as db:
            require_session(db, persona_id, session_id)
            scope = (persona_id, session_id)
            query = "SELECT id FROM turns WHERE " + VISIBLE_TURN
            if before is not None:
                anchor = db.execute(
                    "SELECT rowid FROM turns WHERE " + VISIBLE_TURN + " AND id=?", (*scope, before)
                ).fetchone()
                if not anchor:
                    raise RuntimeErrorBase("历史分页位置已变化，请重新打开会话。")
                query += " AND rowid<?"
                scope = (*scope, anchor[0])
            rows = db.execute(query + " ORDER BY rowid DESC LIMIT ?", (*scope, PAGE_SIZE + 1)).fetchall()
            messages = []
            for row in reversed(rows[:PAGE_SIZE]):
                messages.extend(display_messages(ChatEngine._view(db, row["id"])))
            return {"messages": messages, "before": rows[PAGE_SIZE - 1]["id"] if len(rows) > PAGE_SIZE else None}

    def activity(self, persona_id, session_id):
        with open_history(self._store) as db:
            session = require_session(db, persona_id, session_id)
            scope = (persona_id, session_id)
            stamp = tuple(db.execute(
                "SELECT coalesce(max(rowid),0), coalesce(max(updated_at),'') FROM turns WHERE " + VISIBLE_TURN,
                scope,
            ).fetchone())
            progress = db.execute(
                "SELECT coalesce(max(a.seq),0) FROM request_activity a JOIN turns t ON t.id=a.turn_id "
                "WHERE t.persona_id=? AND t.session_id=? AND t.channel='desktop'", scope,
            ).fetchone()[0]
            turn = db.execute(
                "SELECT id, status FROM turns WHERE " + VISIBLE_TURN +
                " AND status IN ('running','queued') AND cancelled_at IS NULL ORDER BY rowid LIMIT 1", scope,
            ).fetchone()
            latest = db.execute(
                "SELECT status, error_code='cancelled_by_user' AS stopped, "
                "(error_code IN ('model_not_configured','provider_official_deepseek_required','system_proxy_required') "
                "OR error_code LIKE 'provider_http_%' OR error_code IN "
                "('provider_timeout','provider_connection_timeout','provider_network_error')) AS connection_error FROM turns WHERE " +
                VISIBLE_TURN + " ORDER BY rowid DESC LIMIT 1", scope,
            ).fetchone()
            batch = db.execute(
                "SELECT b.id, b.state, b.revision FROM input_batches b WHERE " + UNLINKED_BATCH +
                " AND b.state IN ('collecting','interrupted_buffer','sealed_waiting_capacity') ORDER BY b.first_seq LIMIT 1",
                (*scope, LOCAL_USER_ID),
            ).fetchone()
            members = [dict(row) | {"side": "user", "turn": None} for row in db.execute(
                "SELECT e.id, e.text, e.created_at AS at FROM input_batches b "
                "JOIN input_batch_members m ON m.batch_id=b.id AND m.persona_id=b.persona_id AND m.user_id=b.user_id "
                "JOIN events e ON e.id=m.event_id AND e.session_id=b.session_id AND e.persona_id=b.persona_id "
                "AND e.user_id=b.user_id WHERE " + UNLINKED_BATCH +
                " ORDER BY b.first_seq DESC, m.member_order DESC LIMIT 200",
                (*scope, LOCAL_USER_ID),
            )]
            # A linked batch's bubbles now come from its turn and retain their event IDs.
            return {
                "selected": Store.current(db, required=False) == persona_id,
                "writable": session["state"] != "sealed" and session["current"] in (None, session_id),
                "turn": dict(turn) if turn else None,
                "latest": dict(latest) if latest else None,
                "batch": dict(batch) if batch else None,
                "members": list(reversed(members)),
                "version": [*stamp, progress],
            }
