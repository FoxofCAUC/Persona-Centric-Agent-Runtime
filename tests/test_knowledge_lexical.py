"""Chinese lexical queries use a projection and keep original body identities."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from persona_runtime.knowledge_lexical import ChineseLexicalIndex, ChineseText
from persona_runtime.knowledge_package import file_digest
from persona_runtime.paths import RuntimePaths


class ChineseLexicalTests(unittest.TestCase):
    def test_simplification_and_question_words(self):
        text = ChineseText()
        self.assertEqual(text.simplify("臺灣網際網路"), "台湾网际网路")
        self.assertEqual(text.tokens("地球的天然衛星叫什麼", query=True),
                         ["地球", "天然", "卫星"])
        self.assertEqual(text.tokens("声音听起来高还是低主要由什么决定", query=True),
                         ["声音", "高低"])
        sessions = text.sessions("海水蒸发形成云再下雨回到地面的过程叫什么")
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(session.strip() for session in sessions))
        self.assertIn("第二定律", text.query_words("牛顿第二定律描述了什么"))

    def test_two_character_words_retrieve_original_article_and_section(self):
        root = Path("D:/Persona-Centric Agent Runtime/tmp/m9a-lexical-tests")
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            paths = RuntimePaths(Path(temporary))
            package = paths.home / "package"
            package.mkdir()
            source = package / "body.db"
            with closing(sqlite3.connect(source)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT);
                    CREATE TABLE sections(id INTEGER PRIMARY KEY,article_id INTEGER,title TEXT,content TEXT);
                    INSERT INTO articles VALUES(1,'月球'),(2,'地球'),(3,'北京');
                    INSERT INTO sections VALUES(11,1,'','月球是地球唯一的天然衛星。'),
                        (22,2,'','地球是太陽系中的一顆行星。'),(33,3,'','北京是中國的首都。');
                """)
            before = file_digest(source)
            (package / "manifest.json").write_text(json.dumps({"database": {
                "sha256": before, "file": "body.db"}}), encoding="utf-8")
            service = ChineseLexicalIndex(paths)
            index = service.prepare(package)
            result = service.search(index, "地球的天然卫星叫什么")
            self.assertEqual(result[0]["article_id"], 1)
            self.assertEqual(result[0]["section_id"], 11)
            self.assertEqual(service.search(index, "北京是什么")[0]["article_id"], 3)
            self.assertEqual(file_digest(source), before)
            self.assertEqual(service.prepare(package), index)


if __name__ == "__main__":
    unittest.main()
