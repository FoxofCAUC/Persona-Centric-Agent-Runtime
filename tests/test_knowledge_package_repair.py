"""A derived package must preserve section identities and exact embedding bytes."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from persona_runtime.knowledge_package_repair import copy_chinese_content, write_clustered_vectors


class PackageRepairTests(unittest.TestCase):
    def setUp(self):
        root = Path("D:/Persona-Centric Agent Runtime/tmp/m9a-repair-tests")
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def test_chinese_cohort_excludes_foreign_sections_under_same_article(self):
        source = self.work / "source.db"
        destination = self.work / "derived.db"
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.executescript("""
                CREATE TABLE setup(key TEXT PRIMARY KEY,value BLOB);
                CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT,entity TEXT);
                CREATE TABLE sections(id INTEGER PRIMARY KEY,article_id INTEGER,title TEXT,content TEXT,pow INTEGER);
                INSERT INTO articles VALUES(1,'月球','Q405'),(2,'Foreign','Q2');
                INSERT INTO sections VALUES(1,1,'','Testo di altro argomento',0),
                    (2,2,'','Foreign text',0),(3,1,'','月球是地球的天然卫星。',0);
            """)
            connection.executemany("INSERT INTO setup VALUES(?,?)", [("language", "en"), ("gguf", b"model")])
        with patch("persona_runtime.knowledge_package_repair.CHINESE_SECTION_START", 3):
            copy_chinese_content(source, destination)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(connection.execute("SELECT id FROM articles").fetchall(), [(1,)])
            self.assertEqual(connection.execute("SELECT id,content FROM sections").fetchall(),
                             [(3, "月球是地球的天然卫星。")])
            self.assertEqual(connection.execute("SELECT value FROM setup WHERE key='gguf'").fetchone()[0], b"model")
            self.assertEqual(connection.execute("SELECT rowid FROM article_search WHERE article_search MATCH '月球是'").fetchall(), [])
            self.assertEqual(connection.execute("SELECT rowid FROM section_search WHERE section_search MATCH '天然卫星'").fetchall(), [(3,)])
        with closing(sqlite3.connect(source)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sections").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT value FROM setup WHERE key='language'").fetchone()[0], "en")

    def test_repacked_vectors_preserve_exact_bytes_and_section_binding(self):
        destination = self.work / "vectors.db"
        with closing(sqlite3.connect(destination)) as connection, connection:
            connection.executescript("""
                CREATE TABLE sections(id INTEGER PRIMARY KEY);
                INSERT INTO sections VALUES(10),(20),(30);
                CREATE TABLE vectors_ann_chunks(id INTEGER PRIMARY KEY,chunk BLOB);
                CREATE TABLE vectors_ann_index(id INTEGER PRIMARY KEY,vectors_id INTEGER,chunk_id INTEGER,chunk_position INTEGER);
                CREATE TABLE vectors_ann_centroids(id INTEGER PRIMARY KEY,centroid BLOB);
                CREATE TABLE vectors_ann_centroid_chunks(centroid_id INTEGER,chunk_id INTEGER);
            """)
        vectors = np.array([[1,0],[0,1],[.6,.8]], dtype="<f4")
        ids = np.array([10,20,30], dtype="<i8")
        result = write_clustered_vectors(destination, vectors, ids, self.work, lambda *a, **kw: None)
        self.assertEqual(result["vectors"], 3)
        with closing(sqlite3.connect(destination)) as connection:
            for ident, chunk, position in connection.execute("SELECT vectors_id,chunk_id,chunk_position FROM vectors_ann_index"):
                raw = connection.execute("SELECT chunk FROM vectors_ann_chunks WHERE id=?", (chunk,)).fetchone()[0]
                self.assertEqual(raw[position*8:(position+1)*8], vectors[list(ids).index(ident)].tobytes())


if __name__ == "__main__":
    unittest.main()
