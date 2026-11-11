"""Article candidate fusion and process ownership do not depend on UI state."""

import os
import socket
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor

from persona_runtime.knowledge_retrieval import KnowledgeRetriever, article_candidates, loopback_listener_pid, reciprocal_rank


class RetrievalTests(unittest.TestCase):
    def test_duplicate_sections_do_not_consume_article_candidate_budget(self):
        rows = [{"article_id": 1, "title": "first"}] * 10
        rows += [{"article_id": ident, "title": str(ident)} for ident in range(2, 15)]
        self.assertEqual([row["article_id"] for row in article_candidates(rows)], list(range(1, 11)))

    def test_rrf_uses_unique_article_rank_and_fixed_sixty(self):
        first = [{"article_id": 1}, {"article_id": 1}, {"article_id": 2}]
        second = [{"article_id": 2}, {"article_id": 3}]
        result = reciprocal_rank([first, second])
        self.assertEqual(result[0]["article_id"], 2)
        self.assertAlmostEqual(result[0]["rrf"], 1/62 + 1/61)
        self.assertAlmostEqual(next(row["rrf"] for row in result if row["article_id"] == 1), 1/61)

    def test_listener_owner_is_exact_loopback_process(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.assertEqual(loopback_listener_pid(listener.getsockname()[1]), os.getpid())

    def future_backend(self, retrieve):
        backend = KnowledgeRetriever.__new__(KnowledgeRetriever)
        backend.identity = "public-fixture"
        backend.lock = threading.RLock()
        backend.closing = threading.Event()
        backend.cache = {}
        backend.executor = ThreadPoolExecutor(max_workers=1)
        backend._search = retrieve
        self.addCleanup(backend.executor.shutdown)
        return backend

    def test_finished_worker_timeout_is_failed_instead_of_permanent_pending(self):
        def retrieve(query, allowed):
            raise TimeoutError("fixture")
        backend = self.future_backend(retrieve)
        result = backend.search("public fixture", timeout=1)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "TimeoutError")

    def test_followup_reuses_pending_future(self):
        ready = threading.Event()
        calls = []
        def retrieve(query, allowed):
            calls.append(query)
            ready.wait(2)
            return {"state": "ready"}
        backend = self.future_backend(retrieve)
        self.addCleanup(ready.set)
        self.assertEqual(backend.search("public fixture", timeout=.01)["state"], "pending")
        ready.set()
        self.assertEqual(backend.search("public fixture", timeout=1)["state"], "ready")
        self.assertEqual(calls, ["public fixture"])

    def test_default_wait_keeps_same_future_beyond_its_cache_ttl(self):
        import time
        gate = threading.Event()
        calls = []
        def retrieve(query, allowed):
            calls.append(query)
            gate.wait(2)
            return {'state': 'ready'}
        backend = self.future_backend(retrieve)
        self.addCleanup(gate.set)
        self.assertEqual(backend.search('public fixture', timeout=0)['state'], 'pending')
        key = next(iter(backend.cache))
        original = backend.cache[key][1]
        backend.cache[key] = (time.monotonic() - 25 * 3600, original)
        self.assertEqual(backend.search('public fixture', timeout=0)['state'], 'pending')
        self.assertIs(backend.cache[key][1], original)
        backend.cache[key] = (time.monotonic(), original)
        gate.set()
        self.assertEqual(backend.search('public fixture')['state'], 'ready')
        self.assertEqual(len(calls), 1)

    def test_failed_free_retrieval_can_be_explicitly_retried(self):
        calls = []
        def retrieve(query, allowed):
            calls.append(query)
            if len(calls) == 1: raise OSError('fixture')
            return {'state': 'ready'}
        backend = self.future_backend(retrieve)
        self.assertEqual(backend.search('public fixture')['state'], 'failed')
        self.assertEqual(backend.search('public fixture')['state'], 'ready')
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
