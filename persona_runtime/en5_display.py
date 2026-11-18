"""First visible native text in a specifically bound E.N5 Workbench instance."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .config import ConfigStore
from .models import RuntimeErrorBase
from .private_personas import safe


def require(value, code):
    if not value:
        raise RuntimeErrorBase('en5_display_'+code)


def read_scope(paths):
    marker=paths.home/'runtime/en5-experience.json'
    if not marker.exists():
        return None
    scope=json.loads(safe(marker,paths.home).read_text(encoding='utf-8'))
    sessions=scope.get('sessions')
    require(scope.get('version')=='en5-experience-v1' and scope.get('baseline')=='companion-runtime-v8/deepseek-v4-pro'
            and isinstance(sessions,list) and len(sessions)==2
            and all(isinstance(s,str) and s for s in sessions) and len(set(sessions))==2
            and all(isinstance(scope.get(k),str) and scope[k] for k in ('study_id','persona_id','instance_id','home')),
            'invalid_binding')
    require(Path(scope['home']).resolve()==paths.home.resolve() and
            json.loads(ConfigStore(paths).file.read_text(encoding='utf-8'))['instance_id']==scope['instance_id'],
            'wrong_home')
    return scope


def _binding(scope):
    return json.dumps([scope[k] for k in ('study_id','instance_id','persona_id','sessions','home')],
                      ensure_ascii=False,separators=(',',':'))


def _file(paths):
    return safe(paths.home/'private/en5/display.sqlite3',paths.home)


def read_receipts(paths,study_id,persona_id,session_id):
    scope=read_scope(paths)
    require(scope and (scope['study_id'],scope['persona_id'])==(study_id,persona_id)
            and session_id in scope['sessions'],'session_scope')
    path=_file(paths)
    if not path.is_file():
        return {}
    result={}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        # A read racing the initial SQLite transaction may still see an empty database.
        tables=set(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        if not tables:return {}
        require(tables=={'study','visible'},'receipt_schema')
        binding=db.execute('SELECT binding FROM study WHERE singleton=1').fetchone()
        require(binding and binding[0]==_binding(scope),'receipt_binding')
        for row in db.execute('SELECT * FROM visible WHERE session_id=? ORDER BY rowid',(session_id,)):
            result.setdefault(row['turn_id'],{})[row['phase']]=dict(row)
    return result


def _store(paths,scope,session_id,turn_id,phase,source_id,body_sha256,first_visible_at):
    fresh=read_scope(paths)
    require(fresh and _binding(fresh)==_binding(scope) and session_id in scope['sessions'],'scope_changed')
    path=_file(paths)
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=3)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS study(singleton INTEGER PRIMARY KEY CHECK(singleton=1),binding TEXT NOT NULL)')
        db.execute("""CREATE TABLE IF NOT EXISTS visible(
           session_id TEXT NOT NULL,turn_id TEXT NOT NULL,phase TEXT NOT NULL CHECK(phase IN ('notice','main')),
           source_id TEXT NOT NULL,body_sha256 TEXT NOT NULL,first_visible_at TEXT NOT NULL,
           PRIMARY KEY(session_id,turn_id,phase))""")
        db.execute('INSERT OR IGNORE INTO study VALUES(1,?)',(_binding(scope),))
        require(db.execute('SELECT binding FROM study WHERE singleton=1').fetchone()[0]==_binding(scope),'receipt_binding')
        db.execute('INSERT OR IGNORE INTO visible VALUES(?,?,?,?,?,?)',
                   (session_id,turn_id,phase,source_id,body_sha256,first_visible_at))
        saved=db.execute('SELECT source_id,body_sha256 FROM visible WHERE session_id=? AND turn_id=? AND phase=?',
                         (session_id,turn_id,phase)).fetchone()
        require(saved==(source_id,body_sha256),'first_body_changed')
        db.commit()


class DesktopDisplayCapture:
    """Observe already rendered Tk text. Persist only identity, hash and the first viewport timestamp."""
    def __init__(self,window,scope):
        self.window=window
        self.scope=scope
        self.widget=window.transcript
        self.marks={}
        self.observed={}
        self.jobs={}
        self.timer=None
        self.closed=False
        self.writer=ThreadPoolExecutor(max_workers=1,thread_name_prefix='pcar-en5-display')
        self.widget.configure(yscrollcommand=self.scrolled)
        self.widget.bind('<Map>',lambda _:self.schedule(),add='+')
        self.widget.bind('<Expose>',lambda _:self.schedule(),add='+')
        self.widget.bind('<Destroy>',lambda _:self.close(),add='+')

    @classmethod
    def attach(cls,window):
        scope=read_scope(window.paths)
        return cls(window,scope) if scope else None

    def scrolled(self,first,last):
        self.widget.vbar.set(first,last)
        self.schedule()

    def schedule(self):
        if not self.closed and self.timer is None:
            self.timer=self.widget.after_idle(self.scan)

    def contains(self,turn):
        return (self.window.persona_id==turn.get('persona_id')==self.scope['persona_id']
                and self.window.session_id==turn.get('session_id') and self.window.session_id in self.scope['sessions']
                and turn.get('channel')=='desktop' and turn.get('reply_mode_snapshot')=='context_full'
                and not turn.get('dialogue_generation') and not turn.get('expression') and turn.get('id') is not None)

    def insert_main(self,turn,body):
        if not (self.contains(turn) and turn.get('reply_event_id') and body and
                turn['id'] in {t['id'] for t in self.window.turns[:15]}):
            return False
        key=(turn['session_id'],turn['id'],'main',turn['reply_event_id'])
        tag='en5-main-'+turn['id']
        self.widget.insert('end','人格：')
        self.widget.insert('end',body,tag)
        self.widget.insert('end','\n\n')
        self.marks[tag]=(key,body)
        return True

    def showing(self):
        w=self.window
        return (not self.closed and not w.closed and w.current_page=='chat' and not w.tray_hidden
                and w.persona_id==self.scope['persona_id'] and w.session_id in self.scope['sessions']
                and self.widget.winfo_viewable() and w.state() not in ('withdrawn','iconic'))

    def record(self,key,body):
        now=datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        checksum=hashlib.sha256(body.encode('utf-8')).hexdigest()
        first=self.observed.setdefault(key,(checksum,now))
        require(first[0]==checksum,'observed_text_changed')
        if key not in self.jobs:
            self.jobs[key]=self.writer.submit(_store,self.window.paths,self.scope,*key,*first)

    def scan(self):
        self.timer=None
        if not self.showing():return
        for key,job in list(self.jobs.items()):
            if job.done() and job.exception() is not None:
                self.window.notice.set('体验时标保存失败，请核对当前实例的本地记录。')
                del self.jobs[key]
        top=self.widget.index('@0,0')
        for tag,(key,body) in self.marks.items():
            if key in self.jobs or key[0]!=self.window.session_id:continue
            span=self.widget.tag_ranges(tag)
            if len(span)!=2:continue
            first,last=span
            visible=top if self.widget.compare(first,'<',top) else first
            if (self.widget.compare(visible,'<',last) and self.widget.bbox(visible)
                    and self.widget.get(first,last)==body):
                self.record(key,body)

    def notice(self,turn,source_id,body,span):
        if (self.showing() and self.contains(turn) and turn['id'] in {t['id'] for t in self.window.turns[:15]}
                and len(span)==2 and self.widget.bbox(span[0]) and
                self.widget.get(*span)=='人格：'+body+'\n\n'):
            self.record((turn['session_id'],turn['id'],'notice',source_id),body)

    def close(self):
        if self.closed:return
        self.closed=True
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer=None
        self.writer.shutdown(wait=True)
