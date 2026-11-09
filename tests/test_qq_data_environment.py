"""A verified deployment opts into its own QQ data without shared fallback."""
import hashlib
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch

from persona_runtime.config import atomic_json
from persona_runtime.host import qq_data_environment
from persona_runtime.paths import RuntimePaths
from persona_runtime.models import RuntimeErrorBase


class QQDataEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='m8e-QQ 中文 路径-',dir='D:/Persona-Centric Agent Runtime/tmp')
        self.addCleanup(self.temp.cleanup);self.paths=RuntimePaths(Path(self.temp.name));self.paths.initialize()
        self.file=self.paths.home/'components/napcat/napcat.mjs';self.file.parent.mkdir(parents=True);self.file.write_text('artificial module')
        self.manifest={'qq_data_contract':'runtime-home-v1','napcat_release':'4.18.42',
            'napcat_main_sha256':hashlib.sha256(self.file.read_bytes()).hexdigest()}

    def test_verified_component_uses_own_path_and_does_not_mutate_parent_environment(self):
        atomic_json(self.paths.home/'runtime/deployment.json',self.manifest);original={'TEMP':'owned','NAPCAT_QQ_DATA_DIR':'foreign'}
        result=qq_data_environment(self.paths,original)
        self.assertEqual(result['NAPCAT_QQ_DATA_DIR'],str(self.paths.home/'data/qq'))
        self.assertEqual(result['NAPCAT_WORKDIR'],str(self.file.parent));self.assertEqual(original['NAPCAT_QQ_DATA_DIR'],'foreign')
        self.assertTrue((self.paths.home/'data/qq').is_dir())

    def test_old_deployment_keeps_its_existing_behavior(self):
        atomic_json(self.paths.home/'runtime/deployment.json',{'version':1})
        env={'existing':'value'};self.assertIs(qq_data_environment(self.paths,env),env)

    def test_changed_binary_or_release_is_rejected_without_shared_fallback(self):
        for change in ({'napcat_main_sha256':'wrong'},{'napcat_release':'unverified'},{'qq_data_contract':'unknown'}):
            atomic_json(self.paths.home/'runtime/deployment.json',self.manifest|change)
            with self.subTest(change=change),self.assertRaises(RuntimeErrorBase):qq_data_environment(self.paths,{})

    def test_repeated_component_prepare_preserves_independent_data_contract(self):
        from persona_runtime.deployment import prepare
        for name in ('native','node_modules','static','worker'):(self.file.parent/name).mkdir()
        python=self.paths.home/'components/astrbot/.venv/Scripts/python.exe';python.parent.mkdir(parents=True);python.touch()
        atomic_json(self.paths.home/'runtime/deployment.json',self.manifest)
        with patch.dict(os.environ,{'PCAR_NAPCAT_SOURCE':'','PCAR_ASTRBOT_PYTHON':str(python)}):prepare(self.paths,install=False)
        result=qq_data_environment(self.paths,{})
        self.assertEqual(result['NAPCAT_QQ_DATA_DIR'],str(self.paths.home/'data/qq'))
