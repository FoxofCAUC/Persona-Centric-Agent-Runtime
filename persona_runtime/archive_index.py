"""Disposable transactional lexical index. Never opens the authority as a cache."""
from contextlib import contextmanager
import json
import os
import sqlite3
import subprocess

from .archive import encoded, fingerprint
from .archive_query import TOKENIZER_VERSION, SearchFailure, normal, lexical, encoded_terms
from .instance import InstanceLock
from .models import RuntimeErrorBase, identifier


def partition(spec):
    return fingerprint({k:spec[k] for k in ('scope','session_id','window')})


class ArchiveIndex:
    def __init__(self, store):
        self.store = store
        self.root = store.paths.home/'cache/archive-search/v1'
        self.path = self.root/'index.sqlite3'
        self.restricted = False

    def safe_path(self):
        base = (self.store.paths.home/'cache').resolve()
        if (not base.is_relative_to(self.store.paths.home.resolve())
                or not self.root.resolve().is_relative_to(base) or self.path.is_symlink()):
            raise SearchFailure('unavailable')
        if self.path.exists() and self.store.path.exists() and self.path.samefile(self.store.path):
            raise SearchFailure('unavailable')

    @contextmanager
    def locked(self):
        self.safe_path()
        self.root.mkdir(parents=True,exist_ok=True)
        if not self.restricted:
            if os.name == 'nt':
                account=os.environ['USERDOMAIN']+'\\'+os.environ['USERNAME']
                subprocess.run(['icacls',str(self.root),'/inheritance:r','/grant:r',
                                account+':(OI)(CI)F','SYSTEM:(OI)(CI)F'],check=True,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
            else:
                self.root.chmod(0o700)
            self.restricted=True
        try:
            lock = InstanceLock(self.root/'maintenance.lock')
        except RuntimeErrorBase:
            raise SearchFailure('cache_busy') from None
        try:
            yield
        finally:
            lock.close()

    @contextmanager
    def connection(self, *, create=False, budget=None):
        self.safe_path()
        if create:
            self.root.mkdir(parents=True,exist_ok=True)
        elif not self.path.exists():
            raise SearchFailure('cache_missing')
        new = not self.path.exists()
        uri = self.path.as_uri()+('?mode=rwc' if create else '?mode=rw')
        db = sqlite3.connect(uri,uri=True,isolation_level=None,timeout=.1)
        db.row_factory=sqlite3.Row
        if budget:
            budget.watch(db)
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
                if not create or not new:
                    raise SearchFailure('cache_corrupt')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE meta(id TEXT NOT NULL,schema_version INTEGER NOT NULL,tokenizer TEXT NOT NULL,fts INTEGER NOT NULL)')
                db.execute('CREATE TABLE scope_generations(partition TEXT PRIMARY KEY,scope TEXT NOT NULL,digest TEXT NOT NULL,generation INTEGER NOT NULL)')
                db.execute('CREATE TABLE documents(rowid INTEGER PRIMARY KEY,partition TEXT NOT NULL,document_id TEXT NOT NULL,data_json TEXT NOT NULL,UNIQUE(partition,document_id))')
                db.execute('CREATE INDEX documents_partition ON documents(partition)')
                db.execute('CREATE TABLE maintenance_operations(operation_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL)')
                try:
                    db.execute('CREATE VIRTUAL TABLE fts_documents USING fts5(tokens)')
                    fts=1
                except sqlite3.OperationalError as error:
                    if 'no such module' not in str(error):
                        raise
                    fts=0
                db.execute('INSERT INTO meta VALUES(?,?,?,?)',(identifier(),1,TOKENIZER_VERSION,fts))
                db.commit()
            meta=db.execute('SELECT * FROM meta').fetchone()
            if not meta or meta['schema_version']!=1 or meta['tokenizer']!=TOKENIZER_VERSION:
                raise SearchFailure('cache_corrupt')
            yield db,dict(meta)
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    def status(self,spec):
        self.safe_path()
        if not self.path.exists():
            return {'cache_instance_id':None,'generation':0,'freshness':'unknown','cache_schema':1,'state':'missing'}
        try:
            with self.connection() as (db,meta):
                row=db.execute('SELECT * FROM scope_generations WHERE partition=? AND scope=?',
                               (partition(spec),encoded(spec['scope']))).fetchone()
                return {'cache_instance_id':meta['id'],'generation':row['generation'] if row else 0,
                        'source_digest':row['digest'] if row else None,'freshness':'unknown',
                        'cache_schema':1,'tokenizer_version':meta['tokenizer'],'fts':bool(meta['fts']),'state':'ready' if row else 'missing'}
        except (sqlite3.Error,SearchFailure) as error:
            if isinstance(error,SearchFailure) and str(error)=='unavailable':
                raise
            return {'cache_instance_id':None,'generation':0,'freshness':'unknown','cache_schema':1,'state':'corrupt'}

    def rebuild_corrupt(self):
        """Only an explicit administrator rebuild may quarantine a broken cache."""
        self.safe_path()
        try:
            with self.connection():
                return
        except (sqlite3.DatabaseError,SearchFailure):
            if self.path.exists():
                self.path.rename(self.root/('quarantined-'+identifier()+'.sqlite3'))

    def synchronize(self, corpus, spec, budget, *, force=False, operation=None):
        key=partition(spec)
        was_new = not self.path.exists()
        with self.connection(create=True,budget=budget) as (db,meta):
            db.execute('BEGIN IMMEDIATE')
            if operation:
                fp=fingerprint({'scope':spec['scope'],'partition':key,**operation})
                old=db.execute('SELECT * FROM maintenance_operations WHERE operation_id=?',(operation['operation_id'],)).fetchone()
                if old:
                    if old['fingerprint']!=fp:
                        raise SearchFailure('operation_conflict')
                    return json.loads(old['result_json'])
            row=db.execute('SELECT * FROM scope_generations WHERE partition=?',(key,)).fetchone()
            generation=row['generation'] if row else 0
            if operation and (operation['cache_instance_id'] not in (meta['id'],None)
                              or (operation['cache_instance_id'] is None and not was_new)
                              or operation['expected_generation']!=generation):
                raise SearchFailure('cache_generation_changed')
            if force or not row or row['digest']!=corpus['digest']:
                if meta['fts']:
                    db.execute('DELETE FROM fts_documents WHERE rowid IN (SELECT rowid FROM documents WHERE partition=?)',(key,))
                db.execute('DELETE FROM documents WHERE partition=?',(key,))
                for doc in corpus['documents']:
                    budget.check()
                    rid=db.execute('INSERT INTO documents(partition,document_id,data_json) VALUES(?,?,?)',
                                   (key,doc['document_id'],encoded(doc))).lastrowid
                    if meta['fts']:
                        tokens=' '.join(encoded_terms(lexical(doc['text'],document=True)[0]))
                        db.execute('INSERT INTO fts_documents(rowid,tokens) VALUES(?,?)',(rid,tokens))
                generation+=1
                db.execute('INSERT OR REPLACE INTO scope_generations VALUES(?,?,?,?)',
                           (key,encoded(spec['scope']),corpus['digest'],generation))
            result={'cache_instance_id':meta['id'],'generation':generation,'cache_schema':1,
                    'tokenizer_version':meta['tokenizer'],'source_digest':corpus['digest'],'fts':bool(meta['fts']),
                    'document_count':len(corpus['documents'])}
            if operation:
                db.execute('INSERT INTO maintenance_operations VALUES(?,?,?)',(operation['operation_id'],fp,encoded(result)))
            budget.check()
            db.commit()
            return result

    def candidates(self,spec,state,budget):
        terms,literal=lexical(spec['query'])
        use_fts=state['fts'] and terms and not literal
        with self.connection(budget=budget) as (db,meta):
            db.execute('BEGIN')
            row=db.execute('SELECT * FROM scope_generations WHERE partition=? AND scope=?',
                           (partition(spec),encoded(spec['scope']))).fetchone()
            if meta['id']!=state['cache_instance_id'] or not row or row['generation']!=state['generation'] or row['digest']!=state['source_digest']:
                raise SearchFailure('cache_generation_changed')
            count=db.execute('SELECT count(*) FROM documents WHERE partition=?',(partition(spec),)).fetchone()[0]
            fts_count=db.execute('SELECT count(*) FROM fts_documents JOIN documents d ON d.rowid=fts_documents.rowid WHERE d.partition=?',(partition(spec),)).fetchone()[0] if meta['fts'] else count
            if count!=state['document_count'] or fts_count!=count:
                raise SearchFailure('cache_corrupt')
            if use_fts:
                sql='SELECT d.data_json FROM fts_documents JOIN documents d ON d.rowid=fts_documents.rowid WHERE fts_documents MATCH ? AND d.partition=?'
                args=(' AND '.join('"'+t+'"' for t in encoded_terms(terms)),partition(spec))
            else:
                sql='SELECT data_json FROM documents WHERE partition=?'
                args=(partition(spec),)
            documents=[]
            for row in db.execute(sql,args):
                budget.check();documents.append(json.loads(row[0]))
            return documents, 'bigram_fts' if use_fts else 'literal_substring' if not state['fts'] or literal else 'time_scan'
