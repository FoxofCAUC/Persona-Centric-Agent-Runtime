"""B experience reports retain receipt gaps and bind human sign-off to observed sessions."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from experiments.en5_experience_report import assess, checked_paint, collect, digest, export, make_report, model_estimate, summarize
from experiments.en5_experience_setup import bind_sessions
from persona_runtime.chat import ChatEngine, ModelCompletion
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.en5_display import _store
from persona_runtime.ingress import IngressService
from persona_runtime.models import generic_persona, identifier, timestamp
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


class Local:
    async def complete(self,model,messages):
        return ModelCompletion('ID-OUTPUT',None,{'finish_reason':'stop'})


class IsolatedReport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home=(Path(tmp.name)/'home').resolve()
        self.paths=RuntimePaths(self.home)
        cfg=ConfigStore(self.paths);cfg.initialize()
        cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro'}})
        db=Store(self.paths);db.initialize()
        svc=RuntimeService(db)
        self.pid=svc.create_persona(generic_persona())['id']
        svc.use_persona(self.pid)
        self.sessions=bind_sessions(self.paths,self.pid)
        self.scope={'version':'en5-experience-v1','study_id':'ID-TEST','instance_id':cfg.load()['instance_id'],
            'persona_id':self.pid,'home':str(self.home),'sessions':self.sessions,
            'purpose':'desktop-experience','mode':'author_desktop','baseline':'companion-runtime-v8/deepseek-v4-pro'}
        atomic_json(self.home/'runtime/en5-experience.json',self.scope)
        self.chat=ChatEngine(db,cfg,Local())
        self.addCleanup(self.chat.knowledge.close)
        self.ingress=IngressService(self.chat)

    def test_empty_replay_keeps_approval_and_evidence_pending(self):
        self.scope['mode']='author_material_replay'
        atomic_json(self.home/'runtime/en5-experience.json',self.scope)
        out=export(self.home)
        result=json.loads(out.with_name('report.private.json').read_bytes())
        self.assertEqual([s['summary']['eligible_turns'] for s in result['segments']],[0,0])
        self.assertFalse(result['stage_passed'])
        self.assertIsNone(result['summary']['actual_cny'])
        self.assertFalse(result['binding']['input_approval_verified'])
        self.assertIn('回放',out.read_text(encoding='utf8'))

    async def test_desktop_markers_with_valid_paint_cannot_pass_as_cloud(self):
        batch=self.ingress.accept({'channel':'desktop','persona_id':self.pid,'session_id':self.sessions[0],
                                  'request_id':identifier(),'text':'</script><ID>','desktop_presentation':'tk-visible-v1'})
        linked=self.ingress.action(batch['batch_id'],'seal',batch['batch_revision'],identifier(),self.pid)
        await self.chat.wait()
        turn=self.chat.get(linked['turn_id'])
        self.assertEqual(turn['status'],'completed',turn['error_code'])
        _store(self.paths,self.scope,self.sessions[0],turn['id'],'main',turn['reply_event_id'],digest(turn['reply']),timestamp())
        result=collect(self.home)
        row,=result['segments'][0]['turns']
        self.assertTrue(row['baseline'])
        self.assertIsNotNone(row['main_visible_s'])
        self.assertFalse(row['main_remote_receipt'])
        self.assertFalse(result['engineering_ready'])
        self.assertEqual(result['summary']['routes']['main']['unknown_usage_entries'],1)
        out=export(self.home)
        html=out.read_text(encoding='utf8')
        self.assertNotIn('</script><ID>',html)
        self.assertIn('&lt;/script&gt;&lt;ID&gt;',html)
        self.assertEqual(collect(self.home)['evidence_sha256'],result['evidence_sha256'])

    def test_foreign_session_fails_before_export(self):
        self.scope['sessions'][1]=identifier()
        atomic_json(self.home/'runtime/en5-experience.json',self.scope)
        with self.assertRaisesRegex(ValueError,'wrong_session_owner'):collect(self.home)
        self.assertFalse((self.home/'exports/en5').exists())


def checked_segments():
    rows=[]
    for index,session in enumerate(('s1','s2'),1):
        turns=[{'issues':[],'calls':[],'real_native':i==0,'reused_without_search':i==1,'jev_need_web':i==0} for i in range(15)]
        rows.append({'number':index,'session_id':session,'turns':turns,'summary':summarize(turns),'extra_turns':0})
    return rows


class ReportDecision(unittest.TestCase):
    def test_real_web_reuse_input_and_author_are_separate_conditions(self):
        bind={'mode':'author_material_replay','input_approval_verified':True,'activation_verified':True,'study_id':'test','instance_id':'i','persona_id':'p'}
        report=make_report(bind,checked_segments())
        self.assertTrue(report['engineering_ready'])
        feedback={**{k:bind[k] for k in ('study_id','persona_id','instance_id')},
                  'version':'en5-author-review-v1','evidence_sha256':report['evidence_sha256'],
                  'segments':[{'session_id':session,'naturalness':'accept','wait':'accept'} for session in ('s1','s2')],
                  'coverage':{'topic_switch':'1/1','followup':'1/2','serious_task':'2/1'},'cost':'accept','overall':'accept'}
        self.assertTrue(assess(report,feedback))
        self.assertFalse(assess(report,feedback|{'overall':'undecided'}))
        self.assertFalse(assess(report,feedback|{'coverage':dict.fromkeys(feedback['coverage'],'')}))
        with self.assertRaisesRegex(ValueError,'feedback_version'):assess(report,feedback|{'evidence_sha256':'foreign'})
        self.assertFalse(make_report(dict(bind,input_approval_verified=False),checked_segments())['engineering_ready'])
        segments=checked_segments()
        for t in segments[1]['turns']:
            t['real_native']=False
        self.assertFalse(make_report(bind,segments)['engineering_ready'])
        self.assertFalse(make_report(dict(bind,mode='local_fixture'),checked_segments())['engineering_ready'])

    def test_unconfirmed_or_replaced_text_has_no_visible_time(self):
        import datetime
        at='2026-11-18T03:00:00.000+00:00'
        data={'main':{'first_visible_at':at,'source_id':'e1','phase':'main','body_sha256':digest('R')}}
        start=datetime.datetime.fromisoformat(at).timestamp()-1
        self.assertEqual(checked_paint(data,'main','e1','R',start),start+1)
        for args in [('main','e2','R'),('main','e1','R2'),('notice','e1','R')]:
            self.assertIsNone(checked_paint(data,*args,start))
        self.assertIsNone(checked_paint(data,'main','e1','R',start+5))
        model={'name':'deepseek-v4-pro','base_url':'https://api.deepseek.com'}
        self.assertIsNone(model_estimate('main',model,{'total_tokens':3}))


    def test_search_requires_a_completed_remote_tool_then_same_session_consumption(self):
        from contextlib import closing
        import sqlite3
        from experiments.en5_experience_report import web_calls,web_provenance
        from persona_runtime.deepseek_native_contract import VERSION
        from persona_runtime.knowledge_native_store import save_receipt
        from persona_runtime.models import LOCAL_USER_ID
        turn={'id':'T','persona_id':'P','session_id':'S'}
        model={'name':'deepseek-flash','base_url':'https://api.deepseek.com','_native_web_profile':{'mode':'deep'}}
        text='ID-RETURN'
        fake_diag={'request_headers_sent':True,'http_status':200,'network_route':'system_proxy','thinking':'enabled'}
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.executescript("""CREATE TABLE organization_tasks(id,persona_id,session_id,channel,payload_json,model_snapshot,status);
              CREATE TABLE organization_attempts(id,task_id,started_epoch,usage_json,model_snapshot,status,error_code);
              CREATE TABLE organization_receipts(attempt_id,diagnostic_json,response_text);
              CREATE TABLE knowledge_control(key PRIMARY KEY,value);
              CREATE TABLE context_uses(turn_id,persona_id,user_id,details_json);""")
            db.execute('INSERT INTO organization_tasks VALUES(?,?,?,?,?,?,?)',('K','P','S','desktop',
                  json.dumps({'knowledge_processing':{'root_turn_id':'T','stage':'conversation_native'}}),json.dumps(model),'completed'))
            db.execute('INSERT INTO organization_attempts VALUES(?,?,?,?,?,?,?)',('A','K',8.0,None,json.dumps(model),'completed',None))
            task=db.execute('SELECT * FROM organization_tasks').fetchone()
            url='https://example.invalid/1'
            packet={'version':VERSION,'outcome':'completed','assistant':{'server_tool_calls':[{'calls':[
                        {'type':'deepseek_web_search','status':'completed','sources':[{'url':url}]}]}]}}
            native=save_receipt(db,task,'A',packet)
            audit={'provider':fake_diag,'response_sha256':digest(text),**native}
            db.execute('INSERT INTO organization_receipts VALUES(?,?,?)',('A',json.dumps(audit),text))
            calls,web=web_calls(db,turn)
            self.assertEqual(web,[('K',8.0,{url})])
            self.assertIsNone(calls[0]['tokens'])
            self.assertIsNone(calls[0]['estimated_tool_cny'])
            acquired={'sources':[{'id':'D','url':url}],'retrieval':{'needed':True,'material_acquired':True}}
            prior=set()
            self.assertEqual(web_provenance(db,turn,acquired,prior,web,calls,9.0),(False,False,False))
            self.assertEqual(prior,set())
            self.assertEqual(web_provenance(db,turn,acquired,prior,web,calls,7.0),(True,True,False))
            follow={'sources':acquired['sources'],'retrieval':{'needed':False,'used_previous':True}}
            self.assertFalse(web_provenance(db,turn,follow,prior,[],[],None)[2])
            db.execute('INSERT INTO context_uses VALUES(?,?,?,?)',('T','P',LOCAL_USER_ID,json.dumps({'knowledge':{'source_ids':['D']}})))
            self.assertTrue(web_provenance(db,turn,follow,prior,[],[],None)[2])
            self.assertFalse(web_provenance(db,turn,follow,set(),[],[],None)[2])
            self.assertFalse(web_provenance(db,turn,follow,prior,[],calls,None)[2])
            audit['provider']['network_route']='local'
            db.execute('UPDATE organization_receipts SET diagnostic_json=?',(json.dumps(audit),))
            self.assertEqual(web_calls(db,turn)[1],[])
