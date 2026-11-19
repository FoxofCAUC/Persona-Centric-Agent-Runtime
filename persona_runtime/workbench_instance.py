"""Shared ownership and wake-up protocol for the Windows workbenches."""

import json
import time

from .config import atomic_json
from .instance import InstanceLock
from .lifecycle import require_console
from .models import RuntimeErrorBase, identifier


def acquire_or_wake(paths):
    """Return the instance lock, or notify the workbench that already owns it."""
    try:
        return InstanceLock(paths.home / "runtime/desktop.lock")
    except RuntimeErrorBase:
        lease = require_console(paths)
        atomic_json(paths.home / "runtime/wakeup.json", {"token": lease["token"], "id": identifier()})
        return None


def next_wakeup(paths, token, last_id):
    try:
        value = json.loads((paths.home / "runtime/wakeup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("token") != token:
        return None
    wake_id = value.get("id")
    if isinstance(wake_id, str) and wake_id and wake_id != last_id:
        return wake_id, value.get("action", "show")
    return None


def wait_for_previous_components(controller, lease):
    if not lease.get("previous_stale") and not controller.has_resident_processes():
        return
    deadline = time.monotonic() + 5
    while controller.has_resident_processes():
        if time.monotonic() >= deadline:
            raise RuntimeErrorBase("已登记的旧组件尚未退出，请先检查该实例的运行状态。")
        time.sleep(.2)
