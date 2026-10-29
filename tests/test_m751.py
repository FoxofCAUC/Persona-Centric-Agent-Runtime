"""M7.5.1 targeted checks. Fake vectors test lifecycle, not semantic quality."""
import builtins
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_context import select_context
from persona_runtime.character_embedding import (CharacterEmbeddingProvider, SemanticFailure,
    checked_vectors, digest, file_hash, safe_path, MODEL_ID, ENCODING, _gate)
from persona_runtime.character_semantic_index import CharacterSemanticIndex, sources
from persona_runtime.instance import InstanceLock
from persona_runtime.models import generic_persona, identifier, RuntimeErrorBase
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def lore(item='book', **values):
    return {'id': item, 'title': '书店', 'category': '地点', 'keywords': ['书店'],
            'text': '虚构书店设有安静阅读角。', 'priority': 0, 'enabled': True,
            'visibility': 'conversation', 'dependencies': [], **values}


class FakeProvider:
    fingerprint = 'test-space-v1'

    def identity(self):
        return {'fingerprint': self.fingerprint}

    def chunks(self, text):
        return [{'start': 0, 'end': len(text), 'token_start': 0,
                 'token_end': len(text), 'text': text}]

    def embed_documents(self, texts):
        import numpy as np
        values = []
        for text in texts:
            vector = np.zeros(512, dtype=np.float32)
            vector[hashlib.sha256(text.encode()).digest()[0]] = 1
            values.append(vector)
        return np.asarray(values).reshape((-1, 512))

    def embed_query(self, members):
        return ([{'member_index': i, 'start': 0, 'end': len(t)} for i,t in enumerate(members)],
                self.embed_documents(members))


