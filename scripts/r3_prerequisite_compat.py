"""Synthetic legacy requests, captured before v4; no provider or mutable old pack."""
import hashlib
from persona_runtime.archive import fingerprint
from persona_runtime.character_selector_protocol import messages,prompt_for
from persona_runtime.provider_compat import build_request
from tests.prompt_baseline_fixture import capture_all


def capture(provider):
    pool={'payload':{'roots':{'lore':['fixed-fact'],'example':['fixed-example']},
        'groups':{'lore:fixed-fact':['fixed-fact'],'example:fixed-example':['fixed-example']},
        'sources':[{'id':'fixed-fact','kind':'lore','text':'人工基线角色的编号为42。','source_hash':fingerprint('fixed-fact')},
                   {'id':'fixed-example','kind':'example','scenario':'人工表达对照','text':'先把眼前这一处看清楚。','source_hash':fingerprint('fixed-example')}]}}
    inputs=[(['人工基线：编号是多少？'],'',4),
            (['还没有结束。','更正，刚结束，现在想歇会儿。'],'',4),
            (['接着这条人工引用说。'],'人工引用：这一项仍在进行。',0)]
    selected={};prompts={}
    for version in ('character-selector-v1','character-selector-v2','character-selector-v3'):
        text=prompt_for(version);prompts[version]={'text':text,'sha256':hashlib.sha256(text.encode()).hexdigest()}
        for index,(members,refs,limit) in enumerate(inputs):
            payload=messages(pool,members,refs,limit,48000,prompt_version=version)
            request,contract=build_request(provider|{'_json_output':True,'_character_selection':True},payload)
            selected[f'{version}-{index}']={'messages':payload,'request':request,'contract':contract,'input_hash':fingerprint(payload)}
    # These 34 main assemblies have fixed selected material, IDs and time.
    return {'prompts':prompts,'selector_requests':selected,'main_requests':capture_all()}
