import json
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.archive import fingerprint
from persona_runtime.config import atomic_json
from scripts.m763_cohorts import register_run
from scripts.m763_r04 import model_identity


class SupplierCohort(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.root=self.home/'exports/acceptance'
        self.old=self.root/'old';self.new=self.root/'new'
        self.old.mkdir(parents=True);self.new.mkdir()
        self.fixture=b'{"frozen":"artificial labels"}'
        for path in (self.old,self.new):(path/'independent-v1.json').write_bytes(self.fixture)
        self.hash=sha256(self.fixture).hexdigest()
        self.previous={k:{'name':'old','base_url':'https://old.invalid','encrypted_key':'old'} for k in ('model','character_selector')}
        self.approved={k:{'name':'deepseek','base_url':'https://new.invalid','encrypted_key':'new'} for k in self.previous}
        self.old_prepared={'fixture_sha256':self.hash,'cases':[{'runtime_roots':{'A':str(self.old/'A')}}]}
        self.new_prepared={'fixture_sha256':self.hash,'cases':[{'runtime_roots':{'A':str(self.new/'A')}}]}
        atomic_json(self.old/'prepared.json',self.old_prepared)
        atomic_json(self.old/'execution-freeze.json',{'fixture_hash':self.hash,'models':{k:model_identity(v) for k,v in self.previous.items()}})
        register_run(self.home,self.old,self.old_prepared,self.previous)
        self.original={p:p.read_bytes() for p in self.old.rglob('*') if p.is_file()}
        self.registry=(self.root/'m763-r06-real-run.json').read_bytes()
        self.mock=patch('scripts.m763_cohorts.ConfigStore.load',return_value=self.previous);self.mock.start();self.addCleanup(self.mock.stop)

    def run_new(self,parent=True,approved=None):
        return register_run(self.home,self.new,self.new_prepared,approved or self.approved,self.old if parent else None)

    def test_explicit_switch_is_idempotent_and_preserves_old_evidence(self):
        self.run_new();self.run_new()
        proof=json.loads((self.new/'supplier-cohort.json').read_text())
        self.assertFalse(proof['old_outputs_reused']);self.assertTrue(proof['frozen_labels_unchanged'])
        self.assertEqual(self.registry,(self.root/'m763-r06-real-run.json').read_bytes())
        self.assertTrue(all(path.read_bytes()==value for path,value in self.original.items()))

    def test_no_directory_bypass_without_explicit_switch(self):
        with self.assertRaisesRegex(ValueError,'directory bypass'):self.run_new(False)

    def test_same_model_or_key_only_change_is_not_new_cohort(self):
        for value in (self.previous,{k:v|{'encrypted_key':'new-key','timeout':60} for k,v in self.previous.items()}):
            with self.assertRaisesRegex(ValueError,'changed_provider_or_model'):self.run_new(approved=value)

    def test_labels_or_parent_models_changed_is_refused(self):
        (self.new/'independent-v1.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'fixture_changed'):self.run_new()
        (self.new/'independent-v1.json').write_bytes(self.fixture)
        atomic_json(self.old/'execution-freeze.json',{'fixture_hash':self.hash,'models':{}})
        with self.assertRaisesRegex(ValueError,'parent_model_changed'):self.run_new()

    def test_second_directory_for_same_supplier_is_rejected(self):
        self.run_new();second=self.root/'bypass';second.mkdir();(second/'independent-v1.json').write_bytes(self.fixture)
        with self.assertRaisesRegex(ValueError,'registration_conflict'):
            register_run(self.home,second,self.new_prepared,self.approved,self.old)

    def test_unknown_parent_and_outside_root_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'parent_not_registered'):
            register_run(self.home,self.new,self.new_prepared,self.approved,self.new)
        with self.assertRaises(Exception):
            register_run(self.home,self.home/'outside',self.new_prepared,self.approved,self.old)
