import asyncio,copy,hashlib,json,unittest
from unittest.mock import patch
from persona_runtime.chat import ModelCompletion,ModelError
from persona_runtime.knowledge_wikimedia import ENDPOINT,article_title,plain,public_query,parse
from persona_runtime.knowledge_web import snapshot,validate_snapshot
from persona_runtime.models import RuntimeErrorBase
from persona_runtime.portable_memory import import_export
from tests import test_m4 as fixtures

def envelope(stage='wiki_fetch'):
    if stage=='wiki_search':
        values=[{'query':{'search':[{'ns':0,'title':'水循环','snippet':'水循环','timestamp':'2026-11-11T00:00:00Z'}]}}]
    else:
        values=[{'parse':{'title':'水循环','pageid':5,'revid':77,'text':'<p>水循环包含蒸发、凝结和降水。</p><h2>过程</h2><p>这是水的运动。</p>'}},
                {'query':{'pages':[{'ns':0,'pageid':5,'revisions':[{'revid':77,'timestamp':'2026-11-11T00:00:00Z'}]}],
                          'rightsinfo':{'text':'Creative Commons Attribution-Share Alike 4.0','url':'https://creativecommons.org/licenses/by-sa/4.0/'}}}]
    rows=[]
    for value in values:
        body=json.dumps(value,ensure_ascii=False);rows.append({'endpoint':ENDPOINT,'http_status':200,'body':body,'sha256':hashlib.sha256(body.encode()).hexdigest()})
    return json.dumps({'provider':'wikimedia_api','stage':stage,'responses':rows},ensure_ascii=False)

class Models:
    async def complete(self,model,messages):
        value=json.loads(messages[-1]['content'])
        if 'recent_context' in value:out={'queries':['水循环'],'freshness':'snapshot'}
        elif 'topics' in value:out={'topic_ids':['water']}
        elif 'sources' in value and 'fetch_ids' in messages[0]['content']:out={'fetch_ids':[value['sources'][0]['id']]}
        elif 'sources' in value:out={'section_ids':[value['sources'][0]['id']]}
        elif 'units' in value:out={'decisions':[{'index':i,'supported':True,'temporal_ok':True,'version_ok':True,'note':'原文支持。'} for i in range(len(value['units']))]}
        else:out={'units':[{'text':'水循环包含蒸发、凝结和降水。','kind':'source_claim','refs':[{'material_id':value['materials'][0]['id'],'quote':value['materials'][0]['text']}]}]}
        return json.dumps({'format_version':1,**out},ensure_ascii=False),{'total_tokens':3}

class Main:
    def __init__(self):self.calls=[]
    async def complete(self,model,messages):self.calls.append(messages);return '水循环包含蒸发、凝结和降水。',{'total_tokens':3}

class WikiTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.M4Tests.setUp
    async def asyncTearDown(self):
        from tests.test_knowledge_web import WebTests
        await WebTests.asyncTearDown(self)
    def enable(self,filtered=False):
        self.cfg.update({'model':{'base_url':'https://api.deepseek.com','name':'deepseek-flash','api_key':'artificial-source-test'}})
        pid=self.persona['id'];self.chat.knowledge.update_policy(pid,{'enabled':True,'channels':['desktop']},0,'read')
        policy=self.org.web.policy(pid)
        self.org.web.update_policy(pid,policy['data']|{'enabled':True,'domains':['zh.wikipedia.org'],'topics':{'water':'水循环 site:zh.wikipedia.org'}},0,'web')
        self.main=self.chat.client=Main();self.org.client=Models();self.cloud=[];self.source=[]
        async def cloud(stage,args,model):
            self.cloud.append(stage)
            if stage=='search':
                data={'results':[{'title':'水循环','url':'https://zh.wikipedia.org/wiki/'+('Portal:气象' if filtered else '水循环'),'summary':'','snippet':'','content':'','date':None}]}
                return ModelCompletion(json.dumps(data),{'tool':stage,'billable':True,'amount_usd':'0.00075'}, {})
            error=ModelError('web_http_404',usage={'tool':stage,'billable':False,'amount_usd':'0'},diagnostics={'request_headers_sent':True,'http_status':404});error.tool_response='{"error":"notfound"}';raise error
        async def source(stage,args,timeout):
            self.source.append((stage,args,timeout))
            self.assertNotIn('api_key',args);self.assertNotIn('encrypted_key',args)
            return ModelCompletion(envelope(stage),{'tool':stage,'billable':False,'amount_usd':'0','source_network_requests':1 if stage=='wiki_search' else 2},{'request_headers_sent':True,'http_status':200})
        self.org.web.transport=cloud;self.org.web.wikimedia_transport=source
        self.worker=asyncio.create_task(self.org.worker())
    async def ask(self):
        turn=await self.chat.submit('desktop','水循环是什么？','wiki-'+__import__('uuid').uuid4().hex)
        await asyncio.wait_for(self.chat.wait(),30);return self.chat.get(turn['id'])
    async def test_failed_native_fetch_uses_exact_revision_same_source(self):
        self.enable();result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual(self.cloud,['search','fetch']);self.assertEqual([r[0] for r in self.source],['wiki_fetch'])
        item=self.chat.knowledge.show_evidence(self.persona['id'],result['id'])['evidence'][0]
        self.assertEqual(item['source_origin'],'wikimedia_api');self.assertEqual(item['source_revision_id'],77)
        self.assertIn('oldid=77',item['version_url']);self.assertEqual(item['source_snapshot_kind'],'revision_time')
    async def test_nonarticle_candidates_use_registered_topic_only(self):
        self.enable(filtered=True);result=await self.ask();self.assertEqual(result['status'],'completed',result['error_code'])
        self.assertEqual([r[0] for r in self.source],['wiki_search','wiki_fetch'])
        self.assertEqual(self.source[0][1],{'query':'水循环'})
    async def test_source_disable_blocks_old_answers_and_import_stays_disabled(self):
        self.enable();result=await self.ask()
        package=json.loads(__import__('pathlib').Path(self.service.export_persona()['path']).read_text(encoding='utf-8'))
        self.assertEqual(package['format_version'],22)
        false_version=copy.deepcopy(package);false_version['format_version']=21
        with self.assertRaises(RuntimeErrorBase):import_export(self.store,false_version,self.persona['id'],'bad21')
        imported=import_export(self.store,package,self.persona['id'],'good22')
        self.assertFalse(self.org.web.policy(imported['id'])['data']['enabled'])
        sources=self.chat.knowledge.sources();data=copy.deepcopy(sources['data']);data['wikipedia_zh']['enabled']=False
        self.chat.knowledge.update_sources(data,sources['revision'],'disable-wiki')
        self.assertIsNone(self.chat.get(result['id'])['reply'])

class ParsingTests(unittest.TestCase):
    def test_extraction_retains_math_and_headings_without_scripts(self):
        value=plain('<p>力：<math alttext="F=ma"><mi>F</mi></math></p><h2>条件</h2><p>质量不变。</p><script>evil()</script>')
        self.assertIn('F=ma',value);self.assertIn('## 条件',value);self.assertNotIn('evil',value)
    def test_revision_mismatch_cannot_publish_a_body(self):
        raw=json.loads(envelope());value=json.loads(raw['responses'][1]['body']);value['query']['pages'][0]['revisions'][0]['revid']=78
        body=json.dumps(value);raw['responses'][1].update(body=body,sha256=hashlib.sha256(body.encode()).hexdigest())
        with self.assertRaises(ModelError):parse('wiki_fetch',json.dumps(raw))
    def test_only_public_article_identifiers_and_registered_query_are_used(self):
        self.assertIsNone(article_title('https://zh.wikipedia.org/wiki/User:Private'))
        self.assertIsNone(article_title('https://zh.wikipedia.org/wiki/Portal:气象'))
        self.assertEqual(article_title('https://zh.wikipedia.org/wiki/水循环'),'水循环')
        self.assertIsNone(public_query('DeepSeek site:api-docs.deepseek.com'))
        self.assertIsNone(public_query('水循环 -site:zh.wikipedia.org'))
