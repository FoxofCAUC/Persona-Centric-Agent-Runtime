"""Remote document identity never exempts new binaries or private content."""
from pathlib import Path
import subprocess
import unittest

from persona_runtime.private_personas import publication_scan
from tests import test_pre_m8 as fixtures


class ExistingDocumentTests(unittest.TestCase):
    def setUp(self):
        fixtures.PrivateTests.setUp(self)
        self.repo=Path(self.temp.name)/'publication-repo';self.repo.mkdir()
        self.git('init','-q');(self.repo/'docs').mkdir()
        self.document=self.repo/'docs/guide.docx'
        self.document.write_bytes(b'PK\x03\x04\x00Public artificial document.')
        self.base=self.commit('already published fixture document')
        self.git('update-ref','refs/remotes/origin/main',self.base)

    def git(self,*args):
        return subprocess.check_output(['git',*args],cwd=self.repo,stderr=subprocess.DEVNULL).decode().strip()

    def commit(self,message):
        self.git('add','--all')
        self.git('-c','user.name=Publication Fixture','-c','user.email=fixture@example.invalid','commit','-qm',message)
        return self.git('rev-parse','HEAD')

    def test_exact_published_document_passes_only_with_explicit_base(self):
        self.assertFalse(publication_scan(self.paths,self.repo)['passed'])
        result=publication_scan(self.paths,self.repo,base=self.base)
        self.assertTrue(result['passed']);self.assertEqual(result['unchanged_published_documents'],1)

    def test_changed_or_new_document_is_still_blocked(self):
        original=self.document.read_bytes()
        for path in (self.document,self.repo/'docs/new.docx'):
            with self.subTest(path=path.name):
                path.write_bytes(b'PK\x03\x04\x00New unpublished content.')
                self.assertFalse(publication_scan(self.paths,self.repo,base=self.base)['passed'])
                if path==self.document:path.write_bytes(original)
                else:path.unlink()

    def test_unpublished_head_cannot_exempt_its_own_binary(self):
        self.document.write_bytes(b'PK\x03\x04\x00Unpublished commit.')
        unpublished=self.commit('not on the known origin')
        self.assertFalse(publication_scan(self.paths,self.repo,base=unpublished)['passed'])

    def test_staged_content_is_not_replaced_by_clean_working_file(self):
        self.document.write_bytes(b'PK\x03\x04\x00Unpublished staged content.')
        self.git('add','docs/guide.docx')
        self.document.write_bytes(b'PK\x03\x04\x00Public artificial document.')
        self.assertTrue(publication_scan(self.paths,self.repo,base=self.base)['passed'])
        self.assertFalse(publication_scan(self.paths,self.repo,base=self.base,staged=True)['passed'])

    def test_deleted_outgoing_copy_at_new_path_remains_blocked(self):
        copy=self.repo/'docs/copy.docx';copy.write_bytes(self.document.read_bytes())
        self.commit('new path is new publication');copy.unlink();self.commit('delete new path')
        self.assertFalse(publication_scan(self.paths,self.repo,base=self.base)['passed'])

    def test_existing_private_source_is_never_exempted_by_base_identity(self):
        self.document.write_bytes(self.source.read_bytes());base=self.commit('artificial private original')
        self.assertFalse(publication_scan(self.paths,self.repo,base=base)['passed'])
