"""Native default contracts and shared public fixtures; retired High tests removed."""
import asyncio,copy,json,unittest
from persona_runtime.chat import ModelCompletion,ModelError
from persona_runtime.models import RuntimeErrorBase,identifier
from tests import test_m4 as fixtures

class Main:
    def __init__(self):self.calls=[];self.notice_calls=[]
    async def complete(self,model,messages):
        if '_search_notice_profile' in model:
            self.notice_calls.append((copy.deepcopy(model),copy.deepcopy(messages)))
            return ModelCompletion('等我翻翻看，这个我想查清楚再跟你说。',{'total_tokens':1},{'finish_reason':'stop'})
        self.calls.append(copy.deepcopy(messages))
        return '我会先试社区里这套打法，操作压力小；等你熟悉了再换更激进的配装。',{'total_tokens':1}

class NativeProvider:
    def __init__(self):self.calls=[];self.tools=True
    async def complete(self,model,messages):
        self.calls.append((copy.deepcopy(model),copy.deepcopy(messages)))
        if '_native_web_profile' not in model:raise AssertionError('retired planner dispatched')
        from persona_runtime.deepseek_native_response import NativeResponse
        from tests.test_m9r0_native_contract import response
        raw=response(content='公开任务资料：先练走位，再换装备。',tools=self.tools)
        parser=NativeResponse();parser.consume(raw);return parser.complete()

class GeneralTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    async def asyncTearDown(self):
        self.chat.knowledge.close();self.org.draining=True;await self.chat.wait();await self.org.drain()
        if getattr(self,'worker',None):self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)
    def enable(self):
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com/v1','name':'deepseek-flash'}})
        pid=self.persona['id'];reader=self.chat.knowledge.current_policy(pid);processing=self.org.processing.policy(pid);web=self.org.web.policy(pid)
        self.org.processing.enable_for_persona(pid,True,reader['revision'],processing['revision'],web['revision'],'general-enable')
        self.provider=self.org.client=NativeProvider();self.main=self.chat.client=Main();self.tools=[]
        from persona_runtime.config import atomic_json
        atomic_json(self.paths.home/'private/knowledge-router/config.json',{'base_url':'https://decision.fixture.invalid/v1','name':'jev-test','encrypted_key':'artificial-protected-jev'})
        self.need_web=True;self.decisions=[];self.org.routing.transport=self.decision_transport
        self.org.processing.tools.transport=self.transport;self.worker=asyncio.create_task(self.org.worker())
    async def decision_transport(self,model,body):
        self.decisions.append((copy.deepcopy(model),copy.deepcopy(body)))
        return ModelCompletion(json.dumps({'answers':{'route':{'choice':'web' if self.need_web else 'none','confidence':0.01}}}),{'total_tokens':1},{'network_route':'local_fixture'})
    async def transport(self,stage,args,model):
        self.tools.append((stage,copy.deepcopy(args)))
        data={'results':[{'title':'玩家分享：轻松打法','url':'https://community.example/guide','summary':'社区经验可以有不同看法','content':'点赞1200，玩家试了不同装备，推荐一套操作轻松的路线。'+('普通玩家的经验分享。'*35),'date':'不知道'}]} if stage=='search' else {'title':'帖子','content':'一次玩家实测，说法并不完整。'}
        return ModelCompletion(json.dumps(data,ensure_ascii=False),{'tool':stage,'billable':False,'amount_usd':'0'},{'network_route':'local_fixture'})
    async def ask(self,text='帮我查查游戏配装，社区玩家怎么打？',public=None):
        turn=await self.chat.submit('desktop',text,identifier(),public_task=public)
        await asyncio.wait_for(self.chat.wait(),30);return self.chat.get(turn['id'])
    async def test_enabled_networking_uses_native_without_an_experiment_switch(self):
        self.enable();result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(len(self.main.notice_calls),1)
        self.assertIn('先练走位',self.main.calls[-1][0]['content']);self.assertFalse(self.tools)
        with self.store.connection() as db:
            stages=[r[0] for r in db.execute("SELECT json_extract(payload_json,'$.knowledge_processing.stage') FROM organization_tasks")]
        self.assertEqual(stages,['conversation_web_decision','conversation_search_notice','conversation_native'])
    async def test_old_disabled_experiment_selector_does_not_restore_retired_backend(self):
        self.enable()
        with self.store.connection(write=True) as db:
            key='knowledge.native.policy.'+self.persona['id']
            value=json.loads(db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()[0]);value['data']['enabled']=False
            db.execute('UPDATE knowledge_control SET value=? WHERE key=?',(json.dumps(value),key))
        result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(len(self.provider.calls),1)
    async def test_none_never_starts_tools_or_search_notice(self):
        self.enable();self.need_web=False;result=await self.ask('今天只想随便聊聊。')
        self.assertEqual(result['status'],'completed');self.assertFalse(self.provider.calls)
        self.assertFalse(self.main.notice_calls);self.assertFalse(self.tools)
    async def test_followup_reuses_saved_material_with_no_new_tools(self):
        self.enable();await self.ask();self.need_web=False
        result=await self.ask('就按刚才资料，把走位装备接着说。')
        self.assertEqual(result['status'],'completed');self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(len(self.main.notice_calls),1);self.assertTrue(result['context']['knowledge']['retrieval']['used_previous'])
    async def test_completed_tool_free_native_task_has_only_one_independent_supplement(self):
        self.enable();self.provider.tools=False;result=await self.ask()
        self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual([s for s,a in self.tools],['search']);self.assertEqual(len(self.provider.calls),1)
    async def test_global_off_remains_off_with_native_default(self):
        self.enable();pid=self.persona['id'];r=self.chat.knowledge.current_policy(pid);p=self.org.processing.policy(pid);w=self.org.web.policy(pid)
        self.org.processing.enable_for_persona(pid,False,r['revision'],p['revision'],w['revision'],identifier())
        result=await self.ask();self.assertEqual(result['status'],'completed');self.assertFalse(self.provider.calls);self.assertFalse(self.decisions)
    async def test_explicit_public_task_is_the_only_search_context(self):
        self.enable();result=await self.ask('私人部分留在聊天里',public={'question':'公开的游戏攻略'})
        self.assertEqual(result['status'],'completed');wire=json.dumps(self.provider.calls[0][1],ensure_ascii=False)
        self.assertIn('公开的游戏攻略',wire);self.assertNotIn('私人部分',wire)
    async def test_retired_stages_are_rejected_before_any_remote_attempt(self):
        self.enable();turn=await self.chat.submit('desktop','公开问题',identifier())
        for stage in ('conversation_search_plan','conversation_organize'):
            with self.assertRaisesRegex(RuntimeErrorBase,'legacy_retrieval_frozen'):
                self.org.processing._call(turn,stage,{})
        await self.chat.cancel(turn['id']);await self.chat.wait()
        self.assertFalse(self.provider.calls);self.assertFalse(self.decisions)

if __name__=='__main__':unittest.main()
