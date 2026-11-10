"""Owned local encyclopedia retrieval with one semantic execution position."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from .app_processes import AppProcesses
from .character_embedding import safe_path
from .knowledge_lexical import ChineseLexicalIndex
from .knowledge_package import file_digest
from .knowledge_lexical_search import EncyclopediaLexicalSearch


def loopback_listener_pid(port):
    function = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    function.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                         wintypes.DWORD, ctypes.c_int, wintypes.DWORD]
    function.restype = wintypes.DWORD
    size = wintypes.DWORD()
    if function(None, ctypes.byref(size), False, 2, 3, 0) != 122:
        raise OSError("knowledge_listener_table_unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if function(buffer, ctypes.byref(size), False, 2, 3, 0):
        raise OSError("knowledge_listener_table_unavailable")
    rows = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))
    owners = set()
    for index in range(rows[0]):
        offset = 1 + index * 6
        if rows[offset + 1] == 0x0100007F and socket.ntohs(rows[offset + 2] & 0xFFFF) == port:
            owners.add(int(rows[offset + 5]))
    return next(iter(owners)) if len(owners) == 1 else None


def article_candidates(rows, limit=10):
    result = []
    seen = set()
    for row in rows:
        ident = row.get("article_id")
        if type(ident) is not int or ident <= 0 or ident in seen:
            continue
        seen.add(ident)
        result.append({key: row[key] for key in
            ("article_id", "title", "section_id", "lexical_score", "word_matches", "type", "power",
             "origin_article_id", "origin_section_id", "origin_heading", "origin_title") if key in row})
        if len(result) == limit:
            break
    return result


def reciprocal_rank(channels):
    scores = {}
    rows = {}
    for channel in channels:
        for rank, row in enumerate(article_candidates(channel), 1):
            ident = row["article_id"]
            scores[ident] = scores.get(ident, 0) + 1 / (60 + rank)
            rows.setdefault(ident, row)
    return [dict(rows[ident], rrf=scores[ident])
            for ident in sorted(scores, key=lambda ident: (-scores[ident], ident))]


class KnowledgeRetriever:
    def __init__(self, paths, package, lexical_database):
        self.paths = paths
        self.package = Path(package)
        self.manifest = json.loads((self.package / "manifest.json").read_text(encoding="utf-8"))
        self.database = safe_path(self.package, self.package / self.manifest["database"]["file"])
        self.executable = safe_path(self.package, self.package / "wikilite.exe")
        self.lexical_database = Path(lexical_database)
        self.lexical = ChineseLexicalIndex(paths)
        self.identity = self.manifest["database"]["sha256"]
        self.lexical_search = EncyclopediaLexicalSearch(paths, package, self.lexical)
        self.processes = AppProcesses()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pcar-knowledge")
        self.lexical_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pcar-knowledge-lexical")
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.child = None
        self.url = None
        self.ready = False
        self.cache = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, action, args, timeout=30):
        if self.closing.is_set():
            raise RuntimeError("knowledge_stopped")
        url = self.url + action + "?" + urllib.parse.urlencode(args)
        with self.opener.open(url, timeout=timeout) as response:
            value = json.load(response)
        if value.get("status") != "success":
            raise ValueError("knowledge_native_request_failed")
        return value

    def start(self):
        if self.ready:
            return
        if self.closing.is_set():
            raise RuntimeError("knowledge_stopped")
        if file_digest(self.executable) != self.manifest["program_sha256"]:
            raise ValueError("knowledge_program_checksum_mismatch")
        if file_digest(self.database) != self.identity:
            raise ValueError("knowledge_database_checksum_mismatch")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.lock:
            if self.closing.is_set():
                raise RuntimeError("knowledge_stopped")
            self.url = f"http://127.0.0.1:{port}/api/"
            self.child = self.processes.start("knowledge", [str(self.executable), "-db", str(self.database),
                "-web", "-web-host", "127.0.0.1", "-web-port", str(port),
                "-ai-cache", "-ai-threads", "2"], cwd=self.package, env=dict(os.environ))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not self.closing.is_set():
            with self.lock:
                if not self.child.handle or self.child.poll() is not None:
                    raise RuntimeError("knowledge_native_exited")
            try:
                self.request("search/title", {"query": "月球", "limit": 1}, timeout=1)
                break
            except (OSError, ValueError):
                self.closing.wait(.2)
        else:
            raise TimeoutError("knowledge_native_startup_timeout")
        if loopback_listener_pid(port) != self.child.pid:
            raise RuntimeError("knowledge_native_port_owner_mismatch")
        self.request("search/semantic", {"query": "月球", "limit": 1}, timeout=60)
        self.ready = True

    def _search(self, query, allowed):
        try:
            return self._retrieve(query, allowed)
        except Exception:
            with self.lock:
                self.ready = False
                self.processes.stop("knowledge")
            raise

    def _retrieve(self, query, allowed):
        self.start()
        started = time.monotonic()
        lexical_future = self.lexical_executor.submit(self.lexical_search.search_views, query, self.closing)
        # Wikilite returns section hits. Fill the article candidate budget before RRF.
        sessions = self.lexical.text.sessions(query)
        semantic_views = []
        session_seconds = []
        for session in sessions:
            step = time.monotonic()
            semantic_views.append(article_candidates(self.request("search/semantic", {"query": session, "limit": 50})["results"] or []))
            session_seconds.append(time.monotonic() - step)
        semantic = reciprocal_rank(semantic_views)[:10]
        semantic = [row for row in semantic if self.lexical_search.accepted(row, query)]
        try:
            lexical_details = lexical_future.result()
            full, titles, exact, headings = lexical_details.pop("views")
            related = self.lexical_search.related_titles(semantic, query)
            lexical_views = [full, titles, exact, related, headings]
            lexical = reciprocal_rank(lexical_views)[:10]
            lexical_details.update({"state": "ready", "related_titles": related, "heading_topics": headings})
        except sqlite3.OperationalError as error:
            if self.closing.is_set():
                raise RuntimeError("knowledge_stopped") from error
            lexical = self.lexical.search(self.lexical_database, query, 10)
            lexical_details = {"state": "fallback", "reason": type(error).__name__,
                               "seconds": time.monotonic() - started}
        ranked = reciprocal_rank([lexical, semantic])
        accepted = [row for row in ranked if allowed(row)][0:3]
        articles = [self.request("article", {"id": row["article_id"]})["article"] for row in accepted]
        before_evidence = time.monotonic()
        evidence = self.evidence(articles, query)
        return {"state": "ready", "package_sha256": self.identity,
                "data_revision": self.manifest["data_revision"], "candidates": ranked,
                "channels": {"lexical": lexical, "ann": semantic}, "evidence": evidence,
                "sessions": sessions, "lexical_details": lexical_details,
                "timings": {"lexical_seconds": lexical_details["seconds"], "session_seconds": session_seconds,
                            "evidence_seconds": time.monotonic() - before_evidence},
                "seconds": time.monotonic() - started, "source_snapshot_at": self.manifest["source_snapshot_at"]}

    def evidence(self, articles, query):
        terms = set(self.lexical.text.tokens(query, query=True))
        result = []
        budget = 3000
        for article in articles:
            sections = [section for section in article["sections"] if section.get("content")]
            sections.sort(key=lambda section: -len(terms.intersection(
                self.lexical.text.tokens(section["content"][:4000]))))
            for section in sections[:2]:
                if not budget:
                    return result
                body = section["content"]
                quote = body[:min(500, budget)]
                budget -= len(quote)
                result.append({"kind": "encyclopedia", "package_sha256": self.identity,
                    "data_revision": self.manifest["data_revision"], "article_id": article["id"],
                    "article_title": article["title"], "section_id": section["id"],
                    "section_title": section["title"], "start": 0, "end": len(quote), "quote": quote,
                    "section_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(article["title"].replace(" ", "_")),
                    "source_snapshot_at": self.manifest["source_snapshot_at"], "validity": "package_snapshot"})
        return result

    def search(self, query, *, scope_key="public", allowed=lambda row: True, timeout=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("knowledge_query_invalid")
        key = (self.identity, scope_key, hashlib.sha256(query.encode("utf-8")).hexdigest())
        with self.lock:
            if self.closing.is_set():
                return {"state": "stopped"}
            now = time.monotonic()
            entry = self.cache.get(key)
            if entry is None or now - entry[0] > 24 * 3600:
                if sum(not future.done() for _, future in self.cache.values()) >= 10:
                    return {"state": "busy"}
                if len(self.cache) >= 128:
                    expired = next((saved for saved, (_, future) in self.cache.items() if future.done()), None)
                    if expired is not None:
                        del self.cache[expired]
                future = self.executor.submit(self._search, query, allowed)
                self.cache[key] = (now, future)
            else:
                future = entry[1]
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            if future.done():
                error = future.exception()
                if error is None:
                    return future.result()
                return {"state": "failed", "request_key": key[2], "reason": type(error).__name__}
            return {"state": "pending", "request_key": key[2], "package_sha256": self.identity}
        except Exception as error:
            return {"state": "failed", "request_key": key[2], "reason": type(error).__name__}

    def close(self):
        self.closing.set()
        try:
            with self.lock:
                self.ready = False
                self.processes.close()
        finally:
            try:
                self.lexical_search.close()
            finally:
                self.executor.shutdown(wait=False, cancel_futures=True)
                self.lexical_executor.shutdown(wait=False, cancel_futures=True)
