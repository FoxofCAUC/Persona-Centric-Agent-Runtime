"""Metadata-only operation log. Never log user text, specs or arguments."""

import json

from .models import timestamp
from .paths import RuntimePaths


def record(paths: RuntimePaths, operation: str, *, error: str | None = None, record_id: str | None = None) -> None:
    directory = paths.directories()["logs"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "operations.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"at": timestamp(), "operation": operation,
                                 "record_id": record_id, "error_type": error}) + "\n")
