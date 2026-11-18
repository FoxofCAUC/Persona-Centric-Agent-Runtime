"""Exercise both E.N5 sessions on the real Tk/loopback path with local provider markers."""
import unittest

from persona_runtime.config import atomic_json
from persona_runtime.en5_display import read_receipts
from tests import test_en4_workbench_loopback as workbench


class ExperienceFlow(unittest.IsolatedAsyncioTestCase):
    close_window=workbench.WorkbenchLoopback.close_window
    dispose=workbench.WorkbenchLoopback.dispose
    record_frame=workbench.WorkbenchLoopback.record_frame
    until=workbench.WorkbenchLoopback.until
    submit=workbench.WorkbenchLoopback.submit
    setup_web=workbench.WorkbenchLoopback.setup_web

    def open_window(self):
        if self.binding is not None:
            workbench.WorkbenchLoopback.open_window(self)

    async def asyncSetUp(self):
        from experiments.en5_experience_setup import bind_sessions
        self.binding=None
        await workbench.WorkbenchLoopback.asyncSetUp(self)
        sessions=bind_sessions(self.paths,self.pid)
        self.binding={'version':'en5-experience-v1','study_id':'en5-ling-desktop-v1',
                      'baseline':'companion-runtime-v8/deepseek-v4-pro','mode':'author_desktop',
                      'home':str(self.paths.home.resolve()),'instance_id':self.cfg.load()['instance_id'],
                      'persona_id':self.pid,'sessions':list(sessions),'purpose':'desktop-experience'}
        atomic_json(self.paths.home/'runtime/en5-experience.json',self.binding)
        self.open_window()

    def drawn(self, sid):
        return read_receipts(self.paths,self.binding['study_id'],self.pid,sid)

    async def test_two_full_sessions_reopen_and_notice_order(self):
        from experiments.en5_experience_setup import select_part
        from experiments.en5_experience_report import export
        await self.until(lambda:self.window.session_id==self.binding['sessions'][0])
        native,frames=self.setup_web()
        self.release_route.set()
        for part,sid in enumerate(self.binding['sessions'],1):
            self.assertEqual(select_part(self.paths,self.binding,part),sid)
            if self.window.session_id!=sid:self.window.load_chat(session_id=sid)
            await self.until(lambda:self.window.session_id==sid)
            for n in range(1,16):
                self.need_web=n==4
                await self.submit(f'EN5-{part:02}-{n:02}',public=self.need_web)
                total=(part-1)*15+n
                await self.until(lambda:f'B{total}-Z' in self.frames[-1],timeout=30)
                await self.until(lambda:sum('main' in x for x in self.drawn(sid).values())==n)
            records=self.drawn(sid)
            self.assertEqual(sum('notice' in record for record in records.values()),1)
            for record in records.values():
                if 'notice' in record:
                    self.assertLessEqual(record['notice']['first_visible_at'],record['main']['first_visible_at'])
            await self.close_window()
            self.open_window()
            await self.until(lambda:self.window.session_id==sid and f'B{part*15}-Z' in self.frames[-1])
            self.assertEqual(self.drawn(sid),records)
        self.assertEqual(len(native.calls),2)
        self.assertEqual(len(self.provider.calls),30)
        self.assertEqual(len(self.provider.notices),2)
        self.assertTrue(all(page=='chat' and mapped for page,mapped,_ in frames))
        export(self.paths.home)
        report=self.paths.home/'exports/en5/report.private.json'
        self.assertTrue(report.is_file())
