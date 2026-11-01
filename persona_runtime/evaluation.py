"""Local evaluation evidence, deliberately separate from the authoritative runtime DB.

No model dispatch or release authorization is performed by this module.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import re
import sqlite3

from .archive import fingerprint
from .models import RuntimeErrorBase


def fail(code):
    raise RuntimeErrorBase('evaluation_' + code)


class EvaluationStore:
    def __init__(self, paths):
        self.root = paths.home / 'exports/acceptance/evaluations'

    def path(self, package_id):
        if not isinstance(package_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', package_id):
            fail('invalid_id')
        from .private_personas import safe
        safe(self.root)
        path = self.root / (package_id + '.sqlite3')
        safe(path, self.root)
        return path

    @contextmanager
    def connect(self, package_id, write=False):
        path = self.path(package_id)
        if not path.is_file():
            fail('not_found')
        db = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, package_id, cases, binding, *, seed=763, simulated=False):
        """Explicit offline preparation only; not exposed through management HTTP."""
        if len(cases) != 100 or len({c['id'] for c in cases}) != 100:
            fail('case_count')
        for c in cases:
            if set(c) != {'id', 'scenario', 'author_summary'} or any(not isinstance(v, str) or not v or len(v)>24000 for v in c.values()):
                fail('case_contract')
        path = self.path(package_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation, immutable case/material identity; never overwrite a run.
        with path.open('xb'):
            pass
        db = sqlite3.connect(path)
        try:
            db.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE cases(id TEXT PRIMARY KEY,ordinal INTEGER UNIQUE,scenario TEXT,author_summary TEXT,left_arm TEXT);
                CREATE TABLE slots(case_id TEXT,arm TEXT,state TEXT NOT NULL,request_id TEXT UNIQUE,receipt_id TEXT,attempt_id TEXT,output TEXT,error TEXT,PRIMARY KEY(case_id,arm));
                CREATE TABLE reviews(case_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL);
                CREATE TABLE operations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
                CREATE TABLE audit(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL);
            ''')
            db.executemany('INSERT INTO metadata VALUES(?,?)', [(k,json.dumps(v)) for k,v in {
                'binding':binding,'case_hash':fingerprint(cases),'simulated':simulated,'frozen':False,'seed':seed}.items()])
            for i,c in enumerate(cases):
                left = 'B' if int(sha256(f'{seed}:{c["id"]}'.encode()).hexdigest(),16)%2 else 'C'
                db.execute('INSERT INTO cases VALUES(?,?,?,?,?)',(c['id'],i,c['scenario'],c['author_summary'],left))
                for arm in 'ABC':
                    rid=fingerprint([package_id,c['id'],arm,binding])
                    db.execute('INSERT INTO slots VALUES(?,?,?, ?,NULL,NULL,NULL,NULL)',(c['id'],arm,'not_called',rid))
            db.commit()
        finally:
            db.close()

    def transition(self, package_id, case_id, arm, expected, target, *, receipt_id=None, attempt_id=None, output=None, error=None,elapsed_seconds=None):
        """Ledger only. Interrupted dispatch is unknown, never implicitly runnable."""
        allowed={'not_called':{'accepted'},'accepted':{'running','failed'},'running':{'success','failed','fee_unknown'}}
        if target not in allowed.get(expected,set()):fail('invalid_transition')
        if target=='success' and (not isinstance(output,str) or not output.strip() or len(output)>24000 or not attempt_id):fail('invalid_output')
        with self.connect(package_id,True) as db:
            if target=='accepted' and db.execute("SELECT 1 FROM metadata WHERE key='model_tests_stopped'").fetchone():fail('model_tests_stopped')
            row=db.execute('SELECT * FROM slots WHERE case_id=? AND arm=?',(case_id,arm)).fetchone()
            if not row or row['state']!=expected:fail('slot_conflict')
            db.execute('UPDATE slots SET state=?,receipt_id=COALESCE(?,receipt_id),attempt_id=COALESCE(?,attempt_id),output=?,error=? WHERE case_id=? AND arm=?',
                       (target,receipt_id,attempt_id,output,error,case_id,arm))
            db.execute('INSERT INTO audit(kind,body) VALUES(?,?)',('slot',json.dumps({'case_id':case_id,'arm':arm,'from':expected,'to':target})))
            if target=='failed' and elapsed_seconds is not None and elapsed_seconds>600:
                stop={'reason':'failure_over_600_seconds','case_id':case_id,'arm':arm,'elapsed_seconds':elapsed_seconds}
                db.execute("INSERT OR IGNORE INTO metadata VALUES('model_tests_stopped',?)",(json.dumps(stop),))

    def recover(self, package_id):
        with self.connect(package_id,True) as db:
            # Accepted requests must be looked up by their stable ID; no resubmission.
            db.execute("UPDATE slots SET state='fee_unknown',error='process_interrupted' WHERE state='running'")
            return [dict(r) for r in db.execute("SELECT case_id,arm,request_id,receipt_id,attempt_id,state FROM slots WHERE state IN ('accepted','fee_unknown')")]

    def supplement(self,package_id,case_id,arm,reason,operation_id):
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:fail('supplement_reason')
        with self.connect(package_id,True) as db:
            if db.execute("SELECT 1 FROM metadata WHERE key='model_tests_stopped'").fetchone():fail('model_tests_stopped')
            fp=fingerprint([case_id,arm,reason]);old=db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if old:
                if old['fingerprint']!=fp:fail('operation_conflict')
                return json.loads(old['result'])
            row=db.execute('SELECT * FROM slots WHERE case_id=? AND arm=?',(case_id,arm)).fetchone()
            if not row or row['state']!='failed' or row['output']:fail('supplement_not_eligible')
            # fee_unknown requires authoritative reconciliation first, not a fresh call.
            rid=fingerprint([row['request_id'],operation_id,reason])
            result={'request_id':rid,'parent_request_id':row['request_id']}
            db.execute('INSERT INTO audit(kind,body) VALUES(?,?)',('supplement',json.dumps({'previous':dict(row),'reason':reason,'next_request_id':rid},ensure_ascii=False)))
            db.execute("UPDATE slots SET state='not_called',request_id=?,receipt_id=NULL,attempt_id=NULL,output=NULL,error=NULL WHERE case_id=? AND arm=?",(rid,case_id,arm))
            db.execute('INSERT INTO operations VALUES(?,?,?)',(operation_id,fp,json.dumps(result)))
            return result

    def reconcile_runtime(self,package_id,engine,ingress):
        """Look up durable receipts/attempts. No submit, retry or provider calls."""
        with self.connect(package_id,True) as db:
            binding=json.loads(db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0])
            if binding.get('runtime_home')!=str(engine.store.paths.home.resolve()):fail('runtime_binding')
            for row in db.execute("SELECT * FROM slots WHERE state IN ('accepted','running','fee_unknown')").fetchall():
                receipt=ingress.lookup(binding['channel'],row['request_id'])
                current=engine.get(receipt['turn_id']) if receipt and receipt.get('turn_id') else engine.lookup(binding['channel'],row['request_id'])
                if current is None:continue
                if current['persona_id']!=binding.get('persona_id'):fail('runtime_scope')
                with engine.store.connection() as main:
                    attempts=main.execute('SELECT * FROM attempts WHERE turn_id=? ORDER BY rowid',(current['id'],)).fetchall()
                if len(attempts)>1:fail('unexpected_retry')
                aid=attempts[0]['id'] if attempts else None
                status='fee_unknown'
                output=None
                if current['status']=='completed':
                    output=current.get('reply')
                    if not output:fail('runtime_output_missing')
                    status='success'
                elif current['status']=='failed' and (not attempts or attempts[0]['status']=='failed'):status='failed'
                db.execute('UPDATE slots SET state=?,receipt_id=?,attempt_id=?,output=?,error=? WHERE case_id=? AND arm=?',
                    (status,receipt['receipt_id'] if receipt else None,aid,output,current.get('error_code'),row['case_id'],row['arm']))
                db.execute('INSERT INTO audit(kind,body) VALUES(?,?)',('reconcile',json.dumps({'case_id':row['case_id'],'arm':row['arm'],'turn_id':current['id'],'state':status})))
        return self.status(package_id)

    def status(self, package_id):
        with self.connect(package_id) as db:
            return {'package_id':package_id,'slots':dict(db.execute('SELECT state,count(*) FROM slots GROUP BY state').fetchall()),
                    'reviewed':db.execute('SELECT count(*) FROM reviews').fetchone()[0],
                    'frozen':json.loads(db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])}

    def schedule(self,package_id):
        """Stable randomized main-slot order; caller must resolve accepted/unknown first."""
        with self.connect(package_id) as db:
            seed=json.loads(db.execute("SELECT value FROM metadata WHERE key='seed'").fetchone()[0])
            rows=[dict(r) for r in db.execute('SELECT case_id,arm,request_id,state FROM slots')]
        return sorted(rows,key=lambda r:sha256(f'{seed}:{r["case_id"]}:{r["arm"]}'.encode()).hexdigest())

    def load(self, package_id, batch=1):
        if type(batch) is not int or not 1<=batch<=4:fail('batch')
        with self.connect(package_id) as db:
            rows=[]
            for c in db.execute('SELECT * FROM cases WHERE ordinal>=? AND ordinal<? ORDER BY ordinal',((batch-1)*25,batch*25)):
                slots={r['arm']:r for r in db.execute("SELECT * FROM slots WHERE case_id=? AND arm IN ('B','C')",(c['id'],))}
                if any(s['state']!='success' for s in slots.values()):fail('outputs_incomplete')
                review=db.execute('SELECT * FROM reviews WHERE case_id=?',(c['id'],)).fetchone()
                rows.append({'case_id':c['id'],'scenario':c['scenario'],'author_summary':c['author_summary'],
                    'left':slots[c['left_arm']]['output'],'right':slots['C' if c['left_arm']=='B' else 'B']['output'],
                    'revision':review['revision'] if review else 0,'review':json.loads(review['body']) if review else None})
            return {'package_id':package_id,'batch':batch,'cases':rows}

    def save(self, package_id, case_id, review, expected_revision, operation_id):
        return self._save(package_id,case_id,review,expected_revision,operation_id)

    def _save(self, package_id, case_id, review, expected_revision, operation_id, *,
              audit_kind='review', audit_extra=None):
        from .disposition_core.contracts import ACTIONS
        keys={'left_task','right_task','left_action','right_action','natural','reason'}
        if (type(review) is not dict or set(review)!=keys or
            any(review[k] not in ('pass','critical_error','uncertain') for k in ('left_task','right_task')) or
            any(review[k] not in (*ACTIONS,'uncertain') for k in ('left_action','right_action')) or
            review['natural'] not in ('left','right','tie') or not isinstance(review['reason'],str) or not 1<=len(review['reason'].strip())<=2000 or
            type(expected_revision) is not int or not isinstance(operation_id,str) or not 1<=len(operation_id)<=200):fail('review_contract')
        request=fingerprint([case_id,review,expected_revision])
        with self.connect(package_id,True) as db:
            old=db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if old:
                if old['fingerprint']!=request:fail('operation_conflict')
                return json.loads(old['result'])
            if json.loads(db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0]):fail('review_frozen')
            if db.execute("SELECT count(*) FROM slots WHERE case_id=? AND arm IN ('B','C') AND state='success'",(case_id,)).fetchone()[0]!=2:fail('outputs_incomplete')
            old=db.execute('SELECT revision FROM reviews WHERE case_id=?',(case_id,)).fetchone()
            if (old[0] if old else 0)!=expected_revision:fail('revision_conflict')
            revision=expected_revision+1;body=json.dumps(review,ensure_ascii=False,sort_keys=True)
            db.execute('INSERT INTO reviews VALUES(?,?,?) ON CONFLICT(case_id) DO UPDATE SET revision=excluded.revision,body=excluded.body',(case_id,revision,body))
            result={'case_id':case_id,'revision':revision}
            db.execute('INSERT INTO operations VALUES(?,?,?)',(operation_id,request,json.dumps(result)))
            audit={'case_id':case_id,'revision':revision,'review':review}
            if audit_extra:
                audit.update(audit_extra)
            db.execute('INSERT INTO audit(kind,body) VALUES(?,?)',(audit_kind,json.dumps(audit,ensure_ascii=False,sort_keys=True)))
            return result

    def export(self, package_id):
        """Atomically freeze all 100 reviews before exposing the mapping."""
        with self.connect(package_id,True) as db:
            if db.execute('SELECT count(*) FROM reviews').fetchone()[0]!=100:fail('review_incomplete')
            db.execute("UPDATE metadata SET value='true' WHERE key='frozen'")
            return {'package_id':package_id,'frozen':True,'reviews':[{'case_id':r['id'],'left_arm':r['left_arm'],'review':json.loads(r['body'])}
                for r in db.execute('SELECT c.id,c.left_arm,r.body FROM cases c JOIN reviews r ON r.case_id=c.id ORDER BY c.ordinal')]}
