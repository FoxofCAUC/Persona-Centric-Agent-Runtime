"""Opening a browser must not make it use a disposable runtime directory."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from persona_runtime.desktop_links import normal_user_environment,open_browser
from persona_runtime.models import RuntimeErrorBase


class BrowserEnvironmentTests(unittest.TestCase):
    def test_user_token_environment_does_not_inherit_project_temp_or_qq_paths(self):
        baseline=normal_user_environment()
        with patch.dict(os.environ,{'TEMP':'D:/throwaway-probe/tmp','TMP':'D:/throwaway-probe/tmp',
                'PCAR_NATIVE_PROBE':'private','NAPCAT_QQ_DATA_DIR':'D:/throwaway-probe/data/qq'}):
            actual=normal_user_environment()
            self.assertEqual(actual['TEMP'],baseline['TEMP']);self.assertEqual(actual['TMP'],baseline['TMP'])
            self.assertNotIn('PCAR_NATIVE_PROBE',actual);self.assertNotIn('NAPCAT_QQ_DATA_DIR',actual)
            self.assertEqual(os.environ['TEMP'],'D:/throwaway-probe/tmp')

    def test_browser_helper_receives_normal_environment_and_literal_url_argument(self):
        env=normal_user_environment();url='http://127.0.0.1:36399/webui/?token=artificial'
        with patch('persona_runtime.desktop_links.normal_user_environment',return_value=env), \
                patch('persona_runtime.desktop_links.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(open_browser(url))
        args,kwargs=run.call_args
        self.assertEqual(args[0][-1],url);self.assertNotIn(url,args[0][-2])
        self.assertEqual(kwargs['env'],env);self.assertEqual(kwargs['cwd'],Path(env['USERPROFILE']))
        self.assertNotIn('shell',kwargs)

    def test_environment_failure_does_not_fall_back_to_polluted_process_environment(self):
        with patch('persona_runtime.desktop_links.normal_user_environment',side_effect=RuntimeErrorBase('unavailable')), \
                patch('persona_runtime.desktop_links.subprocess.run') as run:
            with self.assertRaises(RuntimeErrorBase):open_browser('http://127.0.0.1:36399')
        run.assert_not_called()

    def test_failed_browser_association_is_reported(self):
        with patch('persona_runtime.desktop_links.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            self.assertFalse(open_browser('http://127.0.0.1:36399'))

    def test_external_or_non_web_url_never_starts_helper(self):
        with patch('persona_runtime.desktop_links.subprocess.run') as run:
            for url in ('file:///D:/private','https://external.invalid','javascript:alert(1)'):
                with self.subTest(url=url),self.assertRaises(RuntimeErrorBase):open_browser(url)
        run.assert_not_called()
