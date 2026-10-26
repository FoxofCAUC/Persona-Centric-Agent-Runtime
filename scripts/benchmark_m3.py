"""Repeatable local-only orchestration benchmark; never contacts a model provider."""
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.chat import ChatEngine
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class LocalModel:
    async def complete(self, model, messages):
        await asyncio.sleep(.04)
        return "offline benchmark reply", {"total_tokens": 1}


async def benchmark(paths):
    base = paths.home / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="m3-benchmark-", dir=base) as directory:
        isolated = RuntimePaths(Path(directory))
        cfg = ConfigStore(isolated)
        cfg.initialize()
        cfg.update({"model": {"base_url": "http://127.0.0.1:1", "name": "local-stub"}})
        store = Store(isolated)
        store.initialize()
        service = RuntimeService(store)
        service.use_persona(service.create_persona(generic_persona())["id"])
        engine = ChatEngine(store, cfg, LocalModel())
        accepted = []
        turns = []
        for i in range(6):
            started = time.perf_counter()
            turns.append(await engine.submit("desktop", "offline input", str(i)))
            accepted.append(time.perf_counter()-started)
        await engine.wait()
        completed = [engine.get(turn["id"]) for turn in turns]
        def summary(values):
            return {"count": len(values), "p50_seconds": round(statistics.median(values), 4),
                    "p95_seconds": round(statistics.quantiles(values, n=20, method="inclusive")[18], 4)}
        return {"scope": "local orchestration only, simulated 40ms provider; excludes UI and QQ network",
                "real_model_calls": 0, "synthetic_calls": len(turns),
                "accepted": summary(accepted),
                "queue": summary([turn["timings"]["queue_seconds"] for turn in completed]),
                "provider": summary([turn["timings"]["provider_seconds"] for turn in completed]),
                "completed": summary([turn["timings"]["total_seconds"] for turn in completed]),
                "window_refresh_interval_ms": 500, "reply_mode": "ordered complete replies, no merging"}


if __name__ == "__main__":
    paths = RuntimePaths.from_environment()
    report = asyncio.run(benchmark(paths))
    destination = paths.home / "exports/acceptance" / ("M3-offline-benchmark-"+identifier()+".json")
    atomic_json(destination, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Report:", destination)
