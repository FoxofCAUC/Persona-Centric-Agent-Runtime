"""Rebuildable Chinese lexical projection over immutable encyclopedia bodies."""

from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import os
import logging

from .paths import RuntimePaths

from .character_embedding import safe_path
from .config import atomic_json
from .knowledge_package import file_digest, utc_now


VERSION = "zh-windows-simplified-bigrams-lead1500-v1"
_words = re.compile(r"[\u3400-\u9fff]+|[a-z0-9][a-z0-9_-]{1,39}", re.IGNORECASE)
_questions = re.compile(
    r"请问|解释一下|帮我解释|[为叫是由]?什[么麽麼]|是谁|怎么|怎样|如何|哪个|哪里|"
    r"听起来|主要|通常|还有|有何|到底|一下|的|了|吗|呢|呀|啊")
_contrast = re.compile(r"(?<![\u3400-\u9fff])([\u3400-\u9fff])(?:还是|或是|或者|或)([\u3400-\u9fff])(?![\u3400-\u9fff])")
_query_stops = {"用", "把", "为", "受到", "而", "再", "到", "很", "更", "会", "是", "有", "里", "中",
                "将", "由", "以", "方式", "时候", "方面", "东西", "名字", "名称", "过程", "一种", "一个",
                "决定", "描述", "解释", "通常", "主要"}


class ChineseText:
    def __init__(self, paths=None):
        self.paths = paths or RuntimePaths.from_environment()
        self.segmenter = None
        self.mapping = ctypes.WinDLL("kernel32", use_last_error=True).LCMapStringW
        self.mapping.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
                                 ctypes.c_int, wintypes.LPWSTR, ctypes.c_int]
        self.mapping.restype = ctypes.c_int

    def simplify(self, text):
        text = text.replace("\x00", "")
        length = self.mapping(0x0804, 0x02000000, text, -1, None, 0)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        output = ctypes.create_unicode_buffer(length)
        if not self.mapping(0x0804, 0x02000000, text, -1, output, length):
            raise ctypes.WinError(ctypes.get_last_error())
        return output.value.lower()

    def tokens(self, text, *, query=False):
        normalized = " ".join(self.query_words(text)) if query else self.simplify(text)
        tokens = []
        for match in _words.finditer(normalized):
            word = match.group()
            if "\u3400" <= word[0] <= "\u9fff":
                tokens.extend(word[index:index + 2] for index in range(len(word) - 1))
                if len(word) == 1:
                    tokens.append(word)
            else:
                tokens.append(word)
        return tokens

    def query_words(self, text):
        if self.segmenter is None:
            import jieba
            from jieba.posseg import POSTokenizer
            directory = safe_path(self.paths.home, self.paths.home / "cache/knowledge/segmentation")
            directory.mkdir(parents=True, exist_ok=True)
            jieba.setLogLevel(logging.ERROR)
            tokenizer = jieba.Tokenizer()
            tokenizer.tmp_dir = str(directory)
            self.segmenter = POSTokenizer(tokenizer)
        normalized = _questions.sub(" ", self.simplify(text))
        normalized = _contrast.sub(r"\1\2", normalized).replace("还是", " ")
        words = []
        for item in self.segmenter.cut(normalized):
            if (not item.word.strip() or item.word in _query_stops
                    or item.flag[0] in {"p", "r", "u", "c", "x", "e", "y"}):
                continue
            if (words and item.flag.startswith("n")
                    and re.fullmatch(r"第[零一二三四五六七八九十百千0-9]+(?:次|条|项|代|届|章|课|集)?", words[-1])):
                words[-1] += item.word
            else:
                words.append(item.word)
        return words

    def sessions(self, text):
        normalized = self.simplify(text).strip()
        normalized = re.sub(r"^(?:请问|请解释|解释一下|为什么)", "", normalized)
        normalized = re.sub(r"(?:主要由什[么麽麼]决定|是什[么麽麼](?:技术|原理|东西)?|"
                            r"叫什[么麽麼](?:名字)?|是怎么回事)[?？吗呢呀]*$", "", normalized)
        normalized = re.sub(r"(?:的过程|的仪器)$", "", normalized)
        parts = re.split(r"[，。？！；,!?;\n]+|(?:怎样|怎么|如何)(?:把|将)?|再|而|把", normalized)
        parts = [part.strip(" ?？的") for part in parts if part.strip(" ?？的")]
        # Keep contiguous wording and grammatical relationships within each query unit.
        if len(parts) > 1 and len(parts[0]) <= 4:
            parts[1] = parts[0] + parts[1]
        groups = []
        for part in parts:
            if len(part) <= 64:
                groups.append(part)
            else:
                groups.extend((part[:64], part[-64:]))
        selected = groups if len(groups) <= 2 else [groups[0], groups[-1]]
        return list(dict.fromkeys(selected)) or [normalized[:64] or text[:64]]


