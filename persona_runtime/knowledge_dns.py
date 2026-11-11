"""Public naming checks for remote Fetch, with authenticated HTTPS DNS fallback."""
import asyncio
import ipaddress
import json
import time
from urllib.parse import urlsplit
import aiohttp
from .chat import ModelError,model_tls_context

RESOLVER='https://cloudflare-dns.com/dns-query'
VERSION='public-target-dns-v1'

def system_proxy():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            if not winreg.QueryValueEx(key,'ProxyEnable')[0]:return None
            raw=winreg.QueryValueEx(key,'ProxyServer')[0]
        if '=' in raw:
            values=dict(part.split('=',1) for part in raw.split(';') if '=' in part)
            raw=values.get('https') or values.get('http') or ''
        value=raw if '://' in raw else 'http://'+raw
        parsed=urlsplit(value)
        if parsed.scheme in ('http','https') and parsed.hostname in ('127.0.0.1','localhost','::1') and not parsed.username and not parsed.password and parsed.port:
            return value
    except (ImportError,OSError,ValueError,TypeError):pass
    return None

def answer_addresses(data,hostname,kind):
    try:
        if (data['Status']!=0 or data.get('TC') is True or not isinstance(data['Question'],list)
                or len(data['Question'])!=1 or data['Question'][0]['name'].lower().rstrip('.')!=hostname.lower().rstrip('.')
                or data['Question'][0]['type']!=kind):raise ValueError()
        records=data.get('Answer',[])
        if not isinstance(records,list) or len(records)>64:raise ValueError()
        names={hostname.lower().rstrip('.')}
        for _ in range(8):
            before=len(names)
            for row in records:
                if row.get('type')==5 and row.get('name','').lower().rstrip('.') in names:
                    names.add(row['data'].lower().rstrip('.'))
            if len(names)==before:break
        values=[]
        for row in records:
            if row.get('type')==kind and row.get('name','').lower().rstrip('.') in names:
                address=ipaddress.ip_address(row['data'])
                if address.version!=(4 if kind==1 else 6):raise ValueError()
                values.append(str(address))
        return values
    except (ValueError,KeyError,TypeError,AttributeError):raise ModelError('web_dns_unavailable',diagnostics={'request_headers_sent':False}) from None

async def https_answers(hostname,timeout):
    proxy=system_proxy()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),connector=aiohttp.TCPConnector(ssl=model_tls_context()),trust_env=False) as client:
        async def query(kind):
            async with client.get(RESOLVER,params={'name':hostname,'type':kind},headers={'Accept':'application/dns-json'},proxy=proxy,allow_redirects=False) as response:
                if response.status!=200:raise ModelError('web_dns_unavailable',diagnostics={'request_headers_sent':False})
                raw=bytearray()
                async for part in response.content.iter_chunked(16384):
                    raw.extend(part)
                    if len(raw)>65536:raise ModelError('web_dns_unavailable',diagnostics={'request_headers_sent':False})
                return answer_addresses(json.loads(raw),hostname,kind)
        rows=await asyncio.gather(query(1),query(28))
    return sorted(set(rows[0]+rows[1])),bool(proxy)

async def public_target(hostname,port,timeout):
    def failure(code):
        return ModelError(code,usage={'tool':'fetch','billable':False,'amount_usd':'0'},diagnostics={'request_headers_sent':False})
    try:
        async with asyncio.timeout(timeout):
            try:records=await asyncio.get_running_loop().getaddrinfo(hostname,port)
            except OSError:records=[]
            local=sorted({str(ipaddress.ip_address(row[4][0])) for row in records})
            addresses=local;method='system';via_proxy=False
            if not addresses or not all(ipaddress.ip_address(value).is_global for value in addresses):
                addresses,via_proxy=await https_answers(hostname,timeout);method='https_dns'
            if not addresses or not all(ipaddress.ip_address(value).is_global for value in addresses):raise failure('web_target_not_public')
            return {'version':VERSION,'host':hostname,'addresses':addresses,'system_addresses':local,'method':method,
                    'resolver':RESOLVER if method=='https_dns' else None,'via_system_proxy':via_proxy,'checked_epoch':time.time(),
                    'scope':'public_naming_only_provider_dns_and_redirects_not_verified'}
    except ModelError as error:
        if error.usage is None:error.usage={'tool':'fetch','billable':False,'amount_usd':'0'}
        raise
    except (aiohttp.ClientError,TimeoutError,ValueError,OSError):raise failure('web_dns_unavailable') from None
