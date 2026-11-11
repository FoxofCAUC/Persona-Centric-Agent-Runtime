"""Registered public Wikipedia lookups, exact-revision bodies and raw receipts."""
import asyncio
from datetime import datetime,timezone
import hashlib
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import parse_qs,quote,unquote,urlsplit
import aiohttp
from .chat import ModelCompletion,ModelError,model_tls_context
from .knowledge_dns import public_target,system_proxy
from .knowledge_fetch import public_identifier

HOST='zh.wikipedia.org'
ENDPOINT='https://'+HOST+'/w/api.php'
EXTRACTION='mediawiki-html-text-v1'
NAMESPACES={'portal','主题','主題','category','分类','分類','template','模板','special','特殊','help','帮助','幫助','wikipedia','维基百科','維基百科','file','文件','檔案','module','模块','模組','talk','讨论','討論','user','用户','用戶','使用者','user talk','用户讨论','用戶討論','mediawiki'}

def article_title(url):
    parsed=urlsplit(url)
    if parsed.hostname not in (HOST,'zh.m.wikipedia.org') or parsed.scheme not in ('http','https'):return None
    if parsed.path.startswith('/wiki/'):title=unquote(parsed.path[6:])
    elif re.match(r'^/zh(?:-hans|-hant|-cn|-tw|-hk|-mo|-sg|-my)?/',parsed.path):title=unquote(parsed.path.split('/',2)[2])
    elif parsed.path=='/w/index.php':title=parse_qs(parsed.query).get('title',[''])[0]
    else:return None
    title=title.replace('_',' ').strip()
    if title.split(':',1)[0].split('：',1)[0].casefold() in NAMESPACES:return None
    try:return public_identifier('wikipedia_zh',title)
    except Exception:return None

def public_query(query):
    if re.search(r'(?<!\S)-site:(?:zh\.wikipedia\.org|zh\.m\.wikipedia\.org)(?:\s|$)',query):return None
    sites=re.findall(r'(?<!\S)site:([^\s]+)',query)
    if sites and not any(value in (HOST,'zh.m.wikipedia.org') for value in sites):return None
    value=re.sub(r'(?<!\S)-?site:\S+','',query).strip()
    return value if value else None

class PlainArticle(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0;self.math=0
    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if self.skip:self.skip+=tag not in ('br','img','hr','meta','link','input');return
        if tag in ('script','style') or 'mw-editsection' in values.get('class','').split() or tag=='sup' and 'reference' in values.get('class','').split():self.skip=1;return
        if self.math:self.math+=1;return
        if tag=='math' and values.get('alttext'):
            self.parts.append(values['alttext']);self.math=1;return
        if re.fullmatch(r'h[1-6]',tag):self.parts.append('\n'+'#'*int(tag[1])+' ')
        elif tag in ('p','div','li','tr','section','dt','dd','blockquote'):self.parts.append('\n')
        elif tag in ('td','th'):self.parts.append(' | ')
        elif tag in ('br','hr'):self.parts.append('\n')
        elif tag=='img' and 'mwe-math-fallback-image-inline' in values.get('class','') and values.get('alt'):self.parts.append(values['alt'])
    def handle_endtag(self,tag):
        if self.skip:self.skip-=1;return
        if self.math:self.math-=1;return
        if tag in ('p','div','li','tr','section','dt','dd','blockquote') or re.fullmatch(r'h[1-6]',tag):self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip and not self.math:self.parts.append(data)
    def text(self):
        value=re.sub(r'[^\S\n]+',' ',''.join(self.parts))
        return re.sub(r'\n\s*\n+', '\n\n',value).strip()

def plain(html):
    parser=PlainArticle();parser.feed(html);parser.close();return parser.text()

def failure(code,responses,status=None):
    error=ModelError(code,usage={'tool':'wikimedia','billable':False,'amount_usd':'0','source_network_requests':len(responses)},
        diagnostics={'request_headers_sent':bool(responses),'http_status':status,'read_only_source':True})
    error.tool_response=json.dumps({'provider':'wikimedia_api','responses':responses},ensure_ascii=False)
    return error

async def execute(stage,args,timeout):
    responses=[];sent=False;started=time.monotonic()
    try:await public_target(HOST,443,timeout)
    except ModelError as error:
        error.usage={'tool':stage,'provider':'wikimedia_api','billable':False,'amount_usd':'0','source_network_requests':0}
        raise
    trace=aiohttp.TraceConfig()
    async def headers_sent(*_):
        nonlocal sent
        sent=True
    trace.on_request_headers_sent.append(headers_sent)
    async def get(client,parameters):
        nonlocal sent
        sent=False
        parameters=parameters|{'format':'json','formatversion':2}
        async with client.get(ENDPOINT,params=parameters,headers={'Accept':'application/json','User-Agent':'PCAR/0.9 public-knowledge-reader (https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime)'},proxy=system_proxy(),allow_redirects=False) as response:
            raw=bytearray()
            async for part in response.content.iter_chunked(65536):
                raw.extend(part)
                if len(raw)>8*1024**2:raise failure('wiki_response_too_large',responses,response.status)
            text=raw.decode('utf-8',errors='replace')
            row={'endpoint':ENDPOINT,'parameters':parameters,'http_status':response.status,'body':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}
            responses.append(row)
            if response.status!=200:raise failure('wiki_http_'+str(response.status),responses,response.status)
            try:return json.loads(text)
            except ValueError:return {}
    try:
        async with asyncio.timeout(max(.001,timeout-(time.monotonic()-started))):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),connector=aiohttp.TCPConnector(ssl=model_tls_context()),trust_env=False,trace_configs=[trace]) as client:
                if stage=='wiki_search':
                    await get(client,{'action':'query','list':'search','srsearch':args['query'],'srnamespace':0,'srlimit':5,'srprop':'snippet|timestamp'})
                else:
                    title=article_title(args['url'])
                    if not title:raise failure('wiki_article_not_authorized',responses)
                    oldid=parse_qs(urlsplit(args['url']).query).get('oldid',[None])[0]
                    parameters={'action':'parse','page':title,'prop':'text|revid','redirects':1}
                    if oldid and oldid.isdecimal():parameters.pop('page');parameters['oldid']=int(oldid)
                    data=await get(client,parameters)
                    revision=data.get('parse',{}).get('revid')
                    if type(revision) is int and revision>0:
                        await get(client,{'action':'query','revids':revision,'prop':'revisions|info','rvprop':'ids|timestamp','inprop':'url','meta':'siteinfo','siprop':'rightsinfo'})
        envelope={'provider':'wikimedia_api','stage':stage,'responses':responses}
        return ModelCompletion(json.dumps(envelope,ensure_ascii=False),{'tool':stage,'provider':'wikimedia_api','billable':False,'amount_usd':'0','source_network_requests':len(responses),'elapsed_ms':round((time.monotonic()-started)*1000,3)},
                               {'request_headers_sent':sent,'http_status':200,'read_only_source':True})
    except asyncio.CancelledError as error:
        partial=failure('wiki_source_cancelled',responses)
        error.usage=partial.usage;error.tool_response=partial.tool_response;error.diagnostics=partial.diagnostics
        raise
    except ModelError:raise
    except (aiohttp.ClientError,TimeoutError,OSError):raise failure('wiki_transport_unavailable',responses) from None

