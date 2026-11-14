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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--json", action="store_true", help="Machine-readable output with UTC timestamps")
    root.add_argument("--home",type=Path,help="Explicit instance data directory")
    groups = root.add_subparsers(dest="group", required=True)
    groups.add_parser("paths")
    groups.add_parser("init-storage")
    for name in ("serve", "gui", "force-stop"):
        groups.add_parser(name)
    setup=groups.add_parser('prepare')
    setup.add_argument('--base-python',type=Path);setup.add_argument('--local-model-source',type=Path)
    setup.add_argument('--diagnostics',action='store_true');setup.add_argument('--register-existing',action='store_true');setup.add_argument('--no-register',action='store_true')
    model=groups.add_parser('prepare-model');model.add_argument('--local-source',type=Path)
    doctor=groups.add_parser('doctor');doctor.add_argument('--network',action='store_true')
    configure=groups.add_parser('configure-model');configure.add_argument('--role',choices=('main','organization','selector','jev'),required=True)
    configure.add_argument('--base-url');configure.add_argument('--model');configure.add_argument('--key-stdin',action='store_true')
    commands = {
        "db": ("init", "status"),
        "selector": ("status", "records"),
        "quota": ("status",),
        "persona": ("create", "list", "show", "use"),
        "relationship": ("set", "show"),
        "session": ("create", "list", "show"),
        "event": ("append", "list"),
        "memory": ("add", "search", "show", "revise", "retract"),
        "data": ("export", "backup", "import", "migration-export", "migration-import", "migration-activate", "migration-rollback"),
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
                if action in ('import','migration-import'):cmd.add_argument('--file',type=Path,required=True)
                if action in ('migration-import','migration-activate','migration-rollback'):
                    cmd.add_argument('--apply',action='store_true');cmd.add_argument('--dry-run',action='store_true')
                if action=='migration-activate':
                    cmd.add_argument('--authorize-visible',action='store_true');cmd.add_argument('--resume-background',action='store_true')
    return root


def dispatch(args, paths: RuntimePaths):
    if args.group == "force-stop":
        from .emergency import force_stop_related
        report = force_stop_related(paths)
        if not report["success"]:
            raise RuntimeErrorBase("相关进程未完全停止，报告：" + report["report_file"])
        return report
    if args.group == "serve":
        from .lifecycle import ServiceController
        return ServiceController(paths).start('service')
    if args.group == 'prepare':
        from .installation import register_existing,prepare_desktop
        if args.register_existing:return register_existing(paths)
        return prepare_desktop(paths,base_python=args.base_python,local_model_source=args.local_model_source,diagnostics=args.diagnostics,register=not args.no_register)
    if args.group=='prepare-model':
        from .installation import prepare_model
        return prepare_model(paths,local_source=args.local_source)
    if args.group=='configure-model':
        from .installation import configure_model
        return configure_model(paths,args.role,base_url=args.base_url,name=args.model,key_stdin=args.key_stdin)
    if args.group == "gui":
        from .desktop import run
        run(paths)
        return {"closed": True}
    if args.group in ("paths", "init-storage"):
        if args.group == "init-storage":
            paths.initialize()
        return {key: str(value) for key, value in paths.directories().items()}
    if args.group=='data' and args.action.startswith('migration-'):
        from .migration import export_instance,import_instance,activate_instance,rollback_instance
        if args.action=='migration-export':return export_instance(paths,args.name)
        if args.action=='migration-import':return import_instance(paths,args.file,apply=args.apply and not args.dry_run)
        if args.action=='migration-rollback':return rollback_instance(paths,apply=args.apply and not args.dry_run)
        return activate_instance(paths,apply=args.apply and not args.dry_run,authorize_visible=args.authorize_visible,resume_background=args.resume_background)
    from .storage import Store
    from .service import RuntimeService
    store = Store(paths)
    service = RuntimeService(store)
    group, action = args.group, args.action
    if group == 'selector':
        from .config import ConfigStore
        from .desktop_client import LocalClient
        client=LocalClient(ConfigStore(paths))
        if action=='status':return client.manage('character.selector-status')
        with store.connection() as conn:pid=store.current(conn)
        return client.manage('character.selector-records',persona_id=pid)
    if group=='quota':
        from .config import ConfigStore
        from .quotas import QuotaService
        return QuotaService(store,ConfigStore(paths)).current()
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
    if group=='data' and action=='import':
        from .portable_memory import import_export
        from .models import identifier
        return import_export(store,json.loads(args.file.read_text(encoding='utf-8-sig')),service.show_persona()['id'],identifier())
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
        from .installation import resolve_home,activated,doctor
        if args.group=='doctor':
            result=doctor(resolve_home(args.home,required=False),network=args.network)
        else:
            paths = RuntimePaths.from_environment(args.home)
            with activated(paths):result = dispatch(args, paths)
    except (RuntimeErrorBase, ValueError, OSError, sqlite3.Error) as exc:
        error_type = type(exc).__name__
        if isinstance(exc, json.JSONDecodeError):
            message = f"Invalid JSON at line {exc.lineno}, column {exc.colno}"
        else:
            message = str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    finally:
        if paths is not None and args.group not in ("paths", "init-storage", "doctor") and not (args.group=='data' and args.action in ('migration-import','migration-activate','migration-rollback') and (not args.apply or args.dry_run)):
            try:
                record(paths, operation, error=error_type,
                       record_id=result.get("id") if isinstance(result, dict) else None)
            except OSError:
                print("warning: operation log could not be written", file=sys.stderr)
    print(json.dumps(result if args.json else local_display(result), ensure_ascii=False, indent=2))
    return 2 if args.group=='doctor' and not result['ready'] else 0


if __name__ == "__main__":
    sys.exit(main())
