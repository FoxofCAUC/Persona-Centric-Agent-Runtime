"""Repair the single pinned mixed-language upstream package without re-embedding it.

The upstream archive and database remain untouched. The section cohort boundary
is specific to this SHA and was identified before any repaired retrieval result.
"""

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3

from .config import atomic_json
from .knowledge_package import BLOCK_SIZE, file_digest, utc_now


UPSTREAM_SHA256 = "855082b33170350f736943b1abc890a3e462d356a4b586e715d7089f83b2e134"
CHINESE_SECTION_START = 9584771
PIPELINE = "wikilite-zh-cohort-ivf-v1"


def train_centroids(vectors, count, progress):
    import numpy as np

    rng = np.random.default_rng(20261109)
    sample_size = min(len(vectors), 100000)
    sample = np.asarray(vectors[np.sort(rng.choice(len(vectors), sample_size, replace=False))])
    centers = sample[np.linspace(0, sample_size - 1, count, dtype=np.int64)].copy()
    for iteration in range(8):
        sums = np.zeros_like(centers)
        sizes = np.zeros(count, dtype=np.int64)
        for start in range(0, len(sample), 4096):
            batch = sample[start:start + 4096]
            assignment = np.argmax(batch @ centers.T, axis=1)
            np.add.at(sums, assignment, batch)
            sizes += np.bincount(assignment, minlength=count)
        nonempty = sizes > 0
        centers[nonempty] = sums[nonempty]
        norms = np.linalg.norm(centers, axis=1, keepdims=True)
        centers /= np.maximum(norms, 1e-12)
        progress("repair_training", iteration=iteration + 1, iterations=8, centroids=count)
    return centers


def copy_chinese_content(source, destination):
    with closing(sqlite3.connect(destination.as_uri(), uri=True)) as connection:
        connection.execute("PRAGMA cache_size=-262144")
        connection.execute("PRAGMA mmap_size=268435456")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("ATTACH DATABASE ? AS upstream", (source.as_uri() + "?mode=ro",))
        connection.executescript("""
            CREATE TABLE setup(key TEXT PRIMARY KEY,value BLOB);
            CREATE TABLE articles(id INTEGER PRIMARY KEY,title TEXT NOT NULL,entity TEXT NOT NULL);
            CREATE TABLE sections(id INTEGER PRIMARY KEY,article_id INTEGER NOT NULL,
                title TEXT,content TEXT,content_flate BLOB,pow INTEGER DEFAULT 0,
                FOREIGN KEY(article_id) REFERENCES articles(id));
            CREATE TABLE vectors(id INTEGER PRIMARY KEY,embedding BLOB);
            CREATE TABLE vectors_ann_chunks(id INTEGER PRIMARY KEY,chunk BLOB NOT NULL);
            CREATE TABLE vectors_ann_index(id INTEGER PRIMARY KEY,vectors_id INTEGER NOT NULL,
                chunk_id INTEGER NOT NULL,chunk_position INTEGER NOT NULL);
            CREATE TABLE vectors_ann_centroids(id INTEGER PRIMARY KEY,centroid BLOB NOT NULL);
            CREATE TABLE vectors_ann_centroid_chunks(centroid_id INTEGER,chunk_id INTEGER);
            CREATE TABLE vocabulary(term TEXT);
        """)
        connection.execute("INSERT INTO setup SELECT key,value FROM upstream.setup WHERE key!='gguf'")
        connection.execute("UPDATE setup SET value='zh' WHERE key='language'")
        connection.execute("INSERT INTO sections(id,article_id,title,content,pow) "
            "SELECT id,article_id,title,content,pow FROM upstream.sections WHERE id>=?",
            (CHINESE_SECTION_START,))
        connection.execute("INSERT INTO articles SELECT * FROM upstream.articles "
                           "WHERE id IN (SELECT article_id FROM sections)")
        connection.execute("CREATE INDEX idx_sections_article_id ON sections(article_id)")
        connection.commit()
        row = connection.execute("SELECT length(value) FROM upstream.setup WHERE key='gguf'").fetchone()
        connection.execute("INSERT INTO setup(key,value) VALUES('gguf',zeroblob(?))", (row[0],))
        own_row = connection.execute("SELECT rowid FROM setup WHERE key='gguf'").fetchone()[0]
        source_row = connection.execute("SELECT rowid FROM upstream.setup WHERE key='gguf'").fetchone()[0]
        with connection.blobopen("setup", "value", source_row, readonly=True, name="upstream") as incoming:
            with connection.blobopen("setup", "value", own_row) as outgoing:
                while chunk := incoming.read(BLOCK_SIZE):
                    outgoing.write(chunk)
        connection.commit()
        connection.execute("DETACH DATABASE upstream")
        connection.executescript("""
            CREATE VIRTUAL TABLE article_search USING fts5(title,content='articles',
                content_rowid='id',tokenize='trigram');
            CREATE VIRTUAL TABLE section_search USING fts5(title,content,content='sections',
                content_rowid='id',tokenize='trigram');
            INSERT INTO article_search(article_search) VALUES('rebuild');
            INSERT INTO section_search(section_search) VALUES('rebuild');
        """)
        connection.commit()


