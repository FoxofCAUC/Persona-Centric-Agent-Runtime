"""Small rebuildable title projection for the immutable encyclopedia package."""

from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3

from .character_embedding import safe_path
from .config import atomic_json
from .knowledge_package import file_digest, utc_now


TITLE_VERSION = "zh-windows-character-titles-v1"


def title_units(value):
    return re.findall(r"[\u3400-\u9fff]|[a-z0-9][a-z0-9_-]*", value)


class ChineseTitleIndex:
    def __init__(self, paths, package, text):
        self.text = text
        package = Path(package)
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        self.identity = manifest["database"]["sha256"]
        self.section_count = manifest["database"].get("counts", {}).get("sections")
        if not re.fullmatch(r"[a-f0-9]{64}", self.identity):
            raise ValueError("knowledge_title_source_identity_invalid")
        self.source = safe_path(package, package / manifest["database"]["file"])
        self.directory = safe_path(paths.home,
            paths.home / "data/knowledge/indexes" / self.identity / TITLE_VERSION)
        self.database = self.directory / "titles.db"

    def prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        receipt_path = self.directory / "manifest.json"
        if self.database.exists() and receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["version"] != TITLE_VERSION or receipt["source_sha256"] != self.identity:
                raise ValueError("knowledge_title_projection_identity_mismatch")
            if file_digest(self.database) != receipt["sha256"]:
                raise ValueError("knowledge_title_projection_checksum_mismatch")
            return self.database
        if file_digest(self.source) != self.identity:
            raise ValueError("knowledge_title_source_checksum_mismatch")
        temporary = self.directory / "titles.db.building"
        temporary.touch(exist_ok=False)
        count = 0
        with closing(sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)) as incoming:
            with closing(sqlite3.connect(temporary)) as outgoing:
                outgoing.execute("PRAGMA cache_size=-131072")
                outgoing.execute("PRAGMA temp_store=MEMORY")
                outgoing.executescript("""
                    CREATE TABLE titles(article_id INTEGER PRIMARY KEY,title TEXT NOT NULL,
                        normalized TEXT NOT NULL);
                    CREATE VIRTUAL TABLE title_search USING fts5(characters,content='');
                """)
                for article_id, title in incoming.execute("SELECT id,title FROM articles ORDER BY id"):
                    normalized = self.text.simplify(title)
                    outgoing.execute("INSERT INTO titles VALUES(?,?,?)", (article_id, title, normalized))
                    outgoing.execute("INSERT INTO title_search(rowid,characters) VALUES(?,?)",
                                     (article_id, " ".join(title_units(normalized))))
                    count += 1
                    if count % 10000 == 0:
                        outgoing.commit()
                outgoing.commit()
                outgoing.execute("CREATE INDEX normalized_titles ON titles(normalized)")
                outgoing.execute("INSERT INTO title_search(title_search) VALUES('optimize')")
                outgoing.commit()
                if outgoing.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("knowledge_title_projection_integrity_failed")
        receipt = {"version": TITLE_VERSION, "source_sha256": self.identity,
                   "sha256": file_digest(temporary), "articles": count,
                   "created_at": utc_now(), "authority": False, "bytes": temporary.stat().st_size}
        os.replace(temporary, self.database)
        atomic_json(receipt_path, receipt)
        return self.database

    def lookup(self, connection, terms):
        result = []
        seen = set()
        for term in dict.fromkeys(terms):
            normalized = self.text.simplify(term)
            for article_id, title in connection.execute(
                    "SELECT article_id,title FROM titles WHERE normalized=?", (normalized,)):
                if article_id not in seen:
                    seen.add(article_id)
                    result.append({"article_id": article_id, "title": title})
        return result
