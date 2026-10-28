"""Actual installed SDK conversion probe, no QQ login or network/model calls."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


async def check():
    from persona_runtime.sdk_reference_guard import install
    from persona_runtime.qq_bridge import allowed_message
    from aiocqhttp import Event
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
    install(); install()
    calls=[]
    class Bot:
        async def call_action(self, **args):
            calls.append(args);raise AssertionError('No platform action allowed')
    adapter=object.__new__(AiocqhttpAdapter);adapter.bot=Bot()
    raw={'time':100,'self_id':456,'post_type':'message','message_type':'private','sub_type':'friend',
         'user_id':123,'message_id':111,'sender':{'user_id':123,'nickname':'synthetic'},
         'raw_message':'synthetic','font':0,'message':[{'type':'reply','data':{'id':'99'}},{'type':'text','data':{'text':'请引用本地资料'}}]}
    event=Event.from_payload(raw)
    converted=await adapter._convert_handle_message_event(event)
    wrapper=SimpleNamespace(platform_meta=SimpleNamespace(name='aiocqhttp',id='persona-onebot'),
        message_obj=converted,is_private_chat=lambda:True,get_self_id=lambda:'456',get_sender_id=lambda:'123')
    result=allowed_message(wrapper,{'owner_qq':'123','bot_qq':'456'})
    assert result['reference']=={'kind':'platform_message','message_id':'99'}
    assert result['text']=='请引用本地资料' and not calls
    for parts in ([{'type':'reply','data':{'id':'99'}}],raw['message']+[{'type':'reply','data':{'id':'100'}}],raw['message']+[{'type':'image','data':{'file':'x'}}]):
        changed={**raw,'message':parts};wrapper.message_obj=SimpleNamespace(raw_message=changed,message_id='111')
        assert allowed_message(wrapper,{'owner_qq':'123','bot_qq':'456'}) is None
    return {'actual_sdk_conversion':True,'platform_actions':len(calls),'model_calls':0,'qq_login':False,
            'reference_id_preserved':True,'reject_pure_reply_multiple_reply_media':True}


if __name__=='__main__':
    from persona_runtime.paths import RuntimePaths
    paths=RuntimePaths.from_environment();tmp=paths.home/'tmp';tmp.mkdir(parents=True,exist_ok=True)
    original=Path.cwd()
    with tempfile.TemporaryDirectory(prefix='m70-sdk-',dir=tmp) as directory:
        os.chdir(directory);os.environ.update(ASTRBOT_ROOT=directory,ASTRBOT_CLI='1')
        original_connect=socket.socket.connect
        def deny(sock,address):
            if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost'):
                return original_connect(sock,address)
            raise OSError('External networking disabled in M7.0 SDK probe')
        socket.socket.connect=deny
        try:print(json.dumps(asyncio.run(check()),ensure_ascii=True))
        finally:os.chdir(original)
