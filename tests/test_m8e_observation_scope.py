"""A browser opened by the GUI is not an owned runtime component."""
from pathlib import Path
from contextlib import nullcontext
import os
import subprocess
import sys
import tempfile
import time
import unittest
try:
    import psutil
except ModuleNotFoundError:
    psutil=None
from unittest.mock import Mock,patch

from persona_runtime.config import ConfigStore
from persona_runtime.instance import current_process_identity
from persona_runtime.paths import RuntimePaths
from persona_runtime.models import RuntimeErrorBase
if psutil is not None:
    from plugins.m8e_acceptance.processes import owned_processes


@unittest.skipUnless(psutil,'Explicit acceptance SDK environment provides psutil')
class ObservationScopeTests(unittest.TestCase):
    def test_finished_diagnostic_does_not_abort_native_identity_snapshot(self):
        from plugins.m8e_acceptance.finish import snapshot_processes
        process=Mock();process.oneshot.return_value=nullcontext();process.is_running.return_value=False
        with patch('plugins.m8e_acceptance.finish.owned_processes',return_value=[process]), \
                patch('plugins.m8e_acceptance.finish.ProcessIdentityHandle',side_effect=RuntimeErrorBase('query raced exit')):
            self.assertEqual(snapshot_processes(None,{}),[])

    def test_live_process_query_failure_is_not_silently_ignored(self):
        from plugins.m8e_acceptance.finish import snapshot_processes
        process=Mock();process.oneshot.return_value=nullcontext();process.is_running.return_value=True
        with patch('plugins.m8e_acceptance.finish.owned_processes',return_value=[process]), \
                patch('plugins.m8e_acceptance.finish.ProcessIdentityHandle',side_effect=RuntimeErrorBase('access unavailable')):
            with self.assertRaises(RuntimeErrorBase):snapshot_processes(None,{})

    def test_same_parent_external_app_is_excluded_and_kept_alive(self):
        with tempfile.TemporaryDirectory(prefix='m8e-observe-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            paths=RuntimePaths(Path(folder));ConfigStore(paths).initialize()
            children=[]
            try:
                for _ in range(2):
                    children.append(subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)'],
                        creationflags=subprocess.CREATE_NO_WINDOW))
                component,external=children
                controller=Mock();controller.config=ConfigStore(paths)
                controller.owned.side_effect=lambda role:role=='service'
                controller.record.return_value={'host':{'ProcessId':component.pid}}
                with patch('plugins.m8e_acceptance.processes.ServiceController',return_value=controller):
                    observed={p.pid for p in owned_processes(paths,current_process_identity())}
                self.assertIn(os.getpid(),observed);self.assertIn(component.pid,observed)
                self.assertNotIn(external.pid,observed);self.assertIsNone(external.poll())
            finally:
                for child in children:
                    if child.poll() is None:child.terminate()
                    child.wait(timeout=5)

    def test_recycled_owner_identity_is_rejected_before_enumeration(self):
        identity=current_process_identity();identity['created']-=1
        with self.assertRaises(Exception):owned_processes(None,identity)

    def test_venv_launcher_ancestor_is_included_without_unrelated_siblings(self):
        with tempfile.TemporaryDirectory(prefix='m8e-branch-',dir='D:/Persona-Centric Agent Runtime/tmp') as folder:
            paths=RuntimePaths(Path(folder));ConfigStore(paths).initialize();pid_file=paths.home/'pid.txt'
            code='import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-B","-c","import time;time.sleep(30)"]);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)'
            parent=subprocess.Popen([sys.executable,'-B','-c',code,str(pid_file)],creationflags=subprocess.CREATE_NO_WINDOW)
            child=None
            try:
                deadline=time.monotonic()+5
                while not pid_file.exists() and time.monotonic()<deadline:time.sleep(.02)
                child=psutil.Process(int(pid_file.read_text()))
                controller=Mock();controller.config=ConfigStore(paths);controller.owned.side_effect=lambda role:role=='service'
                controller.record.return_value={'host':{'ProcessId':child.pid}}
                with patch('plugins.m8e_acceptance.processes.ServiceController',return_value=controller):
                    observed={p.pid for p in owned_processes(paths,current_process_identity())}
                self.assertIn(parent.pid,observed);self.assertIn(child.pid,observed)
            finally:
                if child and child.is_running():child.terminate();child.wait(timeout=5)
                if parent.poll() is None:parent.terminate()
                parent.wait(timeout=5)
