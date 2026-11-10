"""Chinese lexical views over original sections and verified title identities."""

from contextlib import closing, ExitStack
import ctypes
import math
import re
import sqlite3
import threading
import time

from .knowledge_titles import ChineseTitleIndex, title_units


_query_noise = {"内容", "技术", "表示", "逐渐", "下来", "起来", "位于"}


def ordered_overlap(first, second):
    previous = [0] * (len(second) + 1)
    for character in first:
        current = [0]
        for index, other in enumerate(second):
            if character == other:
                current.append(previous[index] + 1)
            else:
                current.append(max(previous[index + 1], current[-1]))
        previous = current
    return previous[-1]


class EncyclopediaLexicalSearch:
    def __init__(self, paths, package, lexical):
        from opencc import OpenCC
        self.text = lexical.text
        self.simple = OpenCC("t2s")
        self.traditional = OpenCC("s2t")
        self.taiwan = OpenCC("s2tw")
        self.titles = ChineseTitleIndex(paths, package, self.text)
        self.titles.prepare()
        self.source = self.titles.source
        self._reader_lock = threading.RLock()
        self._readers = ExitStack()
        self._connections = None
        self.closed = False
        self.section_count = self.titles.section_count
        if self.section_count is None:
            with closing_connection(self.source) as connection:
                self.section_count = connection.execute("SELECT count(*) FROM sections").fetchone()[0]

    def forms(self, word):
        length = self.text.mapping(0x0804, 0x04000000, word, -1, None, 0)
        buffer = ctypes.create_unicode_buffer(length)
        if not length or not self.text.mapping(0x0804, 0x04000000, word, -1, buffer, length):
            raise ctypes.WinError(ctypes.get_last_error())
        return list(dict.fromkeys([word, buffer.value,
                                  self.traditional.convert(word), self.taiwan.convert(word)]))

    def words(self, query):
        original = self.text.query_words(query)
        words = list(dict.fromkeys(self.simple.convert(word) for word in original
                                   if word not in _query_noise))[:12]
        return words or original[:12]

    def quantity(self, word):
        tags = [item.flag for item in self.text.tagger.cut(word) if item.word.strip()]
        return bool(tags) and all(tag == "m" for tag in tags) and not word.isdigit()

    def accepted(self, row, query):
        return (bool(re.search(r"消歧|同名|多种含义|分别指", self.text.simplify(query)))
                or "消歧" not in self.text.simplify(row["title"]))

    def terms(self, connection, words):
        result = []
        for word in words:
            if len(word) < 2 or self.quantity(word):
                continue
            tags = [item.flag for item in self.text.tagger.cut(word) if item.word.strip()]
            core = any(tag.startswith("n") or tag in {"l", "i"} for tag in tags)
            tokens = []
            forms = self.forms(word)
            for form in forms:
                if len(form) >= 3:
                    tokens.append(form)
                else:
                    rows = connection.execute(
                        "SELECT term,doc FROM terms WHERE term>=? AND term<? "
                        "ORDER BY doc DESC LIMIT 48", (form, form + "\uffff"))
                    tokens.extend(row[0] for row in rows)
            tokens = list(dict.fromkeys(tokens))
            if not tokens:
                continue
            expression = "(" + " OR ".join('"' + token.replace('"', '""') + '"'
                                           for token in tokens) + ")"
            frequency = connection.execute(
                "SELECT count(*) FROM section_search WHERE section_search MATCH ?",
                (expression,)).fetchone()[0]
            weight = math.log1p(self.section_count / max(1, frequency)) * (2 if core else .5)
            result.append({"word": word, "forms": forms, "expression": expression,
                           "frequency": frequency, "weight": weight, "core": core})
        return result

    def full_candidates(self, connection, terms):
        if not terms:
            return []
        parts = ["SELECT rowid,? AS hits FROM section_search WHERE section_search MATCH ?"
                 for term in terms]
        parameters = [value for term in terms for value in (term["weight"], term["expression"])]
        expression = " OR ".join(term["expression"] for term in terms)
        rows = connection.execute(
            "WITH matches AS (" + " UNION ALL ".join(parts) + "), "
            "coverage AS (SELECT rowid,sum(hits) AS hits FROM matches GROUP BY rowid) "
            "SELECT a.id,a.title,s.id,s.title,s.content,c.hits,bm25(section_search) "
            "FROM section_search JOIN sections s ON s.id=section_search.rowid "
            "JOIN articles a ON a.id=s.article_id JOIN coverage c ON c.rowid=s.id "
            "WHERE section_search MATCH ? ORDER BY c.hits DESC,bm25(section_search) LIMIT 150",
            (*parameters, expression)).fetchall()
        core = [term for term in terms if term["core"]]
        result = []
        for article_id, title, section_id, heading, body, score, bm25 in rows:
            paragraph = re.split(r"\n\s*\n", body, 1)[0][:240]
            pieces = re.split(r"(?:是|指|為|为)", paragraph, 1)
            definition = pieces[1] if len(pieces) == 2 else ""
            positions = []
            for term in core:
                found = [definition.find(form) for form in term["forms"] if form in definition]
                if found:
                    positions.append((min(found), len(term["word"])))
            bonus = 0
            if len(core) >= 2 and len(positions) == len(core):
                span = max(position + size for position, size in positions) - min(
                    position for position, size in positions)
                bonus = sum(term["weight"] for term in core) / (1 + span / 80)
            result.append({"article_id": article_id, "title": title, "section_id": section_id,
                           "heading": heading, "body": body, "word_matches": score,
                           "lexical_score": score + bonus, "bm25": bm25,
                           "type": "PCAR-section-lexical"})
        result.sort(key=lambda row: (-row["lexical_score"], row["bm25"]))
        return result

    def title_candidates(self, connection, source, words):
        words = [word for word in words if not self.quantity(word)]
        characters = list(dict.fromkeys(title_units(" ".join(words))))
        if not characters:
            return []
        expression = " OR ".join('"' + character + '"' for character in characters)
        rows = connection.execute(
            "SELECT t.article_id,t.title,t.normalized,bm25(title_search) "
            "FROM title_search JOIN titles t ON t.article_id=title_search.rowid "
            "WHERE title_search MATCH ? ORDER BY bm25(title_search) LIMIT 200", (expression,))
        candidates = []
        for article_id, title, normalized, bm25 in rows:
            units = set(title_units(normalized))
            found = units.intersection(characters)
            groups = sum(bool(set(title_units(word)).intersection(found)) for word in words)
            coverage = len(found) / max(1, len(units))
            if groups < 2 or coverage < 2/3:
                continue
            candidates.append({"article_id": article_id, "title": title, "groups": groups,
                               "coverage": coverage, "bm25": bm25,
                               "ordered": ordered_overlap(normalized, "".join(words))
                                          / max(1, len(normalized)), "type": "PCAR-title-lexical"})
        candidates.sort(key=lambda row: (-row["groups"], -row["coverage"], -row["ordered"], row["bm25"]))
        result = []
        forms = [form for word in words for form in self.forms(word)]
        for row in candidates:
            body = ""
            for (part,) in source.execute(
                    "SELECT content FROM sections WHERE article_id=? ORDER BY id", (row["article_id"],)):
                body += part + "\n"
                if len(body) > 12000:
                    break
            if any(form in body[:12000] for form in forms):
                result.append(row)
            if len(result) == 10:
                break
        return result

    def exact_titles(self, connection, query, words):
        terms = list(words) if len(words) == 1 else []
        for item in self.text.tagger.cut(self.text.simplify(query)):
            if item.flag == "ns" and len(item.word) > 1:
                terms.extend([item.word, item.word + "市", item.word + "县", item.word + "省"])
        return self.titles.lookup(connection, terms)

    def heading_topics(self, connection, candidates):
        result = []
        seen = set()
        for row in candidates[:30]:
            heading = self.text.simplify(row["heading"]).strip()
            if not 2 <= len(heading) <= 24:
                continue
            prefix = row["body"].lstrip()[:80]
            forms = self.forms(heading)
            defines_heading = any(prefix.startswith(form) and re.search(
                r"是|指|為|为", prefix[len(form):len(form) + 24]) for form in forms)
            if not defines_heading:
                continue
            for topic in self.titles.lookup(connection, [heading]):
                if topic["article_id"] == row["article_id"] or topic["article_id"] in seen:
                    continue
                seen.add(topic["article_id"])
                result.append({**topic, "origin_article_id": row["article_id"],
                               "origin_section_id": row["section_id"], "origin_heading": row["heading"],
                               "type": "PCAR-section-topic"})
        return result

    def _read_connections(self):
        if self._connections is None:
            with ExitStack() as opening:
                source = opening.enter_context(closing_connection(self.source))
                titles = opening.enter_context(closing_connection(self.titles.database))
                source.execute("PRAGMA cache_size=-131072")
                source.execute("PRAGMA temp_store=MEMORY")
                source.execute("CREATE VIRTUAL TABLE temp.terms USING fts5vocab(main,section_search,'row')")
                self._readers = opening.pop_all()
                self._connections = (source, titles)
        return self._connections

    def search_views(self, query, stop_event):
        started = time.monotonic()
        with self._reader_lock:
            if self.closed or stop_event.is_set():
                raise RuntimeError("knowledge_stopped")
            words = self.words(query)
            source, titles = self._read_connections()
            source.set_progress_handler(lambda: self.closed or stop_event.is_set(), 10000)
            try:
                terms = self.terms(source, words)
                candidates = self.full_candidates(source, terms)
                title_hits = self.title_candidates(titles, source, words)
                exact = self.exact_titles(titles, query, words)
                headings = self.heading_topics(titles, candidates)
            finally:
                source.set_progress_handler(None, 0)
        full = []
        seen = set()
        for row in candidates:
            if row["article_id"] in seen:
                continue
            seen.add(row["article_id"])
            full.append({key: value for key, value in row.items()
                         if key not in {"body", "heading", "bm25"}})
        views = [[row for row in view if self.accepted(row, query)]
                 for view in (full, title_hits, exact, headings)]
        return {"views": views, "seconds": time.monotonic() - started,
                "words": words, "terms": [{key: value for key, value in term.items()
                                           if key != "expression"} for term in terms]}

    def related_titles(self, semantic, query):
        result = []
        seen = set()
        with self._reader_lock:
            if self.closed:
                raise RuntimeError("knowledge_stopped")
            _, connection = self._read_connections()
            for row in semantic[:5]:
                title = self.text.simplify(re.split(r"[（(]", row["title"], 1)[0]).strip()
                segments = [item.word for item in self.text.tagger.cut(title) if item.word.strip()]
                terms = []
                for offset in range(1, len(segments)):
                    terms.extend(part for part in ("".join(segments[:offset]), "".join(segments[offset:]))
                                 if len(part) >= 3)
                for topic in self.titles.lookup(connection, terms):
                    if topic["article_id"] in seen or not self.accepted(topic, query):
                        continue
                    seen.add(topic["article_id"])
                    result.append({**topic, "type": "PCAR-related-title",
                                   "origin_article_id": row["article_id"], "origin_title": row["title"]})
        return result

    def close(self):
        self.closed = True
        with self._reader_lock:
            self._readers.close()
            self._connections = None


def closing_connection(database):
    return closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, check_same_thread=False))
