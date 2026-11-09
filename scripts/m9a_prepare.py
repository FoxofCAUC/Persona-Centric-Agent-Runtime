"""Explicit M9.A.0 package preparation with local source and progress evidence."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from persona_runtime.config import atomic_json
from persona_runtime.instance import current_process_identity
from persona_runtime.knowledge_package import PackageInstaller, utc_now
from persona_runtime.paths import RuntimePaths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=RuntimePaths.from_environment().home)
    parser.add_argument("--run", required=True, help="New evidence run name")
    args = parser.parse_args()
    if not args.run or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in args.run):
        parser.error("run name must contain letters, digits, hyphens or underscores")
    paths = RuntimePaths(args.home)
    paths.initialize()
    evidence = paths.home / "exports/acceptance/M9.A-20261109" / args.run
    evidence.mkdir(parents=True, exist_ok=False)
    files = ("persona_runtime/knowledge_package.py", "persona_runtime/knowledge_package_repair.py", "scripts/m9a_prepare.py",
             "templates/knowledge-retrieval-cases.json", "tests/test_knowledge_package.py",
             "tests/test_knowledge_package_repair.py")
    frozen = []
    with zipfile.ZipFile(evidence / "execution-source.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            data = (REPO / name).read_bytes()
            archive.writestr(name, data)
            frozen.append({"path": name, "sha256": hashlib.sha256(data).hexdigest()})
    atomic_json(evidence / "started.json", {"at": utc_now(), "pid": os.getpid(),
                "process": current_process_identity(), "files": frozen,
                "stage": "M9.A.0", "production_activated": False})
    with (evidence / "progress.jsonl").open("x", encoding="utf-8", buffering=1) as log:
        def progress(value):
            line = json.dumps(value, ensure_ascii=False)
            log.write(line + "\n")
            print(line, flush=True)
        installer = PackageInstaller(paths, progress=progress)
        try:
            result = installer.prepare()
        except Exception as error:
            failure = {"at": utc_now(), "state": "failed", "reason": type(error).__name__,
                       "stage": "M9.A.0", "production_activated": False}
            if hasattr(error, "code"):
                failure["http_status"] = error.code
            if isinstance(error, ValueError) and re.fullmatch("[a-z0-9_]{1,160}", str(error)):
                failure["code"] = str(error)
            installer.record("failed", reason=failure["reason"])
            atomic_json(evidence / "result.json", failure)
            return 1
    atomic_json(evidence / "result.json", {"at": utc_now(), "state": "staged",
                "stage": "M9.A.0", "manifest": result, "production_activated": False})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
