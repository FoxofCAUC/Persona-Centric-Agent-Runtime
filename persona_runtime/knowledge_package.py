"""Pinned, resumable installation of the public M9.A encyclopedia package."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import struct
import tarfile
import time
import urllib.error
import urllib.request

from .character_embedding import safe_path
from .config import atomic_json
from .instance import InstanceLock
from .network_proxy import system_urlopen


DATA_REVISION = "71ecc16d8ab3e3cd2c33e9428ebfdc2fbedc997f"
PROGRAM_VERSION = "v1.7.24"
BLOCK_SIZE = 1024 * 1024
TRANSFER_SIZE = 64 * BLOCK_SIZE


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str


DATABASE = Asset(
    "chinese-lexical-semantic_ann~25GB.db.gz",
    "https://huggingface.co/datasets/eja/wikilite/resolve/"
    + DATA_REVISION + "/chinese-lexical-semantic_ann~25GB.db.gz?download=true",
    14317994279,
    "a385c108dd98d1aea2c2c1ac0871f4255b07da0312a73d8338869adb3de62284",
)
PROGRAM = Asset(
    "wikilite-windows-amd64-v1.7.24.tar.gz",
    "https://github.com/eja/wikilite/releases/download/v1.7.24/wikilite-windows-amd64.tar.gz",
    5937171,
    "f347836b2f6b8c2acbc9604ce23071175a210bdef1ecc58f984237b800d189db",
)
MODEL = Asset(
    "Qwen3-Embedding-0.6B-Q8_0.gguf",
    "https://huggingface.co/datasets/eja/wikilite/resolve/"
    + DATA_REVISION + "/Qwen3-Embedding-0.6B-Q8_0.gguf?download=true",
    639150592,
    "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ssl_context():
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


class PackageInstaller:
    def __init__(self, paths, progress=None):
        self.paths = paths
        self.root = safe_path(paths.home, paths.home / "data/knowledge/packages")
        self.downloads = safe_path(paths.home, paths.home / "downloads/wikilite")
        self.report = paths.home / "runtime/knowledge-install.json"
        self.progress = progress or (lambda value: None)
        self.root.mkdir(parents=True, exist_ok=True)
        self.downloads.mkdir(parents=True, exist_ok=True)

    def record(self, phase, **details):
        value = {"at": utc_now(), "phase": phase, **details}
        atomic_json(self.report, value)
        self.progress(value)

    def download(self, asset):
        destination = safe_path(self.paths.home, self.downloads / asset.name)
        partial = safe_path(self.paths.home, self.downloads / (asset.name + ".part"))
        if destination.exists():
            if destination.stat().st_size != asset.size or file_digest(destination) != asset.sha256:
                raise ValueError("existing_package_asset_checksum_mismatch")
            self.record("download_reused", asset=asset.name, bytes=asset.size)
            return destination

        last_progress = 0
        attempt = 0
        while True:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > asset.size:
                raise ValueError("package_partial_size_invalid")
            if offset == asset.size:
                break
            end = min(offset + TRANSFER_SIZE, asset.size) - 1
            headers = {"User-Agent": "PCAR-M9A/1", "Accept-Encoding": "identity"}
            if offset or end + 1 < asset.size:
                headers["Range"] = f"bytes={offset}-{end}"
            request = urllib.request.Request(asset.url, headers=headers)
            self.record("downloading", asset=asset.name, bytes=offset,
                        total=asset.size, attempt=attempt + 1)
            try:
                with system_urlopen(request, context=ssl_context(), timeout=60) as response:
                    if response.status not in (200, 206):
                        raise ValueError("package_download_status_invalid")
                    if "Range" in headers and response.status != 206:
                        raise ValueError("package_resume_not_supported")
                    if response.status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        if content_range != f"bytes {offset}-{end}/{asset.size}":
                            raise ValueError("package_content_range_invalid")
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) != end + 1 - offset:
                        raise ValueError("package_download_length_invalid")
                    with partial.open("ab" if offset else "wb") as output:
                        while chunk := response.read(BLOCK_SIZE):
                            offset += len(chunk)
                            if offset > end + 1:
                                raise ValueError("package_download_exceeds_size")
                            output.write(chunk)
                            if time.monotonic() - last_progress >= 5:
                                output.flush()
                                self.record("downloading", asset=asset.name, bytes=offset,
                                            total=asset.size, attempt=attempt + 1)
                                last_progress = time.monotonic()
                        output.flush()
                        os.fsync(output.fileno())
                if offset != end + 1:
                    raise OSError("package_download_incomplete")
                attempt = 0
                continue
            except urllib.error.HTTPError as error:
                if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                    self.record("download_failed", asset=asset.name, bytes=offset,
                                reason="http_" + str(error.code))
                    raise
            except (OSError, urllib.error.URLError) as error:
                if attempt == 3:
                    self.record("download_failed", asset=asset.name, bytes=offset,
                                reason=type(error).__name__)
                    raise
            time.sleep(2 ** attempt)
            attempt += 1

        self.record("verifying_download", asset=asset.name)
        if partial.stat().st_size != asset.size or file_digest(partial) != asset.sha256:
            self.record("checksum_failed", asset=asset.name)
            raise ValueError("package_download_checksum_mismatch")
        os.replace(partial, destination)
        self.record("download_verified", asset=asset.name, bytes=asset.size, sha256=asset.sha256)
        return destination

    def unpack_program(self, archive_path, staging):
        executable = safe_path(self.paths.home, staging / "wikilite.exe")
        with tarfile.open(archive_path, "r:gz") as archive:
            matches = [member for member in archive.getmembers()
                       if member.isfile() and Path(member.name).name.lower() == "wikilite.exe"]
            if len(matches) != 1:
                raise ValueError("wikilite_executable_missing_or_ambiguous")
            if executable.exists():
                with archive.extractfile(matches[0]) as source:
                    expected = hashlib.file_digest(source, "sha256").hexdigest()
                if file_digest(executable) != expected:
                    raise ValueError("staged_program_checksum_mismatch")
                return executable
            temporary = safe_path(self.paths.home, staging / "wikilite.exe.unpacking")
            with archive.extractfile(matches[0]) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, BLOCK_SIZE)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, executable)
        return executable

    def unpack_database(self, archive_path, staging):
        database = safe_path(self.paths.home, staging / "wikilite.db")
        receipt_path = staging / "unpacked.json"
        if database.exists():
            if not receipt_path.exists():
                raise ValueError("knowledge_existing_database_not_registered")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            model_receipt = staging / "model-import.json"
            if not model_receipt.exists() and file_digest(database) != receipt["database_sha256"]:
                raise ValueError("knowledge_unpacked_database_checksum_mismatch")
            return database
        temporary = safe_path(self.paths.home, staging / "wikilite.db.unpacking")
        expanded = 0
        last_progress = 0
        with gzip.open(archive_path, "rb") as source, temporary.open("wb") as output:
            while chunk := source.read(BLOCK_SIZE):
                output.write(chunk)
                expanded += len(chunk)
                if expanded > 40 * 1024 ** 3:
                    raise ValueError("package_expansion_exceeds_bound")
                if time.monotonic() - last_progress >= 5:
                    self.record("unpacking", bytes=expanded)
                    last_progress = time.monotonic()
            output.flush()
            os.fsync(output.fileno())
        atomic_json(receipt_path, {"at": utc_now(), "database_sha256": file_digest(temporary),
                    "archive_sha256": DATABASE.sha256, "size": expanded})
        os.replace(temporary, database)
        return database

    def inspect_database(self, database):
        self.record("database_integrity_check", bytes=database.stat().st_size)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA cache_size=-262144")
            connection.execute("PRAGMA mmap_size=268435456")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise ValueError("knowledge_database_integrity_failed")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"setup", "articles", "sections", "vectors_ann_chunks", "vectors_ann_index"}
            if not required.issubset(tables):
                raise ValueError("knowledge_package_schema_unsupported")
            counts = {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                      for name in ("articles", "sections", "vectors_ann_chunks", "vectors_ann_index")}
            setup = dict(connection.execute(
                "SELECT key, value FROM setup WHERE key IN "
                "('language','model','annSize','modelPrefixSearch','modelPrefixSave',"
                "'date','dumpDate','timestamp','version')"))
            setup = {key: value.decode("utf-8") if isinstance(value, bytes) else value
                     for key, value in setup.items()}
            setup_lengths = dict(connection.execute("SELECT key,length(value) FROM setup"))
            model = None
            row = connection.execute("SELECT rowid FROM setup WHERE key='gguf'").fetchone()
            if row:
                with connection.blobopen("setup", "value", row[0], readonly=True) as blob:
                    header = blob.read(24)
                    if len(header) != 24 or header[:4] != b"GGUF":
                        raise ValueError("knowledge_model_header_invalid")
                    version, tensors, metadata_keys = struct.unpack("<IQQ", header[4:])
                    if version != 3 or not tensors or not metadata_keys:
                        raise ValueError("knowledge_model_format_unsupported")
                    blob.seek(0)
                    digest = hashlib.sha256()
                    while chunk := blob.read(BLOCK_SIZE):
                        digest.update(chunk)
                    model = {"sha256": digest.hexdigest(),
                             "size": len(blob), "format": "GGUF", "format_version": version,
                             "tensors": tensors, "metadata_keys": metadata_keys}
            if not all(counts.values()):
                raise ValueError("knowledge_package_empty_index_or_content")
            if setup.get("model", "").removesuffix(".gguf") != "Qwen3-Embedding-0.6B-Q8_0":
                raise ValueError("knowledge_package_model_identity_mismatch")
            if not 0 < int(setup.get("annSize", 0)) <= 1024:
                raise ValueError("knowledge_package_ann_dimensions_invalid")
            if setup.get("language", "").lower() not in ("zh", "chinese", "中文"):
                self.record("package_quarantined", reason="knowledge_package_language_mismatch",
                            counts=counts, setup=setup, embedded_model=model)
                raise ValueError("knowledge_package_language_mismatch")
            if "modelPrefixSearch" not in setup or "modelPrefixSave" not in setup:
                raise ValueError("knowledge_package_query_prefix_missing")
        return {"counts": counts, "setup": setup, "setup_value_sizes": setup_lengths,
                "embedded_model": model, "size": database.stat().st_size,
                "sha256": file_digest(database)}

    def ensure_model(self, executable, database, inspection):
        receipt_path = database.parent / "model-import.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
        if inspection["embedded_model"] is not None:
            if inspection["embedded_model"]["sha256"] != MODEL.sha256:
                raise ValueError("knowledge_embedded_model_checksum_mismatch")
            if receipt and receipt["state"] != "completed":
                if inspection["embedded_model"]["sha256"] != MODEL.sha256:
                    raise ValueError("knowledge_model_import_checksum_mismatch")
                receipt.update(state="completed", completed_at=utc_now(),
                               derived_database_sha256=inspection["sha256"])
                atomic_json(receipt_path, receipt)
            if receipt and receipt["derived_database_sha256"] != inspection["sha256"]:
                raise ValueError("knowledge_derived_database_checksum_mismatch")
            return inspection, receipt

        model_path = self.download(MODEL)
        if receipt is None:
            receipt = {"state": "started", "started_at": utc_now(),
                       "source_database_sha256": inspection["sha256"],
                       "model_asset": asdict(MODEL)}
            atomic_json(receipt_path, receipt)
        elif receipt["source_database_sha256"] != inspection["sha256"]:
            raise ValueError("knowledge_model_import_source_changed")
        self.record("importing_matching_model", asset=MODEL.name)
        from .app_processes import AppProcesses
        processes = AppProcesses()
        try:
            child = processes.start("knowledge_setup", [str(executable), "-db", str(database),
                "-ai-model-import", str(model_path), "-ai-threads", "2"],
                cwd=database.parent, env=dict(os.environ))
            if child.wait(timeout=300) != 0:
                raise ValueError("knowledge_model_import_failed")
        finally:
            processes.close()
        inspection = self.inspect_database(database)
        if inspection["embedded_model"] is None or inspection["embedded_model"]["sha256"] != MODEL.sha256:
            raise ValueError("knowledge_model_import_checksum_mismatch")
        receipt.update(state="completed", completed_at=utc_now(),
                       derived_database_sha256=inspection["sha256"])
        atomic_json(receipt_path, receipt)
        return inspection, receipt

    def prepare(self):
        lock = InstanceLock(self.root / "install.lock")
        try:
            staging = safe_path(self.paths.home, self.root / ("staging-" + DATA_REVISION[:12]))
            staging.mkdir(parents=True, exist_ok=True)
            if not (staging / "wikilite.db").exists() and shutil.disk_usage(self.paths.home).free < 60 * 1024 ** 3:
                raise OSError("knowledge_install_requires_60_gib_free")
            program_archive = self.download(PROGRAM)
            executable = self.unpack_program(program_archive, staging)
            database_archive = self.download(DATABASE)
            database = self.unpack_database(database_archive, staging)
            from .knowledge_package_repair import UPSTREAM_SHA256, repair
            unpacked = json.loads((staging / "unpacked.json").read_text(encoding="utf-8"))
            repair_receipt = None
            if unpacked["database_sha256"] == UPSTREAM_SHA256:
                database, repair_receipt = repair(database, self.record)
            inspection = self.inspect_database(database)
            inspection, model_import = self.ensure_model(executable, database, inspection)
            inspection["file"] = database.name
            manifest = {"format_version": 1, "state": "staged", "created_at": utc_now(),
                        "data_revision": DATA_REVISION, "program_version": PROGRAM_VERSION,
                        "assets": [asdict(PROGRAM), asdict(DATABASE)],
                        "program_sha256": file_digest(executable), "database": inspection,
                        "model_import": model_import,
                        "repair": repair_receipt,
                        "source_snapshot_at": None,
                        "dataset_repository_updated_at": "2026-07-02T12:46:45Z",
                        "licenses": {
                            "dataset": {"declared": "GFDL", "source":
                                "https://huggingface.co/datasets/eja/wikilite/blob/" + DATA_REVISION + "/README.md"},
                            "program": {"declared": "GPL-3.0", "source":
                                "https://github.com/eja/wikilite/blob/72e03fc5557f8acbd84de2c34bb2d2eaf48f529a/LICENSE"},
                            "model": {"declared": "Apache-2.0", "source":
                                "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B"}},
                        "production_activated": False}
            atomic_json(staging / "manifest.json", manifest)
            self.record("staged", directory=str(staging), inspection=inspection)
            return manifest
        finally:
            lock.close()