def read_chinese_vectors(source, work, progress):
    import numpy as np

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-65536")
        size = int(connection.execute("SELECT value FROM setup WHERE key='annSize'").fetchone()[0])
        count, first_chunk = connection.execute("SELECT count(*),min(chunk_id) FROM vectors_ann_index "
            "WHERE vectors_id>=?", (CHINESE_SECTION_START,)).fetchone()
        vectors = np.memmap(work / "vectors.f32", mode="w+", dtype="<f4", shape=(count, size))
        identities = np.memmap(work / "sections.i64", mode="w+", dtype="<i8", shape=(count,))
        written = 0
        for chunk_id, blob in connection.execute(
                "SELECT id,chunk FROM vectors_ann_chunks WHERE id>=? ORDER BY id", (first_chunk,)):
            rows = connection.execute("SELECT vectors_id,chunk_position FROM vectors_ann_index "
                "WHERE chunk_id=? AND vectors_id>=? ORDER BY chunk_position",
                (chunk_id, CHINESE_SECTION_START)).fetchall()
            if not rows:
                continue
            matrix = np.frombuffer(blob, dtype="<f4").reshape(-1, size)
            positions = [row[1] for row in rows]
            vectors[written:written + len(rows)] = matrix[positions]
            identities[written:written + len(rows)] = [row[0] for row in rows]
            written += len(rows)
            if chunk_id % 1000 == 0:
                progress("repair_reading_vectors", vectors=written, total=count)
        if written != count or not np.isfinite(vectors).all():
            raise ValueError("knowledge_repair_vectors_invalid")
        vectors.flush()
        identities.flush()
        return vectors, identities


