"""Explicit public-package feasibility probe. Does not activate production knowledge."""

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import sqlite3
from contextlib import closing

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from persona_runtime.app_processes import AppProcesses
from persona_runtime.character_embedding import safe_path
from persona_runtime.config import atomic_json
from persona_runtime.instance import ProcessIdentityHandle
from persona_runtime.knowledge_package import DATA_REVISION, PROGRAM, PackageInstaller, file_digest, utc_now
from persona_runtime.lifecycle import listener
from persona_runtime.paths import RuntimePaths


class MemoryCounters(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("page_faults", wintypes.DWORD),
                ("peak_rss", ctypes.c_size_t), ("rss", ctypes.c_size_t),
                ("peak_paged_pool", ctypes.c_size_t), ("paged_pool", ctypes.c_size_t),
                ("peak_nonpaged_pool", ctypes.c_size_t), ("nonpaged_pool", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t),
                ("private_bytes", ctypes.c_size_t)]


def memory_counters(handle):
    function = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    function.restype = wintypes.BOOL
    counters = MemoryCounters()
    counters.size = ctypes.sizeof(counters)
    if not function(handle, ctypes.byref(counters), counters.size):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters


def adapter_probe(paths, package, fixtures, evidence, manifest):
    from persona_runtime.knowledge_lexical import ChineseLexicalIndex
    from persona_runtime.knowledge_retrieval import KnowledgeRetriever
    parent_initial = memory_counters(wintypes.HANDLE(-1)).rss
    index = ChineseLexicalIndex(paths).prepare(package)
    backend = KnowledgeRetriever(paths, package, index)
    stopped = threading.Event()
    samples = []
    results = []
    native = None
    failure = None

    def observe():
        while not stopped.wait(.2):
            try:
                child = memory_counters(backend.child.handle)
                parent = memory_counters(wintypes.HANDLE(-1))
                samples.append({"child_peak_rss": child.peak_rss,
                                "parent_rss": parent.rss})
            except OSError:
                return

    try:
        warmup_started = time.monotonic()
        warmup = backend.search("月球", scope_key="acceptance", timeout=90)
        if warmup["state"] != "ready":
            raise RuntimeError("knowledge_adapter_warmup_failed")
        atomic_json(evidence / "warmup.json", {"seconds": time.monotonic() - warmup_started,
                    "candidates": warmup["candidates"]})
        native = ProcessIdentityHandle(backend.child.pid)
        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        database = safe_path(package, package / manifest["database"]["file"])
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            for case in fixtures["cases"]:
                if not case["retrieval_required"]:
                    results.append({"id": case["id"], "state": "not_executed_chitchat_routing_is_later_stage"})
                    continue
                started = time.monotonic()
                value = backend.search(case["query"], scope_key="acceptance", timeout=5)
                initial_state = value["state"]
                initial_wait = time.monotonic() - started
                if initial_state == "pending":
                    value = backend.search(case["query"], scope_key="acceptance", timeout=60)
                seconds = time.monotonic() - started
                top5 = value.get("candidates", [])[:5]
                matched = any(row["title"] in case["expected_titles"] for row in top5)
                references = value.get("evidence", [])
                bound = bool(references)
                for ref in references:
                    row = connection.execute("SELECT article_id,content FROM sections WHERE id=?",
                                             (ref["section_id"],)).fetchone()
                    bound = bound and row is not None and row[0] == ref["article_id"]
                    bound = bound and row[1][ref["start"]:ref["end"]] == ref["quote"]
                    bound = bound and hashlib.sha256(row[1].encode("utf-8")).hexdigest() == ref["section_sha256"]
                budget = (len(references) <= 6 and len({r["article_id"] for r in references}) <= 3
                          and sum(len(r["quote"]) for r in references) <= 3000)
                result = {"id": case["id"], "query": case["query"], "state": value["state"],
                          "initial_state": initial_state, "initial_wait_seconds": initial_wait,
                          "matched": matched, "seconds": seconds, "bound": bound,
                          "budget_valid": budget, "top5": top5, "retrieval": value}
                results.append(result)
                atomic_json(evidence / (case["id"] + ".json"), result)
                print(json.dumps({key: result[key] for key in
                    ("id", "state", "matched", "seconds", "bound", "budget_valid", "top5")}, ensure_ascii=True), flush=True)
    except Exception as error:
        failure = {"reason": type(error).__name__}
    finally:
        stopped.set()
        backend.close()
        released = native is None or not native.alive()
        if native:
            native.close()
    measured = [row for row in results if "matched" in row]
    times = sorted(row["seconds"] for row in measured)
    p95 = times[max(0, math.ceil(len(times)*.95)-1)] if times else None
    peak_child = max((row["child_peak_rss"] for row in samples), default=0)
    parent_delta = max(0, max((row["parent_rss"] for row in samples), default=parent_initial) - parent_initial)
    passed = failure is None and len(measured) == 20 and sum(row["matched"] for row in measured) >= 18
    passed = passed and all(row["bound"] and row["budget_valid"] and row["state"] == "ready" for row in measured)
    passed = passed and p95 is not None and 0 < peak_child + parent_delta <= 3 * 1024**3 and released
    report = {"at": utc_now(), "stage": "M9.A.0", "adapter": True, "state": "passed" if passed else "failed",
              "measured": len(measured), "top5_matches": sum(row["matched"] for row in measured),
              "p95_seconds": p95, "p95_limit_seconds": None, "latency_gate_enforced": False,
              "latency_target_met": None,
              "acceptance_priority": "recall_first_initial_version", "peak_child_rss_bytes": peak_child,
              "parent_rss_increase_bytes": parent_delta, "peak_component_estimate_bytes": peak_child + parent_delta,
              "child_released": released, "failure": failure, "production_activated": False,
              "chitchat_product_routing": "not_executed", "results": results}
    report["lexical_policy"] = {"normalizer": "opencc-python-reimplemented==0.1.7",
                                "title_index_version": "zh-windows-character-titles-v1",
                                "ann_dimensions": manifest["database"]["setup"]["annSize"],
                                "parent_baseline": "before_knowledge_components"}
    atomic_json(evidence / "resources.json", samples)
    atomic_json(evidence / "result.json", report)
    print(json.dumps({key: value for key, value in report.items() if key != "results"}), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=RuntimePaths.from_environment().home)
    parser.add_argument("--run", required=True)
    parser.add_argument("--upstream-diagnostic", action="store_true",
                        help="Observe a checksum-verified quarantined upstream package without passing its gate")
    parser.add_argument("--adapter", action="store_true", help="Use the shared product retrieval adapter")
    args = parser.parse_args()
    if args.adapter and args.upstream_diagnostic:
        parser.error("adapter and upstream diagnostic are mutually exclusive")
    if not args.run or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in args.run):
        parser.error("invalid run name")
    paths = RuntimePaths(args.home)
    package = paths.home / "data/knowledge/packages" / ("staging-" + DATA_REVISION[:12])
    executable = package / "wikilite.exe"
    if args.upstream_diagnostic:
        installer = PackageInstaller(paths)
        archive = installer.download(PROGRAM)
        installer.unpack_program(archive, package)
        unpacked = json.loads((package / "unpacked.json").read_text(encoding="utf-8"))
        if file_digest(package / "wikilite.db") != unpacked["database_sha256"]:
            raise ValueError("upstream_database_checksum_mismatch")
        manifest = {"database": {"sha256": unpacked["database_sha256"]}}
        database = package / "wikilite.db"
    else:
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        if manifest["state"] != "staged" or file_digest(executable) != manifest["program_sha256"]:
            raise ValueError("staged_program_not_verified")
        database = safe_path(package, package / manifest["database"]["file"])
        if file_digest(database) != manifest["database"]["sha256"]:
            raise ValueError("staged_database_checksum_mismatch")
    evidence = paths.home / "exports/acceptance/M9.A-20261109" / args.run
    evidence.mkdir(parents=True, exist_ok=False)
    fixture_path = REPO / "templates/knowledge-retrieval-cases.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(evidence / "execution-source.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        archive.write(Path(__file__), "scripts/m9a_probe.py")
        archive.write(fixture_path, "templates/knowledge-retrieval-cases.json")
        if args.adapter:
            archive.write(REPO / "pyproject.toml", "pyproject.toml")
            archive.write(REPO / "uv.lock", "uv.lock")
            for name in ("knowledge_lexical.py", "knowledge_lexical_search.py", "knowledge_titles.py",
                         "knowledge_retrieval.py", "app_processes.py"):
                archive.write(REPO / "persona_runtime" / name, "persona_runtime/" + name)
    atomic_json(evidence / "started.json", {"at": utc_now(), "fixture_sha256": file_digest(fixture_path),
                "package_sha256": manifest["database"]["sha256"], "stage": "M9.A.0",
                "purpose": "quarantined_upstream_diagnostic" if args.upstream_diagnostic else "gate",
                "production_activated": False})
    if args.adapter:
        return adapter_probe(paths, package, fixtures, evidence, manifest)
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}/api/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    phase = "starting"
    request_action = None

    def request(action, values, timeout=8):
        nonlocal request_action
        request_action = action
        encoded = urllib.parse.urlencode(values)
        with opener.open(base_url + action + "?" + encoded, timeout=timeout) as response:
            body = json.load(response)
        if body.get("status") != "success":
            raise ValueError("wikilite_response_failed")
        return body

    environment = dict(os.environ)
    processes = AppProcesses()
    stopped = threading.Event()
    samples = []
    child = None
    results = []
    failure = None
    startup_seconds = None
    native = None
    try:
        child = processes.start("knowledge", [str(executable), "-db", str(database),
            "-web", "-web-host", "127.0.0.1", "-web-port", str(port),
            "-ai-cache", "-ai-threads", "2"], cwd=package, env=environment)
        native = ProcessIdentityHandle(child.pid)

        def observe():
            while not stopped.wait(.2):
                try:
                    memory = memory_counters(child.handle)
                    samples.append({"epoch": time.time(), "rss": memory.rss,
                                    "peak_rss": memory.peak_rss})
                except OSError:
                    return

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        started = time.monotonic()
        while time.monotonic() - started < 90:
            if child.poll() is not None:
                raise RuntimeError("wikilite_exited_before_ready")
            try:
                request("search/title", {"query": "月球", "limit": 1}, timeout=1)
                break
            except (OSError, ValueError):
                time.sleep(.25)
        else:
            raise TimeoutError("wikilite_startup_timeout")
        startup_seconds = time.monotonic() - started
        phase = "port_ownership"
        owner = listener(port)
        if not owner or owner["ProcessId"] != child.pid:
            raise RuntimeError("wikilite_port_owner_mismatch")
        warmup_started = time.monotonic()
        phase = "semantic_warmup"
        warmup = request("search/semantic", {"query": "月球", "limit": 1}, timeout=60)
        atomic_json(evidence / "warmup.json", {"seconds": time.monotonic()-warmup_started,
                    "results": warmup.get("results"), "startup_seconds": startup_seconds})
        for case in fixtures["cases"]:
            phase = case["id"]
            if not case["retrieval_required"]:
                results.append({"id": case["id"], "state": "not_executed_chitchat_routing_is_later_stage"})
                continue
            started = time.monotonic()
            scores = {}
            titles = {}
            channels = {}
            for action in ("search/lexical", "search/semantic"):
                channel = request(action, {"query": case["query"], "limit": 10})
                candidates = channel.get("results") or []
                channels[action] = candidates
                seen = set()
                for rank, item in enumerate(candidates, 1):
                    article_id = item["article_id"]
                    if article_id in seen:
                        continue
                    seen.add(article_id)
                    scores[article_id] = scores.get(article_id, 0) + 1 / (60 + rank)
                    titles[article_id] = item["title"]
            ordered = sorted(scores, key=lambda key: (-scores[key], key))
            top5 = [{"id": key, "title": titles[key], "rrf": scores[key]} for key in ordered[:5]]
            expected = set(case["expected_titles"])
            matched = any(item["title"] in expected for item in top5)
            evidence_rows = []
            for article_id in ordered[:3]:
                article = request("article", {"id": article_id})["article"]
                for section in article.get("sections", []):
                    text = section.get("content") or ""
                    if text:
                        evidence_rows.append({"article_id": article_id, "section_id": section["id"],
                            "title": article["title"], "characters": len(text),
                            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
                        break
            result = {"id": case["id"], "query": case["query"], "top5": top5,
                      "matched": matched, "seconds": time.monotonic()-started,
                      "evidence": evidence_rows, "channels": channels}
            results.append(result)
            atomic_json(evidence / (case["id"] + ".json"), result)
            print(json.dumps({key: result[key] for key in ("id", "matched", "seconds", "top5")},
                             ensure_ascii=False), flush=True)
    except Exception as error:
        failure = {"reason": type(error).__name__, "phase": phase, "request_action": request_action}
        if isinstance(error, urllib.error.HTTPError):
            failure["http_status"] = error.code
    finally:
        stopped.set()
        try:
            processes.close()
        except Exception as error:
            failure = {"reason": "release_" + type(error).__name__}
        child_released = native is None or not native.alive()
        if native is not None:
            native.close()
    measured = [item for item in results if "matched" in item]
    times = sorted(item["seconds"] for item in measured)
    p95 = times[max(0, math.ceil(len(times)*.95)-1)] if times else None
    peak = max((item["peak_rss"] for item in samples), default=0)
    passed = failure is None and len(measured) == 20 and sum(item["matched"] for item in measured) >= 18
    passed = passed and p95 is not None and 0 < peak <= 3 * 1024**3 and child_released
    passed = passed and not args.upstream_diagnostic
    report = {"at": utc_now(), "stage": "M9.A.0", "state": "passed" if passed else "failed",
              "measured": len(measured), "top5_matches": sum(item["matched"] for item in measured),
              "p95_seconds": p95, "peak_rss_bytes": peak, "failure": failure,
              "p95_limit_seconds": None, "latency_gate_enforced": False,
              "latency_target_met": None,
              "acceptance_priority": "recall_first_initial_version",
              "child_released": child_released, "production_activated": False,
              "upstream_diagnostic": args.upstream_diagnostic,
              "chitchat_product_routing": "not_executed", "results": results}
    atomic_json(evidence / "result.json", report)
    atomic_json(evidence / "resources.json", samples)
    print(json.dumps({key: value for key, value in report.items() if key != "results"},
                     ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
