"""Local HTTP and native widget admission; zero external calls."""
from copy import deepcopy
from unittest.mock import patch
from tests import test_m761_api as api_base
from tests import test_m761_desktop as ui_base
from persona_runtime.decision_capsule import AssistAdmission
from persona_runtime.models import identifier


class API(api_base.API):
    async def test_assist_is_process_only_and_bridge_denied(self):
        args={'persona_id':self.pid,'channel':'desktop','members':['我想找人倾诉'],'mode':'assist'}
        code,_=await self.post('disposition.preview',args);self.assertEqual(code,400)
        engine=self.app['engine'];engine.assist_admission=AssistAdmission(self.paths.home,
            self.app['config'].load()['instance_id'],frozenset((self.pid,)),self.paths.home/'absent')
        code,value=await self.post('disposition.preview',args)
        self.assertEqual(code,200);self.assertFalse(value['persisted']);self.assertFalse(value['applied_to_prompt'])
        code,_=await self.post('disposition.preview',args,'bridge');self.assertEqual(code,403)
        code,value=await self.post('interaction.current',{'persona_id':self.pid,'channel':'desktop'})
        self.assertTrue(value['assist_admitted'])
        with self.app['store'].connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)


class Desktop(ui_base.Desktop):
    def test_assist_option_only_when_instance_admitted(self):
        w=self.window();w.persona_id='A';w.show_page('character');w.update()
        self.assertEqual(tuple(w.dispo_mode_combo.cget('values')),('baseline','shadow'))
        callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append(success)):
            w.refresh_dispositions()
        value={'pid':'A','state':{'status':'not_configured','revision':0},'sources':[],
               'document':None,'history':[],'policy':{'disposition_mode':'shadow','assist_admitted':True}}
        callbacks[-1](value)
        self.assertEqual(tuple(w.dispo_mode_combo.cget('values')),('baseline','shadow','assist'))

    def test_preview_mode_change_rejects_late_response(self):
        w=self.window();w.dispo_pid='A';w.persona_id='A';w.dispo_state={'revision':1}
        w.dispo_preview_text.insert('1.0','我想找人倾诉');w.dispo_mode.set('assist')
        callbacks=[]
        with patch.object(w,'run_task',side_effect=lambda n,r,a,success=None,**kw:callbacks.append(success)),patch.object(w,'show_json') as show:
            w.preview_disposition();w.dispo_mode.set('shadow');callbacks[-1]({'capsule':'stale'})
            show.assert_not_called()
