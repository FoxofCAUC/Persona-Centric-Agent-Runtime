import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from persona_runtime.archive import fingerprint
from persona_runtime.paths import RuntimePaths
from scripts.audit_m763_context_replay import ReadOnlyStore, NoDispatch, verify_message_hash, reject_index


class ContextAudit(unittest.TestCase):
    def test_authority_connection_rejects_write_and_initialize(self):
        with tempfile.TemporaryDirectory() as root:
            store = ReadOnlyStore(RuntimePaths(Path(root)))
            store.path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(store.path)) as db:
                db.execute('CREATE TABLE sample(value TEXT)')
                db.execute("INSERT INTO sample VALUES('unchanged')")
                db.commit()
            before = store.path.read_bytes()
            db = store._connect()
            try:
                self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0], 'unchanged')
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("UPDATE sample SET value='changed'")
            finally:
                db.close()
            with self.assertRaisesRegex(AssertionError, 'cannot_write_authority'):
                with store.connection(write=True):
                    pass
            with self.assertRaisesRegex(AssertionError, 'cannot_initialize'):
                store._connect(create=True)
            self.assertEqual(before, store.path.read_bytes())

    def test_dispatch_and_local_index_work_cannot_be_started(self):
        with self.assertRaisesRegex(AssertionError, 'cannot_call_model'):
            asyncio.run(NoDispatch().complete({}, []))
        with self.assertRaisesRegex(AssertionError, 'reuse_frozen_material'):
            reject_index({})

    def test_both_manifest_and_actual_wire_hash_must_match(self):
        messages = [{'role': 'user', 'content': '人工  \n完整成员'}]
        expected = fingerprint(messages)
        self.assertEqual(verify_message_hash(messages, {'input_snapshot_hash': expected}, {'request_hash': expected}), expected)
        with self.assertRaisesRegex(ValueError, 'not_reproducible'):
            verify_message_hash(messages, {'input_snapshot_hash': expected}, {'request_hash': 'wrong'})
        with self.assertRaisesRegex(ValueError, 'not_reproducible'):
            verify_message_hash(messages, {'input_snapshot_hash': 'wrong'}, {'request_hash': expected})


if __name__ == '__main__':
    unittest.main()