class ChineseLexicalIndex:
    def __init__(self, paths):
        self.paths = paths
        self.text = ChineseText(paths)

    def prepare(self, package, progress=None):
        progress = progress or (lambda *a, **kw: None)
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        identity = manifest["database"]["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("knowledge_lexical_source_identity_invalid")
        source = safe_path(package, package / manifest["database"]["file"])
        directory = safe_path(self.paths.home,
            self.paths.home / "data/knowledge/indexes" / identity / VERSION)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "lexical.db"
        receipt_path = directory / "manifest.json"
        if destination.exists() and receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["source_sha256"] != identity or receipt["version"] != VERSION:
                raise ValueError("knowledge_lexical_projection_identity_mismatch")
            if file_digest(destination) != receipt["sha256"]:
                raise ValueError("knowledge_lexical_projection_checksum_mismatch")
            return destination
        if file_digest(source) != identity:
            raise ValueError("knowledge_lexical_source_checksum_mismatch")
        temporary = directory / "lexical.db.building"
        if temporary.exists():
            raise ValueError("knowledge_lexical_incomplete_projection_requires_recovery")
        started = time.monotonic()
        count = 0
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as incoming:
            incoming.execute("PRAGMA query_only=ON")
            incoming.execute("PRAGMA cache_size=-131072")
            with closing(sqlite3.connect(temporary)) as outgoing:
                outgoing.execute("PRAGMA cache_size=-131072")
                outgoing.execute("PRAGMA temp_store=MEMORY")
                outgoing.executescript("""
                    CREATE TABLE documents(article_id INTEGER PRIMARY KEY,title TEXT NOT NULL,
                        section_id INTEGER NOT NULL,body_sha256 TEXT NOT NULL);
                    CREATE VIRTUAL TABLE search USING fts5(title,body,content='');
                """)
                rows = incoming.execute("SELECT a.id,a.title,s.id,s.content FROM sections s "
                    "JOIN articles a ON a.id=s.article_id WHERE s.title='' AND s.content!='' ORDER BY s.id")
                for article_id, title, section_id, content in rows:
                    body = content[:1500]
                    outgoing.execute("INSERT INTO documents VALUES(?,?,?,?)",
                        (article_id, title, section_id, hashlib.sha256(body.encode("utf-8")).hexdigest()))
                    outgoing.execute("INSERT INTO search(rowid,title,body) VALUES(?,?,?)",
                        (article_id, " ".join(self.text.tokens(title)), " ".join(self.text.tokens(body))))
                    count += 1
                    if count % 10000 == 0:
                        outgoing.commit()
                        progress("lexical_building", articles=count, seconds=time.monotonic() - started)
                outgoing.commit()
                outgoing.execute("INSERT INTO search(search) VALUES('optimize')")
                outgoing.commit()
                if outgoing.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("knowledge_lexical_projection_integrity_failed")
        receipt = {"version": VERSION, "source_sha256": identity, "articles": count,
                   "sha256": file_digest(temporary), "created_at": utc_now(),
                   "indexed_body_characters": 1500, "authority": False}
        os.replace(temporary, destination)
        atomic_json(receipt_path, receipt)
        progress("lexical_ready", articles=count, seconds=time.monotonic() - started)
        return destination

    def search(self, database, query, limit=10):
        words = list(dict.fromkeys(self.text.query_words(query)))[:12]
        if not words:
            return []
        phrases = ['"' + " ".join(self.text.tokens(word)).replace('"', '""') + '"' for word in words]
        phrases = [phrase for phrase in phrases if phrase != '""']
        if not phrases:
            return []
        parts = []
        parameters = []
        for phrase in phrases:
            parts.append("SELECT rowid,1 AS coverage,0 AS title_hits FROM search WHERE search MATCH ?")
            parts.append("SELECT rowid,0 AS coverage,1 AS title_hits FROM search WHERE search MATCH ?")
            parameters.extend((phrase, "title:" + phrase))
        expression = " OR ".join(phrases)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA temp_store=MEMORY")
            rows = connection.execute("WITH matches AS (" + " UNION ALL ".join(parts) + "), "
                "coverage AS (SELECT rowid,sum(coverage) AS hits,sum(title_hits) AS title_hits "
                "FROM matches GROUP BY rowid) "
                "SELECT d.article_id,d.title,d.section_id,bm25(search,3,1) AS score,c.hits "
                "FROM search JOIN documents d ON d.article_id=search.rowid "
                "JOIN coverage c ON c.rowid=search.rowid WHERE search MATCH ? "
                "ORDER BY c.hits DESC,c.title_hits*1.0/max(1,length(d.title)) DESC,score LIMIT ?",
                (*parameters, expression, limit)).fetchall()
        return [{"article_id": row[0], "title": row[1], "section_id": row[2],
                 "lexical_score": row[3], "word_matches": row[4], "type": "PCAR-lexical"} for row in rows]
