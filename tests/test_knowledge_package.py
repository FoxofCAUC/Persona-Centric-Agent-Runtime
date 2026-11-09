"""Installation failures must never publish incomplete knowledge assets."""

import gzip
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.knowledge_package import Asset, PackageInstaller
from persona_runtime.paths import RuntimePaths


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(data))}


class KnowledgePackageTests(unittest.TestCase):
    def setUp(self):
        root = Path("D:/Persona-Centric Agent Runtime/tmp/m9a-package-tests")
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.paths = RuntimePaths(Path(self.temporary.name))
        self.paths.initialize()
        self.installer = PackageInstaller(self.paths)
        self.data = b"abcdefgh"
        self.asset = Asset("fixture.bin", "https://example.invalid/fixture", len(self.data),
                           hashlib.sha256(self.data).hexdigest())

    def test_resume_preserves_prefix_and_checks_hash(self):
        partial = self.installer.downloads / "fixture.bin.part"
        partial.write_bytes(self.data[:3])
        response = Response(self.data[3:], 206, {
            "Content-Range": "bytes 3-7/8", "Content-Length": "5"})
        with patch("persona_runtime.knowledge_package.urllib.request.urlopen", return_value=response) as get:
            result = self.installer.download(self.asset)
        self.assertEqual(get.call_args.args[0].get_header("Range"), "bytes=3-7")
        self.assertEqual(result.read_bytes(), self.data)
        self.assertFalse(partial.exists())

    def test_ignored_range_keeps_existing_prefix(self):
        partial = self.installer.downloads / "fixture.bin.part"
        partial.write_bytes(self.data[:3])
        with patch("persona_runtime.knowledge_package.urllib.request.urlopen",
                   return_value=Response(self.data)):
            with self.assertRaisesRegex(ValueError, "resume_not_supported"):
                self.installer.download(self.asset)
        self.assertEqual(partial.read_bytes(), self.data[:3])
        self.assertFalse((self.installer.downloads / self.asset.name).exists())

    def test_wrong_total_range_is_rejected(self):
        partial = self.installer.downloads / "fixture.bin.part"
        partial.write_bytes(self.data[:3])
        response = Response(self.data[3:], 206, {
            "Content-Range": "bytes 3-7/9", "Content-Length": "5"})
        with patch("persona_runtime.knowledge_package.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "content_range_invalid"):
                self.installer.download(self.asset)
        self.assertEqual(partial.read_bytes(), self.data[:3])

    def test_bounded_ranges_publish_only_after_all_segments(self):
        responses = [Response(self.data[:4], 206, {
            "Content-Range": "bytes 0-3/8", "Content-Length": "4"}),
            Response(self.data[4:], 206, {
                "Content-Range": "bytes 4-7/8", "Content-Length": "4"})]
        with patch("persona_runtime.knowledge_package.TRANSFER_SIZE", 4), patch(
                "persona_runtime.knowledge_package.urllib.request.urlopen", side_effect=responses) as get:
            result = self.installer.download(self.asset)
        self.assertEqual([call.args[0].get_header("Range") for call in get.call_args_list],
                         ["bytes=0-3", "bytes=4-7"])
        self.assertEqual(result.read_bytes(), self.data)

    def test_checksum_failure_keeps_unpublished_partial(self):
        with patch("persona_runtime.knowledge_package.urllib.request.urlopen",
                   return_value=Response(b"12345678")):
            with self.assertRaisesRegex(ValueError, "checksum_mismatch"):
                self.installer.download(self.asset)
        self.assertFalse((self.installer.downloads / self.asset.name).exists())
        self.assertEqual((self.installer.downloads / "fixture.bin.part").read_bytes(), b"12345678")

    def test_reused_download_is_reverified(self):
        destination = self.installer.downloads / self.asset.name
        destination.write_bytes(b"12345678")
        with patch("persona_runtime.knowledge_package.urllib.request.urlopen") as get:
            with self.assertRaisesRegex(ValueError, "existing_package_asset_checksum_mismatch"):
                self.installer.download(self.asset)
        get.assert_not_called()

    def test_archive_paths_are_not_extracted(self):
        staging = self.installer.root / "test-staging"
        staging.mkdir()
        archive_path = self.installer.downloads / "test.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, data in (("../../outside.txt", b"outside"), ("bin/wikilite.exe", b"program")):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        result = self.installer.unpack_program(archive_path, staging)
        self.assertEqual(result.read_bytes(), b"program")
        self.assertFalse((self.paths.home / "outside.txt").exists())
        result.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "staged_program_checksum_mismatch"):
            self.installer.unpack_program(archive_path, staging)

    def test_truncated_gzip_never_creates_final_database(self):
        staging = self.installer.root / "test-staging"
        staging.mkdir()
        archive_path = self.installer.downloads / "test.db.gz"
        archive_path.write_bytes(gzip.compress(b"fixture" * 100)[:-5])
        with self.assertRaises((EOFError, gzip.BadGzipFile)):
            self.installer.unpack_database(archive_path, staging)
        self.assertFalse((staging / "wikilite.db").exists())

    def test_embedded_model_is_hashed_without_loading_blob_into_memory(self):
        database = self.paths.home / "model-fixture.db"
        model = b"GGUF" + struct.pack("<IQQ", 3, 1, 1) + b"public-fixture"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript("""
                CREATE TABLE setup(key TEXT PRIMARY KEY,value BLOB);
                CREATE TABLE articles(id INTEGER);
                CREATE TABLE sections(id INTEGER);
                CREATE TABLE vectors_ann_chunks(id INTEGER);
                CREATE TABLE vectors_ann_index(id INTEGER);
            """)
            connection.executemany("INSERT INTO setup VALUES(?,?)", [
                ("language", "zh"), ("model", "Qwen3-Embedding-0.6B-Q8_0"),
                ("annSize", "64"), ("modelPrefixSearch", "query:"),
                ("modelPrefixSave", ""), ("gguf", model)])
            for table in ("articles", "sections", "vectors_ann_chunks", "vectors_ann_index"):
                connection.execute(f'INSERT INTO "{table}" VALUES(1)')
        result = self.installer.inspect_database(database)
        self.assertEqual(result["embedded_model"]["sha256"], hashlib.sha256(model).hexdigest())
        self.assertEqual(result["embedded_model"]["size"], len(model))
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE setup SET value=? WHERE key='gguf'", (b"broken",))
        with self.assertRaisesRegex(ValueError, "model_header_invalid"):
            self.installer.inspect_database(database)

    def test_changed_unpacked_database_is_rejected(self):
        staging = self.installer.root / "test-staging"
        staging.mkdir()
        archive_path = self.installer.downloads / "test.db.gz"
        archive_path.write_bytes(gzip.compress(b"public-db-fixture"))
        database = self.installer.unpack_database(archive_path, staging)
        receipt = json.loads((staging / "unpacked.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["database_sha256"], hashlib.sha256(database.read_bytes()).hexdigest())
        database.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "unpacked_database_checksum_mismatch"):
            self.installer.unpack_database(archive_path, staging)

    def test_different_embedded_model_is_never_accepted_by_name_alone(self):
        with self.assertRaisesRegex(ValueError, "embedded_model_checksum_mismatch"):
            self.installer.ensure_model(self.paths.home / "program.exe", self.paths.home / "body.db",
                                        {"embedded_model": {"sha256": "0" * 64}})


if __name__ == "__main__":
    unittest.main()
