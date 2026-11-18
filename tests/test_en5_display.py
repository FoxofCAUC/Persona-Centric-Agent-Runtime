"""Desktop receipts use mapped native text in the two explicit study sessions."""
from contextlib import closing
from datetime import datetime
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
from tkinter import scrolledtext
import time
import unittest

from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.en5_display import DesktopDisplayCapture, read_receipts, read_scope
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.paths import RuntimePaths


class DisplayReceipts(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.paths=RuntimePaths(Path(temp.name))
        cfg=ConfigStore(self.paths).initialize()
        self.scope={'version':'en5-experience-v1','study_id':'E5','home':str(self.paths.home),
                    'persona_id':'P','sessions':['S1','S2'],'instance_id':cfg['instance_id'],
                    'baseline':'companion-runtime-v8/deepseek-v4-pro','mode':'local_fixture'}
        self.marker=self.paths.home/'runtime/en5-experience.json'

    def window(self):
        w=tk.Tk()
        w.withdraw()
        w.geometry('500x240+20000+20000')
        w.paths=self.paths
        w.current_page='chat'
        w.closed=w.tray_hidden=False
        w.persona_id='P'
        w.session_id='S1'
        w.turns=[]
        w.notice=tk.StringVar(w)
        w.transcript=scrolledtext.ScrolledText(w,wrap='word')
        w.transcript.pack(fill='both',expand=True)
        w.capture=DesktopDisplayCapture.attach(w)
        return w

    def receipts(self,session='S1'):
        return read_receipts(self.paths,'E5','P',session)

    def turn(self,w,n=1):
        value={'id':f'T{n}','persona_id':w.persona_id,'session_id':w.session_id,'channel':'desktop',
               'reply_mode_snapshot':'context_full','reply_event_id':f'E{n}'}
        w.turns.append(value)
        return value

    def wait(self,w,condition):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            w.update()
            if condition():return
            time.sleep(.01)
        self.fail('no matching viewport receipt')

    def test_scope_is_explicit_and_bound_to_instance(self):
        self.assertIsNone(read_scope(self.paths))
        w=self.window()
        self.assertIsNone(w.capture)
        w.destroy()
        self.assertFalse((self.paths.home/'private/en5').exists())
        atomic_json(self.marker,self.scope|{'instance_id':'OTHER'})
        with self.assertRaisesRegex(RuntimeErrorBase,'wrong_home'):
            read_scope(self.paths)
        atomic_json(self.marker,self.scope)
        with self.assertRaisesRegex(RuntimeErrorBase,'session_scope'):
            self.receipts('FOREIGN')

    def test_visible_body_is_measured_once_even_after_reopen(self):
        atomic_json(self.marker,self.scope)
        body='M1\n\nM2'
        w=self.window()
        try:
            self.assertTrue(w.capture.insert_main(self.turn(w),body))
            w.update()
            self.assertEqual(self.receipts(),{})
            w.deiconify()
            self.wait(w,lambda:self.receipts().get('T1',{}).get('main'))
            first=self.receipts()['T1']['main']
            self.assertEqual(first['body_sha256'],hashlib.sha256(body.encode()).hexdigest())
            self.assertIsNotNone(datetime.fromisoformat(first['first_visible_at']).tzinfo)
            self.assertEqual(first['source_id'],'E1')
        finally:
            w.destroy()
        w=self.window()
        try:
            w.capture.insert_main(self.turn(w),body)
            w.deiconify()
            self.wait(w,lambda:bool(w.capture.jobs) and all(job.done() for job in w.capture.jobs.values()))
            self.assertEqual(self.receipts()['T1']['main'],first)
            w.session_id='S3'
            self.assertFalse(w.capture.insert_main(self.turn(w,2),'M3'))
        finally:
            w.destroy()
        with closing(sqlite3.connect(self.paths.home/'private/en5/display.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visible').fetchone()[0],1)
            self.assertNotIn('M1',str(db.execute('SELECT * FROM visible').fetchall()))

    def test_scrolled_view_and_notice_require_matching_tag_and_scope(self):
        atomic_json(self.marker,self.scope)
        w=self.window()
        try:
            turn=self.turn(w)
            w.transcript.insert('end','TOP\n'*80)
            w.capture.insert_main(turn,'BOTTOM')
            w.deiconify()
            w.transcript.yview_moveto(0)
            w.update()
            self.assertEqual(self.receipts(),{})
            w.transcript.see('end')
            self.wait(w,lambda:self.receipts().get('T1',{}).get('main'))
            w.transcript.insert('end','人格：N0\n\n','pending-notice')
            marks=w.transcript.tag_ranges('pending-notice')
            w.transcript.see(marks[0])
            w.update()
            w.capture.notice(turn,'NID','N1',marks)
            self.assertNotIn('notice',self.receipts()['T1'])
            w.current_page='home'
            w.capture.notice(turn,'NID','N0',marks)
            self.assertNotIn('notice',self.receipts()['T1'])
            w.current_page='chat'
            w.capture.notice(turn,'NID','N0',marks)
            self.wait(w,lambda:self.receipts()['T1'].get('notice'))
            self.assertEqual(self.receipts()['T1']['notice']['source_id'],'NID')
            w.session_id='S2'
            w.turns=[]
            w.capture.insert_main(self.turn(w,2),'SS2')
            w.transcript.see('end')
            w.capture.schedule()
            self.wait(w,lambda:'T2' in self.receipts('S2'))
            self.assertNotIn('T2',self.receipts())
            for n in range(3,17):
                self.turn(w,n)
            self.assertFalse(w.capture.insert_main(self.turn(w,17),'EXTRA'))
        finally:
            w.destroy()
