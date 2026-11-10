"""Full-section lexical repairs keep source bodies and topic identities intact."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from persona_runtime.knowledge_lexical import ChineseLexicalIndex
from persona_runtime.knowledge_lexical_search import EncyclopediaLexicalSearch
from persona_runtime.knowledge_package import file_digest
from persona_runtime.paths import RuntimePaths


class FullLexicalTests(unittest.TestCase):
    def setUp(self):
        root = Path("D:/Persona-Centric Agent Runtime/tmp/m9a-full-lexical-tests")
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.paths = RuntimePaths(Path(temporary.name))
        self.package = self.paths.home / "package"
        self.package.mkdir()
        self.source = self.package / "body.db"
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("""
                CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT NOT NULL,entity TEXT NOT NULL);
                CREATE TABLE sections(id INTEGER PRIMARY KEY,article_id INTEGER,title TEXT,content TEXT);
                CREATE INDEX section_articles ON sections(article_id);
                CREATE VIRTUAL TABLE section_search USING fts5(title,content,
                    content='sections',content_rowid='id',tokenize='trigram');
            """)
            db.executemany("INSERT INTO articles VALUES(?,?,?)", [
                (1, "文件去重", ""), (2, "生物特征", ""), (3, "地理总览", ""),
                (4, "冰川", ""), (5, "自行车", ""), (6, "电动自行车", "")])
            db.executemany("INSERT INTO sections VALUES(?,?,?,?)", [
                (11, 1, "", "重複的資料可以透過比對檔案移除。"),
                (22, 2, "", "特徵是描述生物差異的性質。"),
                (33, 3, "冰川", "冰川是多年積雪形成的巨大冰體。"),
                (44, 4, "", "冰川是緩慢移動的天然冰體。"),
                (55, 5, "", "自行車以人力踩踏板行進。"),
                (66, 6, "", "电动自行车使用电池辅助行进。")])
            db.execute("INSERT INTO section_search(rowid,title,content) SELECT id,title,content FROM sections")
            db.commit()
        self.fingerprint = file_digest(self.source)
        (self.package / "manifest.json").write_text(json.dumps({"database": {
            "sha256": self.fingerprint, "file": self.source.name, "counts": {"sections": 6}}}),
            encoding="utf-8")
        self.lexical = ChineseLexicalIndex(self.paths)
        self.searcher = EncyclopediaLexicalSearch(self.paths, self.package, self.lexical)
        self.addCleanup(self.searcher.close)

    def test_two_character_terms_and_contextual_variants_reach_original_sections(self):
        for query, article_id in [("重复是什么", 1), ("特征是什么", 2)]:
            result = self.searcher.search_views(query, threading.Event())
            self.assertEqual(result["views"][0][0]["article_id"], article_id)
            self.assertTrue(result["terms"])
        self.assertEqual(file_digest(self.source), self.fingerprint)

    def test_section_definition_route_keeps_origin_separate_from_target(self):
        result = self.searcher.search_views("多年积雪形成冰川是什么", threading.Event())
        topics = result["views"][3]
        topic = next(row for row in topics if row["article_id"] == 4)
        self.assertEqual(topic["origin_article_id"], 3)
        self.assertEqual(topic["origin_section_id"], 33)
        self.assertNotIn("section_id", topic)

    def test_related_title_expansion_uses_existing_word_boundary_titles(self):
        result = self.searcher.related_titles(
            [{"article_id": 6, "title": "电动自行车"}], "电池驱动的骑行车辆")
        row = next(row for row in result if row["article_id"] == 5)
        self.assertEqual(row["origin_article_id"], 6)
        self.assertNotIn("section_id", row)

    def test_stop_numeric_terms_and_explicit_disambiguation(self):
        stopped = threading.Event()
        stopped.set()
        with self.assertRaisesRegex(RuntimeError, "knowledge_stopped"):
            self.searcher.search_views("公开主题", stopped)
        self.searcher.words("两个仪器")
        self.assertTrue(self.searcher.quantity("两个"))
        self.assertFalse(self.searcher.quantity("2001"))
        self.assertEqual(self.searcher.words("技术是什么"), ["技术"])
        row = {"article_id": 7, "title": "冰川 (消歧义)"}
        self.assertFalse(self.searcher.accepted(row, "冰川是什么"))
        self.assertTrue(self.searcher.accepted(row, "冰川有哪些同名含义"))

    def test_title_projection_checksum_rejects_corrupted_reuse(self):
        with self.searcher.titles.database.open("r+b") as stream:
            stream.seek(100)
            stream.write(b"invalid")
        with self.assertRaisesRegex(ValueError, "knowledge_title_projection_checksum_mismatch"):
            self.searcher.titles.prepare()

    def test_readers_are_reused_and_closed_with_the_component(self):
        self.searcher.search_views("重复是什么", threading.Event())
        readers = self.searcher._connections
        self.searcher.search_views("特征是什么", threading.Event())
        self.assertIs(self.searcher._connections, readers)
        self.searcher.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            readers[0].execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, "knowledge_stopped"):
            self.searcher.search_views("重复是什么", threading.Event())

    def test_close_releases_readers_after_an_inflight_search(self):
        entered = threading.Event()
        def waiting_candidates(connection, terms):
            entered.set()
            while not self.searcher.closed:
                time.sleep(.01)
            raise sqlite3.OperationalError("interrupted")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(self.searcher, "full_candidates", waiting_candidates):
                future = executor.submit(self.searcher.search_views, "重复是什么", threading.Event())
                self.assertTrue(entered.wait(2))
                self.searcher.close()
                with self.assertRaises(sqlite3.OperationalError):
                    future.result(timeout=2)
        self.assertIsNone(self.searcher._connections)


if __name__ == "__main__":
    unittest.main()