def write_clustered_vectors(destination, vectors, identities, work, progress):
    import numpy as np

    count = max(1, math.ceil(len(vectors) / 2500))
    centers = train_centroids(vectors, count, progress)
    assignment = np.memmap(work / "assignment.i32", mode="w+", dtype="<i4", shape=(len(vectors),))
    for start in range(0, len(vectors), 8192):
        batch = np.asarray(vectors[start:start + 8192])
        assignment[start:start + len(batch)] = np.argmax(batch @ centers.T, axis=1)
        if start % (8192 * 50) == 0:
            progress("repair_assigning_vectors", vectors=start, total=len(vectors))
    assignment.flush()
    order = np.argsort(assignment, kind="stable")
    sizes = np.bincount(assignment, minlength=count)
    with closing(sqlite3.connect(destination)) as connection, connection:
        connection.execute("PRAGMA cache_size=-65536")
        offset = 0
        chunk_id = 0
        for center_id, size in enumerate(sizes, 1):
            connection.execute("INSERT INTO vectors_ann_centroids VALUES(?,?)",
                               (center_id, centers[center_id - 1].astype("<f4").tobytes()))
            group = order[offset:offset + size]
            for start in range(0, len(group), 2500):
                selected = group[start:start + 2500]
                chunk_id += 1
                connection.execute("INSERT INTO vectors_ann_chunks VALUES(?,?)",
                                   (chunk_id, vectors[selected].astype("<f4").tobytes()))
                connection.executemany("INSERT INTO vectors_ann_index(vectors_id,chunk_id,chunk_position) "
                    "VALUES(?,?,?)", [(int(identities[index]), chunk_id, position)
                                      for position, index in enumerate(selected)])
                connection.execute("INSERT INTO vectors_ann_centroid_chunks VALUES(?,?)",
                                   (center_id, chunk_id))
            offset += size
            if center_id % 100 == 0:
                progress("repair_writing_index", vectors=int(offset), total=len(vectors))
        connection.execute("CREATE UNIQUE INDEX idx_vectors_ann_index_chunk_id_position "
                           "ON vectors_ann_index(chunk_id,chunk_position)")
        connection.execute("CREATE UNIQUE INDEX idx_vectors_ann_index_section ON vectors_ann_index(vectors_id)")
        connection.execute("CREATE INDEX idx_vectors_ann_centroid_chunks "
                           "ON vectors_ann_centroid_chunks(centroid_id)")
        missing = connection.execute("SELECT count(*) FROM vectors_ann_index v "
            "LEFT JOIN sections s ON s.id=v.vectors_id WHERE s.id IS NULL").fetchone()[0]
        if missing or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("knowledge_repair_evidence_binding_invalid")
    return {"vectors": len(vectors), "centroids": count, "chunks": chunk_id}


def repair(source, progress):
    """Produce a derivative for this upstream checksum, never modifying the source."""
    receipt_path = source.parent / "chinese-repair.json"
    destination = source.parent / "wikilite.zh-cohort-ivf.db"
    builder_sha = file_digest(Path(__file__))
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (receipt.get("pipeline") != PIPELINE or receipt.get("builder_sha256") != builder_sha
                or receipt.get("source_sha256") != UPSTREAM_SHA256
                or file_digest(destination) != receipt["derived_sha256"]):
            raise ValueError("knowledge_repair_receipt_mismatch")
        return destination, receipt
    if file_digest(source) != UPSTREAM_SHA256:
        raise ValueError("knowledge_repair_not_supported_for_source")
    if shutil.disk_usage(source.parent).free < 35 * 1024**3:
        raise OSError("knowledge_repair_requires_35_gib_free")
    # The builder runs in an owned process. Keep matrix work bounded on this machine.
    os.environ["OPENBLAS_NUM_THREADS"] = "2"
    os.environ["OMP_NUM_THREADS"] = "2"
    work = source.parent / (PIPELINE + ".work")
    work.mkdir(exist_ok=True)
    temporary = work / "wikilite.db"
    if temporary.exists():
        raise ValueError("knowledge_repair_incomplete_work_requires_recovery")
    progress("repair_copying_chinese_content", inferred_section_start=CHINESE_SECTION_START)
    copy_chinese_content(source, temporary)
    vectors, identities = read_chinese_vectors(source, work, progress)
    statistics = write_clustered_vectors(temporary, vectors, identities, work, progress)
    del vectors, identities
    receipt = {"format_version": 1, "pipeline": PIPELINE, "builder_sha256": builder_sha,
               "source_sha256": UPSTREAM_SHA256, "source_language": "en", "effective_language": "zh",
               "section_start": CHINESE_SECTION_START, "boundary_status": "inferred_import_cohort",
               "boundary_evidence": "package-language-profile.json", "statistics": statistics,
               "embedding_model_changed": False, "embedding_values_changed": False,
               "lexical_tokenizer": "trigram", "derived_sha256": file_digest(temporary),
               "completed_at": utc_now(), "source_snapshot_at": None}
    os.replace(temporary, destination)
    atomic_json(receipt_path, receipt)
    progress("repair_staged", statistics=statistics, derived_sha256=receipt["derived_sha256"])
    return destination, receipt
