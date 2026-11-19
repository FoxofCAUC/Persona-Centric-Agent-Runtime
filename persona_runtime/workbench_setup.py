"""First-run setup and scoped local editing for the desktop workbench."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

from . import character, profile
from .automatic_memory import usable_fact
from .config import SPEECH_BASELINE_MODEL
from .desktop_client import LocalAPIError, TransportUnknown
from .migration import migration_pending
from .models import CATEGORIES, LOCAL_USER_ID, PersonaSpec, RuntimeErrorBase, identifier
from .private_personas import guard_acceptance_write
from .provider_compat import CURRENT_DEEPSEEK
from .provider_policy import require_generation_provider
from .service import RuntimeService
from .storage import Store
from .workbench_history import open_history, require_session


OFFICIAL_URL = "https://api.deepseek.com"
TEXT_MODELS = (SPEECH_BASELINE_MODEL, *sorted(
    model for model in CURRENT_DEEPSEEK if model != SPEECH_BASELINE_MODEL and "vision" not in model
))
PACKAGE_LIMIT = 256 * 1024
VERSION_PAGE = 25
EDIT_SECTIONS = (*profile.KINDS, "character")
EXTENSION_FIELDS = {"appearance": ("appearance", "description"),
                    "hobbies": ("interests", "hobbies"), "world": ("world", "description")}
CONNECTION_CODES = {
    "model_not_configured", "provider_official_deepseek_required", "provider_http_401",
    "provider_http_402", "provider_http_403", "provider_http_404", "provider_http_429",
    "provider_timeout", "provider_connection_timeout", "provider_network_error",
    "network_proxy_required", "system_proxy_required",
}


class SetupError(ValueError):
    """An expected setup error whose display code contains no user input."""


class WorkbenchSetup:
    ACTIONS = (
        "connection", "save_connection", "test_connection", "test_status",
        "preview_package", "discard_package", "create_role", "creation_status",
        "person", "select_person", "save_profile", "save_extensions", "append_material",
        "versions", "version_detail", "restore_version",
        "memories", "memory_sources", "add_memory", "change_memory", "memory_policy",
        "diagnostics",
    )

    def __init__(self, conversation, runtime_status):
        self.chat = conversation
        self.store = conversation.store
        self.config = conversation.config
        self.client = conversation.client
        self.runtime_status = runtime_status
        self.package = None

    def call(self, action, args):
        if action not in self.ACTIONS or not isinstance(args, dict):
            return {"ok": False, "error": "invalid_request"}
        try:
            return {"ok": True, "value": getattr(self, action)(**args)}
        except (SetupError, LocalAPIError) as error:
            # The UI translates only recognized codes.
            code = str(error)
            if isinstance(error, LocalAPIError) and code not in {
                "profile_version_conflict", "character_version_conflict", "version_conflict",
                "source_expired", "state_expiry_must_be_aware_future_within_7_days",
                "migration_configuration_required", "persona_selection_conflict",
                "character_selection_conflict", "profile_too_long", "submission_conflict",
                "too_many_lore_entries", "too_many_expression_examples", "character_too_large",
                "duplicate_character_value:keywords", "duplicate_character_value:example_keywords",
                "en5_desktop_ingress_required", "service_draining", "queue_full", "organization_model_changed",
            }:
                code = "operation_rejected"
            return {"ok": False, "error": code}
        except TransportUnknown:
            return {"ok": False, "error": "unconfirmed"}
        except (RuntimeErrorBase, OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            return {"ok": False, "error": "local_error"}

    @contextmanager
    def writing(self, persona_id=None):
        with self.chat.lock:
            if not self.chat.available():
                raise SetupError("service_waiting")
            try:
                self.client.call("GET", "/v1/health", timeout=2)
            except TransportUnknown:
                raise SetupError("service_waiting") from None
            if persona_id is not None:
                with open_history(self.store) as db:
                    if Store.current(db, required=False) != persona_id:
                        raise SetupError("select_person_first")
                guard_acceptance_write(self.store, persona_id)
            yield

    def connection(self):
        model = self.config.public()["model"]
        try:
            require_generation_provider(model | {"name": SPEECH_BASELINE_MODEL})
            official = True
        except ValueError:
            official = False
        return {
            "endpoint": OFFICIAL_URL, "name": model["name"], "choices": TEXT_MODELS,
            "has_key": model["has_key"], "legacy": bool(model["base_url"]) and (
                not official or model["name"] not in TEXT_MODELS
            ),
            "key_reusable": official or not model["has_key"],
            "configured": official and model["name"] in TEXT_MODELS and model["has_key"],
            "migration_pending": migration_pending(self.store.paths),
        }

    def save_connection(self, name, key_mode, key=""):
        if name not in TEXT_MODELS or key_mode not in ("keep", "replace", "clear"):
            raise SetupError("invalid_connection")
        if not isinstance(key, str) or key_mode != "replace" and key or (
            key_mode == "replace" and (not key or len(key) > 4096 or
                                       any(not 33 <= ord(c) <= 126 for c in key))
        ):
            raise SetupError("invalid_key")
        with self.writing():
            if key_mode == "keep" and not self.connection()["key_reusable"]:
                raise SetupError("replace_legacy_key")
            if key_mode == "keep":
                model = {"name": name}
            elif key_mode == "clear":
                model = {"api_key": ""}
            else:
                model = {"name": name, "base_url": OFFICIAL_URL, "api_key": key}
            self.client.call("POST", "/v1/config", {"model": model})
            return self.connection()

    def test_connection(self, request_id, confirmed):
        if confirmed is not True:
            raise SetupError("test_confirmation_required")
        operation_id(request_id)
        with self.writing():
            if not self.connection()["configured"]:
                raise SetupError("connection_required")
            self.client.call("POST", "/v1/model-test", {"request_id": request_id})
        return self.test_status(request_id)

    def test_status(self, request_id):
        operation_id(request_id)
        with open_history(self.store) as db:
            row = db.execute(
                "SELECT status,error_code FROM turns WHERE channel='test' AND source_key=?",
                ("test:" + request_id,),
            ).fetchone()
        if row is None:
            return {"status": "unknown"}
        return {"status": row["status"], "reason": safe_reason(row["error_code"])}

    def preview_package(self, text):
        if not isinstance(text, str):
            raise SetupError("invalid_package")
        try:
            if len(text.encode("utf-8")) > PACKAGE_LIMIT:
                raise SetupError("package_too_large")
            package = json.loads(text.removeprefix("\ufeff"))
            if isinstance(package, dict) and "format" in package:
                if set(package) != {"format", "persona_spec", "character"} or package["format"] != "pcar_character_v1":
                    raise SetupError("unsupported_package")
                spec = PersonaSpec.from_dict(package["persona_spec"]).to_dict()
                data = character.validate_character(package["character"])
            else:
                spec = PersonaSpec.from_dict(package).to_dict()
                data = character.empty_character()
        except (ValueError, RecursionError) as error:
            if isinstance(error, SetupError):
                raise
            raise SetupError("invalid_package") from None
        token = identifier()
        with self.chat.lock:
            self.package = (token, data)
        return {"token": token, "spec": spec, "materials": material_summary(data),
                "extensions": extensions_view(data)}

    def discard_package(self, token):
        with self.chat.lock:
            if self.package is not None and token == self.package[0]:
                self.package = None

    def create_role(self, spec, relationship, package_token, request_id, extensions=None):
        operation_id(request_id)
        try:
            spec = PersonaSpec.from_dict(spec).to_dict()
        except RuntimeErrorBase:
            raise SetupError("invalid_persona") from None
        if not isinstance(relationship, str) or len(profile.encode({"description": relationship})) > 16000:
            raise SetupError("profile_too_long")
        with self.writing():
            data = character.empty_character()
            if package_token is not None:
                if self.package is None or self.package[0] != package_token:
                    raise SetupError("package_changed")
                data = self.package[1]
            data = extend_character(data, extensions)
            with open_history(self.store) as db:
                selected = Store.current(db, required=False)
            result = self.client.manage(
                "character.import", persona_id=selected, operation_id=request_id,
                package={"format": "pcar_character_v1", "persona_spec": spec, "character": data},
                relationship=relationship or None,
            )
            return {"id": result["id"], "name": result["name"]}

    def creation_status(self, request_id):
        operation_id(request_id)
        with open_history(self.store) as db:
            row = db.execute(
                "SELECT p.id,p.name FROM character_operations o JOIN personas p ON p.id=o.persona_id "
                "LEFT JOIN persona_lifecycle l ON l.persona_id=p.id "
                "WHERE o.operation_id=? AND o.user_id=? AND l.deleted_at IS NULL",
                (request_id, LOCAL_USER_ID),
            ).fetchone()
        return dict(row) if row else None

    def person(self, persona_id):
        with open_history(self.store) as db:
            row = RuntimeService._persona(db, persona_id)
            versions = {kind: profile.head(db, persona_id, kind) for kind in profile.KINDS}
            data = character.head(db, persona_id)
            return {
                "id": row["id"], "name": row["name"], "spec": row["spec"],
                "selected": Store.current(db, required=False) == persona_id,
                "versions": {key: version_view(value) for key, value in versions.items()},
                "state_active": profile.active_state(versions["state"]),
                "materials": material_summary(data["data"]) | {"id": data["id"]},
                "extensions": extensions_view(data["data"]),
            }

    def select_person(self, persona_id):
        with self.writing():
            self.chat._settle_previous()
            result = self.client.manage("persona.use", persona_id=persona_id)
        return {"id": result["id"], "name": result["name"]}

    def save_profile(self, persona_id, kind, data, expected, request_id):
        operation_id(request_id)
        if kind not in profile.KINDS:
            raise SetupError("invalid_section")
        if not isinstance(data, dict):
            raise SetupError("invalid_persona")
        with self.writing(persona_id):
            with open_history(self.store) as db:
                profile.check_persona(db, persona_id)
                try:
                    data = profile.ProfileService.validate(db, persona_id, kind, data)
                except RuntimeErrorBase as error:
                    if str(error) in {"state_expiry_must_be_aware_future_within_7_days", "profile_too_long"}:
                        raise SetupError(str(error)) from None
                    raise SetupError("invalid_persona") from None
            return version_view(self.client.manage(
                "profile.update", kind=kind, data=data, expected_version=expected, operation_id=request_id,
            ))

    def save_extensions(self, persona_id, changes, expected, request_id):
        operation_id(request_id)
        with self.writing(persona_id):
            original = self.client.manage("character.show", persona_id=persona_id, version_id=expected)
            data = extend_character(original["data"], changes)
            return self._save_materials(persona_id, data, expected, request_id)

    def append_material(self, persona_id, group, fields, expected, request_id):
        operation_id(request_id)
        if group not in ("lorebook", "examples") or not isinstance(fields, dict):
            raise SetupError("invalid_material")
        editable = {"text", "enabled", "keywords"}
        editable |= {"title", "category", "visibility"} if group == "lorebook" else {"scenario"}
        if set(fields) != editable:
            raise SetupError("invalid_material")
        with self.writing(persona_id):
            # Use the original base and a stable entry ID when a save result is uncertain.
            original = self.client.manage("character.show", persona_id=persona_id, version_id=expected)
            data = original["data"]
            entry = {"id": request_id, **fields}
            if group == "lorebook":
                entry.update(priority=0, dependencies=[])
            data[group].append(entry)
            try:
                character.validate_character(data)
            except RuntimeErrorBase as error:
                if str(error) in {"too_many_lore_entries", "too_many_expression_examples", "character_too_large"}:
                    raise SetupError(str(error)) from None
                raise SetupError("invalid_material") from None
            return self._save_materials(persona_id, data, expected, request_id)

    def _save_materials(self, persona_id, data, expected, request_id):
        return version_view(self.client.manage(
            "character.update", persona_id=persona_id, data=data,
            expected_version=expected, operation_id=request_id,
        ), include_data=False)

    def versions(self, persona_id, kind, before=None):
        table, where, args = version_scope(persona_id, kind)
        if before is not None and (type(before) is not int or before <= 0):
            raise SetupError("invalid_request")
        with open_history(self.store) as db:
            profile.check_persona(db, persona_id)
            current = current_version(db, persona_id, kind)["id"]
            rows = db.execute(
                f"SELECT id,revision,created_at FROM {table} WHERE {where} "
                "AND (? IS NULL OR revision < ?) ORDER BY revision DESC LIMIT ?",
                (*args, before, before, VERSION_PAGE + 1),
            ).fetchall()
            return {"current": current, "rows": [dict(row) for row in rows[:VERSION_PAGE]],
                    "before": rows[VERSION_PAGE - 1]["revision"] if len(rows) > VERSION_PAGE else None}

    def version_detail(self, persona_id, kind, version_id):
        table, where, args = version_scope(persona_id, kind)
        with open_history(self.store) as db:
            profile.check_persona(db, persona_id)
            row = db.execute(f"SELECT * FROM {table} WHERE {where} AND id=?",
                             (*args, version_id)).fetchone()
            if row is None:
                raise SetupError("version_not_found")
            current = current_version(db, persona_id, kind)
            old = character.decode_version(row) if kind == "character" else profile_row(row)
            a, b = current["data"], old["data"]
            differences = {key: {"current": a.get(key), "historical": b.get(key)}
                           for key in a.keys() | b.keys() if a.get(key) != b.get(key)}
            blocked = kind != "character" and profile.source_blocked(db, old["source_ids"])
            if kind == "state" and old["data"]["text"] and not profile.active_state(old):
                blocked = True
            return {"version": version_view(old), "expected": current["id"], "differences": differences,
                    "can_restore": not blocked and current["id"] != version_id,
                    "selected": Store.current(db, required=False) == persona_id}

    def restore_version(self, persona_id, kind, version_id, expected, request_id):
        operation_id(request_id)
        with self.writing(persona_id):
            # Also checks the kind, so a valid ID from a different tab is rejected.
            self.version_detail(persona_id, kind, version_id)
            args = {"version_id": version_id, "expected_version": expected, "operation_id": request_id}
            if kind == "character":
                args["persona_id"] = persona_id
            return version_view(self.client.manage(
                ("character" if kind == "character" else "profile") + ".restore", **args,
            ), include_data=False)

    def memories(self, persona_id, query=""):
        if not isinstance(query, str) or len(query) > 300:
            raise SetupError("invalid_request")
        with open_history(self.store) as db:
            profile.check_persona(db, persona_id)
            selected = Store.current(db, required=False) == persona_id
            rows = db.execute(
                "SELECT m.*,e.role AS source_role,e.text AS source_text,e.created_at AS source_at FROM memories m "
                "JOIN events e ON e.id=m.source_event_id AND e.persona_id=m.persona_id AND e.user_id=m.user_id "
                "WHERE m.persona_id=? AND m.user_id=? AND m.status='active' AND instr(m.text, ?) > 0 "
                "ORDER BY m.updated_at DESC,m.id DESC LIMIT 100",
                (persona_id, LOCAL_USER_ID, query),
            )
            items = [{key: row[key] for key in (
                "id", "category", "text", "updated_at", "source_role", "source_text", "source_at",
            )} for row in rows if usable_fact(db, row)]
        policy = self.client.manage("organization.policy.current", persona_id=persona_id)
        return {"items": items, "selected": selected, "policy": policy_view(policy)}

    def memory_sources(self, persona_id, session_id):
        with open_history(self.store) as db:
            require_session(db, persona_id, session_id)
            return [dict(row) for row in db.execute(
                "SELECT e.id,e.role,e.text,e.created_at FROM events e "
                "WHERE e.persona_id=? AND e.session_id=? AND e.user_id=? AND e.role IN ('user','note') "
                "AND NOT EXISTS (SELECT 1 FROM source_exclusions x WHERE x.event_id=e.id) "
                "ORDER BY e.rowid DESC LIMIT 50", (persona_id, session_id, LOCAL_USER_ID),
            )]

    def add_memory(self, persona_id, session_id, event_id, category, text):
        if category not in CATEGORIES or not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise SetupError("invalid_memory")
        with self.writing(persona_id):
            if not any(row["id"] == event_id for row in self.memory_sources(persona_id, session_id)):
                raise SetupError("memory_source_changed")
            result = self.client.manage("memory.add", source_event_id=event_id, category=category, text=text)
        return {"id": result["id"]}

    def change_memory(self, persona_id, memory_id, text=None):
        with self.writing(persona_id):
            with open_history(self.store) as db:
                RuntimeService._owned(db, "memories", memory_id, persona_id)
            if text is None:
                self.client.manage("memory.retract", memory_id=memory_id)
            else:
                if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                    raise SetupError("invalid_memory")
                self.client.manage("memory.revise", memory_id=memory_id, text=text)

    def memory_policy(self, persona_id, enabled, expected, request_id):
        operation_id(request_id)
        if type(enabled) is not bool:
            raise SetupError("invalid_request")
        with self.writing(persona_id):
            policy = self.client.manage("organization.policy.current", persona_id=persona_id)
            if policy["id"] != expected:
                raise SetupError("version_conflict")
            if enabled and not policy["data"]["enabled"]:
                raise SetupError("organization_disabled")
            cfg = self.config.public()
            model = cfg["model"] if cfg["organization"]["reuse_chat"] else cfg["organization"]["model"]
            if enabled:
                try:
                    require_generation_provider(model)
                    if not model["has_key"]:
                        raise ValueError()
                except ValueError:
                    raise SetupError("organization_connection_required") from None
            data = policy["data"] | {"memory_automation": enabled}
            return policy_view(self.client.manage(
                "organization.policy.update", persona_id=persona_id, data=data,
                expected_version=expected, expected_selected=persona_id, operation_id=request_id,
            ))

    def diagnostics(self):
        result = self.runtime_status()
        pending = None
        try:
            pending = len(self.client.manage("delivery.pending"))
        except (RuntimeErrorBase, OSError):
            pass
        return result | {
            "pending": pending,
            "unconfirmed": len(self.client.journal.pending()) if self.client.journal.path.is_file() else 0,
            "connection": self.connection()["configured"],
        }


def operation_id(value):
    from uuid import UUID
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise SetupError("invalid_request") from None


def safe_reason(code):
    return code if code in CONNECTION_CODES or code == "hourly_limit_reached" else None


def version_view(row, *, include_data=True):
    fields = ("id", "revision", "created_at", "data") if include_data else ("id", "revision", "created_at")
    return {field: row[field] for field in fields if field in row}


def material_summary(data):
    return {
        "attributes": sum(data[group] != defaults for group, defaults in character.FIELDS.items()),
        "lore": len(data["lorebook"]),
        "private": sum(row["visibility"] == "creator_only" for row in data["lorebook"]),
        "enabled": sum(row["enabled"] and row["visibility"] == "conversation" for row in data["lorebook"]),
        "examples": len(data["examples"]),
        "examples_enabled": sum(row["enabled"] for row in data["examples"]),
    }


def extensions_view(data):
    age = data["age"]
    return {key: data[group][field] for key, (group, field) in EXTENSION_FIELDS.items()} | {
        "age": age["fixed_age"],
        "age_editable": age["policy"] != "calendar" and age["birth_date"] is None,
    }


def extend_character(data, changes):
    if changes is None or changes == {}:
        return data
    if not isinstance(changes, dict) or not set(changes) <= {"age", *EXTENSION_FIELDS}:
        raise SetupError("invalid_extensions")
    result = deepcopy(data)
    for key, value in changes.items():
        if key == "age":
            if not extensions_view(data)["age_editable"] or (
                value is not None and (type(value) is not int or value < 0)
            ):
                raise SetupError("invalid_extensions")
            result["age"].update(policy="unspecified" if value is None else "fixed", fixed_age=value)
        else:
            group, field = EXTENSION_FIELDS[key]
            result[group][field] = value
    try:
        return character.validate_character(result)
    except RuntimeErrorBase:
        raise SetupError("invalid_extensions") from None


def version_scope(persona_id, kind):
    if kind not in EDIT_SECTIONS:
        raise SetupError("invalid_section")
    scope = "persona_id=? AND user_id=?"
    args = (persona_id, LOCAL_USER_ID)
    if kind == "character":
        return "character_versions", scope, args
    return "profile_versions", scope + " AND kind=?", (*args, kind)


def current_version(db, persona_id, kind):
    return character.head(db, persona_id) if kind == "character" else profile.head(db, persona_id, kind)


def profile_row(row):
    return dict(row) | {"data": json.loads(row["data_json"]), "source_ids": json.loads(row["source_ids_json"])}


def policy_view(policy):
    return {"id": policy["id"], "enabled": policy["data"]["enabled"] and
            policy["data"].get("memory_automation", False)}