def parse(stage,text):
    try:
        envelope=json.loads(text);responses=envelope['responses']
        if envelope['provider']!='wikimedia_api' or envelope['stage']!=stage or not responses or len(responses)>2:raise ValueError()
        if any(row['endpoint']!=ENDPOINT or row['http_status']!=200 or hashlib.sha256(row['body'].encode()).hexdigest()!=row['sha256'] for row in responses):raise ValueError()
        data=json.loads(responses[0]['body'])
        if stage=='wiki_search':
            results=[]
            for row in data['query']['search']:
                if row['ns']!=0:continue
                url='https://'+HOST+'/wiki/'+quote(row['title'].replace(' ','_'))
                if not article_title(url):continue
                results.append({'title':row['title'],'url':url,'summary':plain(row.get('snippet','')),'snippet':plain(row.get('snippet','')),'content':'','date':row.get('timestamp'),'provider':'wikimedia_api'})
            return {'results':results}
        if len(responses)==1 and data.get('error',{}).get('code')=='missingtitle':return {'not_found':True}
        parsed=data['parse'];info=json.loads(responses[1]['body'])['query'];pages=info['pages']
        if len(pages)!=1 or pages[0].get('missing') or pages[0]['ns']!=0 or pages[0]['pageid']!=parsed['pageid']:raise ValueError()
        revision=pages[0]['revisions'][0]
        if type(parsed['revid']) is not int or parsed['revid']<=0 or revision['revid']!=parsed['revid']:raise ValueError()
        body=plain(parsed['text'])
        if not body:raise ValueError()
        date=revision.get('timestamp')
        if date:
            stamp=datetime.fromisoformat(date.replace('Z','+00:00'))
            if stamp.tzinfo is None:raise ValueError()
        rights=info.get('rightsinfo')
        upstream={'provider':'wikimedia_api','page_id':parsed['pageid'],'revision_id':parsed['revid'],'revision_at':date,
                  'version_url':ENDPOINT.removesuffix('api.php')+'index.php?oldid='+str(parsed['revid']),
                  'rightsinfo':rights,'extraction':EXTRACTION,'raw_response_sha256':[r['sha256'] for r in responses]}
        return {'title':parsed['title'],'content':body,'upstream':upstream}
    except (ValueError,KeyError,TypeError,IndexError,AttributeError):raise ModelError('wiki_response_invalid') from None
