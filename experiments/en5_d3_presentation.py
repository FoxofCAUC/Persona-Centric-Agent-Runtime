"""Lossless D3 presentation on the normal Workbench transcript, with durable per-bubble receipts."""
import asyncio
from contextlib import closing, contextmanager
import hashlib
import json
import math
import re
import sqlite3
import time

from persona_runtime.archive import fingerprint, read_exact, turn_scope
from persona_runtime.chat_display import protected_ranges
from persona_runtime.desktop import Workbench
from persona_runtime.expression_contract import ProtectedSpan
from persona_runtime.expression_protocol import bubble_plan
from persona_runtime.expression_sources import protected
from persona_runtime.qq_text import is_code_line
from persona_runtime.storage import Store
from .en5_decision_contract import require


def frames(body, split):
    spans = tuple(ProtectedSpan(**s) for s in protected(body))
    cuts = []
    if split and not any(is_code_line(s) or s.startswith(('    ', '\t')) for s in body.splitlines()):
        exclusions = [(s.start,s.end) for s in spans] + protected_ranges(body)
        for m in re.finditer(r'(?:[。！？!?…]+[”」』）】]*[ \t]*|\r?\n(?:[ \t]*\r?\n)+)', body):
            end = m.end()
            if (body[end:].strip() and body[cuts[-1] if cuts else 0:end].strip()
                    and not any(start < end < stop for start,stop in exclusions)):
                cuts.append(end)
    if len(cuts) >= 12:
        cuts = []
    pieces = [body[a:b] for a,b in zip([0,*cuts],[*cuts,len(body)])]
    return bubble_plan(body,spans,pieces)