class SemanticTests(unittest.TestCase):
    def setUp(self):
        root = RuntimePaths.from_environment().home / 'tmp'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='m751-', dir=root)
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths(Path(self.temp.name))
        self.store = Store(self.paths); self.store.initialize()
        self.service = RuntimeService(self.store)
        self.pid = self.service.create_persona(generic_persona())['id']
        self.service.use_persona(self.pid)
        self.characters = CharacterService(self.store)
        self.provider = FakeProvider()
        self.index = CharacterSemanticIndex(self.store, self.provider)
        self.data = empty_character()
        self.data['lorebook'] = [lore(), lore('private', visibility='creator_only'), lore('off', enabled=False)]
        self.data['examples'] = [{'id':'hello','scenario':'问候','keywords':['你好'],'text':'你好呀','enabled':True}]
        self.version = self.save(self.data)

    def save(self, data):
        return self.characters.update(self.pid, data, self.characters.current(self.pid)['id'], identifier())

    def path(self, version=None):
        return self.index._path(self.index._spec(version or self.version))

    def test_filter_before_encoding(self):
        src = sources(self.version)
        self.assertEqual([(s['kind'],s['item_id']) for s in src], [('lore','book'),('example','hello')])
        with patch.object(self.provider, 'embed_documents', wraps=self.provider.embed_documents) as embed:
            self.index.build(self.pid, self.version['id'])
        self.assertEqual(len(embed.call_args.args[0]),2)

    def test_idempotency_and_no_authority_write(self):
        before = file_hash(self.store.path)
        self.assertEqual(self.index.build(self.pid,self.version['id'])['status'],'built')
        with patch.object(self.provider,'embed_documents',side_effect=AssertionError('should reuse')):
            self.assertEqual(self.index.build(self.pid,self.version['id'])['status'],'hit')
        self.assertEqual(file_hash(self.store.path),before)

    def test_edit_restore_and_old_version(self):
        self.index.build(self.pid,self.version['id'])
        changed = deepcopy(self.data); changed['lorebook'][0]['text']='新版虚构茶室'
        new = self.save(changed); self.index.build(self.pid,new['id'])
        restored = self.characters.restore(self.pid,self.version['id'],new['id'],identifier())
        self.index.build(self.pid,restored['id'])
        self.assertEqual(len({self.path(),self.path(new),self.path(restored)}),3)
        self.assertEqual(self.index.query(self.pid,self.version['id'],['书店'])['version_id'],self.version['id'])

    def test_import_new_identity_no_cache_sharing(self):
        exported = self.characters.export(self.pid)
        imported = self.characters.import_package(self.pid, json.loads(Path(exported['path']).read_text(encoding='utf-8')), identifier())
        other = imported['id']
        current = self.characters.current(other)
        self.index.build(other,current['id'])
        self.assertNotEqual(self.path(),self.path(current))
        with self.assertRaises(RuntimeErrorBase):
            self.index.query(other,self.version['id'],['书店'])

    def test_unknown_and_latest_not_substituted(self):
        with self.assertRaises(RuntimeErrorBase):self.index.build(self.pid,'unknown')
        with self.assertRaisesRegex(SemanticFailure,'version_required'):self.index.build(self.pid,None)

    def test_model_and_template_invalidate(self):
        self.index.build(self.pid,self.version['id'])
        self.provider.fingerprint='test-space-v2'
        with self.assertRaisesRegex(SemanticFailure,'cache_missing'):self.index.query(self.pid,self.version['id'],['书店'])
        self.index.build(self.pid,self.version['id'])
        with patch('persona_runtime.character_semantic_index.TEMPLATE_VERSION','v2'):
            with self.assertRaisesRegex(SemanticFailure,'cache_missing'):self.index.query(self.pid,self.version['id'],['书店'])

    def test_corruption_explicit_rebuild(self):
        self.index.build(self.pid,self.version['id']); self.path().write_bytes(b'corrupt')
        with self.assertRaisesRegex(SemanticFailure,'cache_invalid'):self.index.query(self.pid,self.version['id'],['书店'])
        with self.assertRaisesRegex(SemanticFailure,'cache_invalid'):self.index.build(self.pid,self.version['id'])
        self.index.build(self.pid,self.version['id'],rebuild=True)
        self.assertEqual(len(self.index.query(self.pid,self.version['id'],['书店'])['candidates']),2)

    def test_vector_corruption_detected(self):
        self.index.build(self.pid,self.version['id'])
        with closing(sqlite3.connect(self.path())) as conn, conn:conn.execute('UPDATE chunks SET vector=? WHERE ordinal=0',(b'wrong',))
        with self.assertRaisesRegex(SemanticFailure,'cache_invalid'):self.index.query(self.pid,self.version['id'],['书店'])

    def test_concurrent_build_busy(self):
        self.index.build(self.pid,self.version['id'])
        lock = InstanceLock(self.path().with_suffix('.lock'))
        try:
            with self.assertRaisesRegex(SemanticFailure,'cache_busy'):self.index.build(self.pid,self.version['id'])
        finally:lock.close()

    def test_failed_publish_preserves_old_generation(self):
        self.index.build(self.pid,self.version['id']); old=file_hash(self.path())
        def fail(path,*args):
            path.write_bytes(b'half'); raise OSError('injected disk failure')
        with patch.object(self.index,'_publish',side_effect=fail):
            with self.assertRaises(OSError):self.index.build(self.pid,self.version['id'],rebuild=True)
        self.assertEqual(file_hash(self.path()),old)
        self.assertFalse(list(self.index.root.glob('*.tmp')))

    def test_source_race_rejects_publish(self):
        with patch.object(self.index,'_snapshot',side_effect=[self.version,self.version | {'data':empty_character()}]):
            with self.assertRaisesRegex(SemanticFailure,'source_changed'):self.index.build(self.pid,self.version['id'])
        self.assertFalse(self.path().exists())

    def test_invalidate_does_not_delete_old_versions(self):
        self.index.build(self.pid,self.version['id'])
        changed=deepcopy(self.data);changed['interests']['notes']='新版'
        new=self.save(changed);self.index.build(self.pid,new['id'])
        self.index.invalidate(self.pid,new['id'])
        self.assertTrue(self.path().exists());self.assertFalse(self.path(new).exists())

    def test_multiple_members_and_no_authorization(self):
        self.index.build(self.pid,self.version['id'])
        result=self.index.query(self.pid,self.version['id'],['第一条','第二条'])
        self.assertEqual([c['member_index'] for c in result['query_chunks']],[0,1])
        self.assertFalse(result['activation_authorized'])
        self.assertNotIn('text',json.dumps(result))

    def test_stable_tie_order(self):
        self.index.build(self.pid,self.version['id'])
        with patch.object(self.provider,'embed_query',return_value=([{'member_index':0,'start':0,'end':1}],self.provider.embed_documents(['相同']))):
            first=self.index.query(self.pid,self.version['id'],['x'])
            self.assertEqual(first,self.index.query(self.pid,self.version['id'],['x']))

    def test_authority_hardlink_rejected(self):
        self.index.root.mkdir(parents=True)
        os.link(self.store.path,self.path())
        with self.assertRaisesRegex(SemanticFailure,'unsafe'):self.index.build(self.pid,self.version['id'])

    def test_path_escape(self):
        with self.assertRaisesRegex(SemanticFailure,'unsafe'):safe_path(self.paths.home,self.paths.home/'../other')

    def test_model_missing_and_busy(self):
        provider=CharacterEmbeddingProvider(self.paths,self.paths.home/'models/character-embedding/missing')
        self.assertEqual(provider.diagnose()['reason'],'embedding_unavailable')
        _gate.acquire()
        try:self.assertEqual(provider.diagnose()['reason'],'embedding_busy')
        finally:_gate.release()

    def model_fixture(self):
        root=self.paths.home/'models/character-embedding'
        root.mkdir(parents=True)
        files={name:b'artificial model fixture' for name in
               ('model_optimized.onnx','tokenizer.json','config.json','special_tokens_map.json','tokenizer_config.json')}
        manifest={'model_id':MODEL_ID,'dimension':512,'encoding':ENCODING,'revision':'a'*40,
                  'packages':{'fastembed':'0.9.1'},
                  'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
        fingerprint=digest(manifest);manifest['fingerprint']=fingerprint
        folder=root/fingerprint;folder.mkdir()
        for name,data in files.items():(folder/name).write_bytes(data)
        (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return CharacterEmbeddingProvider(self.paths,folder)

    def test_model_hash_tamper(self):
        provider=self.model_fixture();provider.identity()
        (provider.directory/'model_optimized.onnx').write_bytes(b'tampered model')
        self.assertEqual(provider.diagnose()['reason'],'embedding_model_invalid')

    def test_manifest_tamper(self):
        provider=self.model_fixture()
        p=provider.directory/'manifest.json';data=json.loads(p.read_text());data['dimension']=384
        p.write_text(json.dumps(data))
        self.assertEqual(provider.diagnose()['reason'],'embedding_model_invalid')

    def test_dependency_missing_is_diagnostic(self):
        provider=self.model_fixture();original=builtins.__import__
        def guarded(name,*args,**kwargs):
            if name=='fastembed':raise ImportError('test missing dependency')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=guarded):
            self.assertEqual(provider.diagnose()['reason'],'embedding_dependency_missing')

    def test_dependency_version_mismatch(self):
        provider=self.model_fixture()
        with patch('importlib.metadata.version',return_value='unapproved'):
            self.assertEqual(provider.diagnose()['reason'],'embedding_dependency_mismatch')

    def test_invalid_generated_vectors_do_not_replace_cache(self):
        import numpy as np
        self.index.build(self.pid,self.version['id']);old=file_hash(self.path())
        with patch.object(self.provider,'embed_documents',return_value=np.zeros((2,512))):
            with self.assertRaisesRegex(SemanticFailure,'invalid_vector'):
                self.index.build(self.pid,self.version['id'],rebuild=True)
        self.assertEqual(file_hash(self.path()),old)

    def test_empty_character_index(self):
        empty=self.save(empty_character())
        self.assertEqual(self.index.build(self.pid,empty['id'])['chunks'],0)
        self.assertEqual(self.index.query(self.pid,empty['id'],['你好'])['candidates'],[])

    def test_partial_cache_write_preserves_old(self):
        self.index.build(self.pid,self.version['id']);old=file_hash(self.path())
        real=self.index._publish
        def interrupted(path,*args):
            real(path,*args)
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute('DELETE FROM chunks WHERE ordinal=0')
        with patch.object(self.index,'_publish',side_effect=interrupted):
            with self.assertRaisesRegex(SemanticFailure,'cache_invalid'):
                self.index.build(self.pid,self.version['id'],rebuild=True)
        self.assertEqual(file_hash(self.path()),old)

    def test_visibility_change_cannot_reuse_old_cache(self):
        self.index.build(self.pid,self.version['id'])
        changed=deepcopy(self.data);changed['lorebook'][0]['visibility']='creator_only'
        new=self.save(changed)
        with self.assertRaisesRegex(SemanticFailure,'cache_missing'):self.index.query(self.pid,new['id'],['书店'])
        self.index.build(self.pid,new['id'])
        self.assertNotIn('book',[r['item_id'] for r in self.index.query(self.pid,new['id'],['书店'])['candidates']])
        self.assertIn('book',[r['item_id'] for r in self.index.query(self.pid,self.version['id'],['书店'])['candidates']])

    def test_invalid_vectors(self):
        import numpy as np
        for v in (np.zeros((1,512)),np.ones((1,511)),np.full((1,512),float('nan')),np.full((1,512),float('inf'))):
            with self.assertRaisesRegex(SemanticFailure,'invalid_vector'):checked_vectors(v,1)
        self.assertAlmostEqual(float(np.linalg.norm(checked_vectors(np.ones((1,512)),1))),1,places=5)

    def test_optional_import_without_site_packages(self):
        result=subprocess.run([sys.executable,'-B','-S','-c',
            'from persona_runtime.character_context import select_context; from persona_runtime.character_embedding import CharacterEmbeddingProvider; from persona_runtime.character_semantic_index import CharacterSemanticIndex; print("ok")'],
            capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),'ok')

    def test_lexical_facts_unchanged(self):
        selected=select_context(self.version,'你血型是什么，书店呢？')
        self.assertIn('appearance.blood_type',selected['fact_paths'])
        self.assertIn('book',selected['lore_ids'])
        self.assertNotIn('private',selected['lore_ids'])
