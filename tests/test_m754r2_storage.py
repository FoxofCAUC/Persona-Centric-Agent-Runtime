"""Schema/config compatibility; no formal data mutation."""
from contextlib import closing
import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_m72 import old_database
from persona_runtime import storage
from persona_runtime.paths import RuntimePaths
from persona_runtime.config import ConfigStore,atomic_json

class StorageTests(unittest.TestCase):
    def test_versions_one_to_fourteen_and_repeat(self):
        for version in range(1,15):
            with self.subTest(version=version),tempfile.TemporaryDirectory(dir=RuntimePaths.from_environment().home/'tmp',prefix='r2-migrate-') as folder:
                paths=RuntimePaths(Path(folder));paths.initialize();store=storage.Store(paths);old_database(store.path,min(11,version))
                with closing(sqlite3.connect(store.path)) as db,db:
                    if version>=12:storage.execute_schema(db,storage.SCHEMA12)
                    if version>=13:storage.execute_schema(db,storage.SCHEMA13)
                    if version>=14:
                        from persona_runtime.schema_m74 import migrate
                        migrate(db)
                    db.execute(f'PRAGMA user_version={version}')
                store.initialize();store.initialize()
                with store.connection() as db:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],15)
                    self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
                    self.assertEqual(db.execute('SELECT count(*) FROM character_selection_attempts').fetchone()[0],0)

    def test_config_v1_read_does_not_write_and_explicit_init_backs_up(self):
        with tempfile.TemporaryDirectory(dir=RuntimePaths.from_environment().home/'tmp',prefix='r2-config-') as folder:
            cfg=ConfigStore(RuntimePaths(Path(folder)));cfg.initialize();value=cfg.load();value['version']=1;value.pop('character_selector');atomic_json(cfg.file,value);before=cfg.file.read_bytes()
            self.assertEqual(cfg.load()['version'],2);self.assertEqual(cfg.file.read_bytes(),before)
            cfg.initialize();self.assertEqual(json.loads(cfg.file.read_text())['version'],2)
            backup=list(cfg.file.parent.glob('workbench-v1-*.json'));self.assertEqual(len(backup),1);self.assertEqual(backup[0].read_bytes(),before)
            self.assertFalse(cfg.load()['character_selector']['enabled'])
            cfg.initialize();self.assertEqual(len(list(cfg.file.parent.glob('workbench-v1-*.json'))),1)