class DesktopLedger:
    frames_for = staticmethod(frames)

    def __init__(self, paths, binding):
        self.path = paths.home/'private/en5-d3/desktop.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.binding = binding
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS plans(turn TEXT PRIMARY KEY,session TEXT NOT NULL,event TEXT NOT NULL,
                    sha TEXT NOT NULL,frames TEXT NOT NULL,stopped INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS visible(turn TEXT NOT NULL,idx INTEGER NOT NULL,at REAL NOT NULL,
                    PRIMARY KEY(turn,idx));
                CREATE TABLE IF NOT EXISTS decisions(turn TEXT PRIMARY KEY,scope TEXT NOT NULL,wire TEXT NOT NULL,
                    started REAL NOT NULL,reason TEXT,choice TEXT,raw BLOB,http INTEGER,elapsed_ms REAL,usage TEXT);
            """)
            digest = fingerprint([str(paths.home.resolve()),binding])
            db.execute('INSERT OR IGNORE INTO identity VALUES(1,?)',(digest,))
            require(db.execute('SELECT binding FROM identity WHERE id=1').fetchone()[0]==digest,'desktop_instance_changed')

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path,timeout=3)) as db:
            db.row_factory=sqlite3.Row
            with db:
                yield db

    def plan(self, turn, body):
        sid=turn['session_id']; tid=turn['id']; eid=turn['reply_event_id']
        require(turn['persona_id']==self.binding['persona_id'] and sid in self.binding['sessions']
                and turn['status']=='completed' and eid and isinstance(body,str) and body.strip(),'display_scope')
        checksum=hashlib.sha256(body.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM plans WHERE turn=?',(tid,)).fetchone()
            if not row:
                parts=self.frames_for(body,self.binding['sessions'][sid]['display']=='bubbles')
                db.execute('INSERT INTO plans(turn,session,event,sha,frames) VALUES(?,?,?,?,?)',
                           (tid,sid,eid,checksum,json.dumps(parts,ensure_ascii=False)))
                stopped=False
            else:
                require((row['session'],row['event'],row['sha'])==(sid,eid,checksum),'display_source_changed')
                parts=json.loads(row['frames']); stopped=bool(row['stopped'])
            seen=[r['at'] for r in db.execute('SELECT at FROM visible WHERE turn=? ORDER BY idx',(tid,))]
        return parts,seen,stopped

    def acknowledge(self, tid, index, now):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT frames,stopped FROM plans WHERE turn=?',(tid,)).fetchone()
            require(row is not None,'missing_display_plan')
            count=db.execute('SELECT count(*) FROM visible WHERE turn=?',(tid,)).fetchone()[0]
            if row['stopped'] or index<count:return False
            require(index==count and index<len(json.loads(row['frames'])),'display_order')
            db.execute('INSERT INTO visible VALUES(?,?,?)',(tid,index,now))
            return True

    def stop(self, session):
        with self.db() as db:
            return db.execute("""UPDATE plans SET stopped=1 WHERE session=? AND stopped=0
                AND json_array_length(frames)>(SELECT count(*) FROM visible WHERE turn=plans.turn)""",(session,)).rowcount

    def prefix(self, turn, body):
        parts, seen, _ = self.plan(turn,body)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            seen=db.execute('SELECT count(*) FROM visible WHERE turn=?',(turn['id'],)).fetchone()[0]
            if seen < len(parts):
                db.execute('UPDATE plans SET stopped=1 WHERE turn=?',(turn['id'],))
        return ''.join(p['text'] for p in parts[:seen])


class BubbleCapture:
    def __init__(self, window, ledger):
        self.window=window; self.ledger=ledger; self.widget=window.transcript
        self.store=Store(window.paths)
        self.marks={}; self.timer=None; self.next_timer=None; self.closed=False
        self.widget.configure(yscrollcommand=self.scrolled)
        self.widget.bind('<Expose>',lambda _:self.schedule(),add='+')
        self.widget.bind('<Map>',lambda _:self.schedule(),add='+')
        self.widget.bind('<Destroy>',lambda _:self.close(),add='+')

    def scrolled(self, first,last):
        self.widget.vbar.set(first,last)
        self.schedule()

    def showing(self):
        w=self.window
        return (not w.closed and not self.closed and w.current_page=='chat' and not w.tray_hidden
                and w.persona_id==self.ledger.binding['persona_id'] and self.widget.winfo_viewable()
                and w.state() not in ('withdrawn','iconic'))

    def source_live(self, tid, sid):
        with self.store.connection() as db:
            row=db.execute('SELECT * FROM turns WHERE id=? AND session_id=? AND persona_id=?',
                           (tid,sid,self.ledger.binding['persona_id'])).fetchone()
            return bool(row and self.store.current(db)==row['persona_id'] and not row['cancelled_at']
                        and read_exact(db,turn_scope(db,dict(row)),{'kind':'turn','id':tid}).get('usable'))

    def insert_main(self, turn, body):
        sid=turn['session_id']
        if sid not in self.ledger.binding['sessions']:return False
        if sid!=self.window.session_id or turn['persona_id']!=self.window.persona_id:return True
        parts,seen,stopped=self.ledger.plan(turn,body)
        n=len(seen)
        if n<len(parts) and not stopped and not self.source_live(turn['id'],sid):
            self.ledger.stop(sid)
            stopped=True
        due=seen[-1]+parts[n]['delay_ms']/1000 if n and n<len(parts) else 0
        offer=n<len(parts) and not stopped and self.showing() and time.time()>=due
        for index,part in enumerate(parts[:n+int(offer)]):
            tag='d3-'+turn['id']+'-'+str(index)
            self.widget.insert('end','人格：')
            self.widget.insert('end',part['text'],tag)
            self.widget.insert('end','\n\n')
        if n<len(parts) and not stopped:
            self.marks[turn['id']]=(sid,n,parts[n]['text'],tag if offer else None,due)
        return True

    def schedule(self):
        if not self.closed and self.timer is None:
            self.timer=self.window.after_idle(self.scan)

    def refresh(self):
        self.next_timer=None
        if self.showing():self.window.render_chat()

    def scan(self):
        self.timer=None
        if not self.showing():return
        next_delay=None
        for tid,(sid,index,text,tag,due) in list(self.marks.items()):
            if sid!=self.window.session_id:continue
            if tag is None:
                delay=max(1,math.ceil(1000*(due-time.time())))
            else:
                span=self.widget.tag_ranges(tag)
                if len(span)!=2 or self.widget.get(*span)!=text:continue
                top=self.widget.index('@0,0')
                point=top if self.widget.compare(span[0],'<',top) else span[0]
                if not self.widget.compare(point,'<',span[1]) or not self.widget.bbox(point):continue
                if self.source_live(tid,sid):
                    self.ledger.acknowledge(tid,index,time.time())
                else:
                    self.ledger.stop(sid)
                self.marks.pop(tid,None)
                delay=1
            next_delay=delay if next_delay is None else min(next_delay,delay)
        if next_delay is not None:
            if self.next_timer is not None:self.window.after_cancel(self.next_timer)
            self.next_timer=self.window.after(next_delay,self.refresh)

    def notice(self,*_):
        raise RuntimeError('d3_style_study_requires_offline_knowledge')

    def close(self):
        self.closed=True
        for timer in (self.timer,self.next_timer):
            if timer is not None:
                self.window.after_cancel(timer)
        self.timer=self.next_timer=None


class D3Workbench(Workbench):
    capture_type = BubbleCapture

    def __init__(self, paths, ledger):
        super().__init__(paths)
        if self.en5_display:self.en5_display.close()
        self.en5_display=self.capture_type(self,ledger)

    def use_persona(self,persona_id):
        if self.persona_id and self.persona_id!=persona_id and isinstance(getattr(self,'en5_display',None),BubbleCapture):
            self.en5_display.ledger.stop(self.session_id)
        return super().use_persona(persona_id)

    def load_chat(self, fresh=False, session_id=None):
        if self.session_id and (fresh or session_id and session_id!=self.session_id) and isinstance(getattr(self,'en5_display',None),BubbleCapture):
            self.en5_display.ledger.stop(self.session_id)
        return super().load_chat(fresh=fresh,session_id=session_id)

    def stop_current_reply(self):
        if isinstance(self.en5_display,BubbleCapture) and self.session_id:
            stopped=self.en5_display.ledger.stop(self.session_id)
            self.render_chat()
            if stopped and not any(t['status'] in ('queued','running') and not t.get('cancelled_at') for t in self.turns):
                self.notice.set('已停止显示后续气泡。')
                return
        return super().stop_current_reply()


async def close_local_window(window):
    """Close native Tcl timers as well as Python callbacks before releasing the local server."""
    window.closed=True
    try:
        window.en5_display.close()
        for timer in window.tk.splitlist(window.tk.call('after','info')):
            window.tk.call('after','cancel',timer)
        await asyncio.to_thread(window.hub.pool.shutdown,wait=True,cancel_futures=True)
        window.hub.drain()
    finally:
        window.destroy()
