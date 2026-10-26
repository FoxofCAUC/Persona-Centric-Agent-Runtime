"""Local persona and memory persistence CLI; no model calls."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

from .audit import record
from .models import CATEGORIES, ROLES, PersonaSpec, RuntimeErrorBase, generic_persona
from .paths import RuntimePaths
from .service import RuntimeService
from .storage import Store


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--json", action="store_true", help="Machine-readable output with UTC timestamps")
    groups = root.add_subparsers(dest="group", required=True)
    groups.add_parser("paths")
    groups.add_parser("init-storage")
    for name in ("serve", "prepare", "gui"):
        groups.add_parser(name)
    commands = {
        "db": ("init", "status"),
        "persona": ("create", "list", "show", "use"),
        "relationship": ("set", "show"),
        "session": ("create", "list", "show"),
        "event": ("append", "list"),
        "memory": ("add", "search", "show", "revise", "retract"),
        "data": ("export", "backup"),
    }
    for group, actions in commands.items():
        parent = groups.add_parser(group)
        sub = parent.add_subparsers(dest="action", required=True)
        for action in actions:
            cmd = sub.add_parser(action)
            if group == "persona" and action == "create":
                cmd.add_argument("--file", type=Path, help="UTF-8 persona JSON; omit for generic template")
            if (group == "persona" and action in ("show", "use")) or (
                    group == "session" and action == "show") or (
                    group == "memory" and action in ("show", "revise", "retract")):
                if action == "show" and group == "persona":
                    cmd.add_argument("id", nargs="?")
                else:
                    cmd.add_argument("id")
            if group == "relationship" and action == "set":
                cmd.add_argument("--text", required=True)
            if group == "session" and action == "create":
                cmd.add_argument("--scope", default="private")
            if group == "event":
                cmd.add_argument("--session", required=True)
                if action == "append":
                    cmd.add_argument("--role", choices=ROLES, required=True)
                    cmd.add_argument("--text", required=True)
            if group == "memory":
                if action == "add":
                    cmd.add_argument("--category", choices=CATEGORIES, required=True)
                if action in ("add", "revise"):
                    cmd.add_argument("--text", required=True)
                    cmd.add_argument("--source", required=action == "add")
                if action == "search":
                    cmd.add_argument("query", nargs="?", default="")
                    cmd.add_argument("--limit", type=int, default=20)
            if group == "data":
                cmd.add_argument("--name", help="File name within the configured export/backup directory")
    return root


def dispatch(args, paths: RuntimePaths):
    if args.group == "serve":
        import asyncio
        from .server import serve
        asyncio.run(serve(paths))
        return {"stopped": True}
    if args.group == "prepare":
        from .deployment import prepare
        return prepare(paths)
    if args.group == "gui":
        from .desktop import run
        run(paths)
        return {"closed": True}
    if args.group in ("paths", "init-storage"):
        if args.group == "init-storage":
            paths.initialize()
        return {key: str(value) for key, value in paths.directories().items()}
    store = Store(paths)
    service = RuntimeService(store)
    group, action = args.group, args.action
    if group == "db":
        if action == "init":
            store.initialize()
        return store.status()
    if group == "persona":
        if action == "create":
            spec = (PersonaSpec.from_dict(json.loads(args.file.read_text(encoding="utf-8-sig")))
                    if args.file else generic_persona())
            return service.create_persona(spec)
        if action == "list":
            return service.list_personas()
        if action == "show":
            return service.show_persona(args.id)
        return service.use_persona(args.id)
    if group == "relationship":
        return service.set_relationship(args.text) if action == "set" else service.show_relationship()
    if group == "session":
        if action == "create":
            return service.create_session(args.scope)
        return service.list_sessions() if action == "list" else service.show_session(args.id)
    if group == "event":
        return (service.append_event(args.session, args.role, args.text) if action == "append"
                else service.show_session(args.session)["events"])
    if group == "memory":
        if action == "add":
            return service.add_memory(args.category, args.text, args.source)
        if action == "search":
            return service.search_memories(args.query, args.limit)
        if action == "show":
            return service.show_memory(args.id)
        if action == "revise":
            return service.revise_memory(args.id, args.text, args.source)
        return service.retract_memory(args.id)
    return service.export_persona(args.name) if action == "export" else service.backup(args.name)


def local_display(value):
    if isinstance(value, dict):
        return {k: (datetime.fromisoformat(v).astimezone().isoformat() if k.endswith("_at")
                    and isinstance(v, str) else local_display(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [local_display(v) for v in value]
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    operation = args.group + ("." + args.action if hasattr(args, "action") else "")
    paths = None
    error_type = None
    result = None
    try:
        paths = RuntimePaths.from_environment()
        result = dispatch(args, paths)
    except (RuntimeErrorBase, ValueError, OSError, sqlite3.Error) as exc:
        error_type = type(exc).__name__
        if isinstance(exc, json.JSONDecodeError):
            message = f"Invalid JSON at line {exc.lineno}, column {exc.colno}"
        else:
            message = str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    finally:
        if paths is not None and args.group not in ("paths", "init-storage"):
            try:
                record(paths, operation, error=error_type,
                       record_id=result.get("id") if isinstance(result, dict) else None)
            except OSError:
                print("warning: operation log could not be written", file=sys.stderr)
    print(json.dumps(result if args.json else local_display(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
