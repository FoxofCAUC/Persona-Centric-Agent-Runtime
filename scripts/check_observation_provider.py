"""Bounded authenticated catalog check; no generation and no credentials in output."""
import argparse,asyncio,json,ssl,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import aiohttp,certifi
from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.security import unprotect
from scripts.m763_r04 import model_identity
from persona_runtime.provider_compat import endpoint


async def run(home,output):
    home=home.resolve();output=output.resolve()
    if not output.is_relative_to(home/'exports/acceptance') or output.exists():raise ValueError('New controlled evidence path required')
    model=ConfigStore(RuntimePaths(home)).load()['character_selector'];start=time.monotonic()
    result={'normal':False,'model_calls':0,'checked_epoch':time.time(),
            'connection_fingerprint':model_identity(model),'contract':'m763-r04-provider-v1'}
    try:
        connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
        async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=20),trust_env=False) as client:
            async with client.get(endpoint(model,'models'),headers={'Authorization':'Bearer '+unprotect(model['encrypted_key'])},allow_redirects=False) as response:
                result['http_status']=response.status;body=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body)>2*1024*1024:raise ValueError('bounded_response_exceeded')
                data=json.loads(body);valid=isinstance(data,dict) and isinstance(data.get('data'),list)
                listed=valid and any(isinstance(x,dict) and x.get('id')==model['name'] for x in data['data'])
                result.update(catalog_valid=valid,configured_model_listed=listed,normal=response.status==200 and listed)
    except Exception as exc:result['failure_type']=type(exc).__name__
    result['elapsed_ms']=round((time.monotonic()-start)*1000,3)
    with output.open('x',encoding='utf-8') as f:json.dump(result,f,indent=2)
    print(json.dumps(result))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('formal_home',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    asyncio.run(run(a.formal_home,a.output))
