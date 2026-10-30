"""Wire-level reply contracts and bounded, body-free validation diagnostics."""
import hashlib
import json

from .reply_batch import _unique_object, _valid_fences, expression_version
from .bubble_contract import V3

VERSION='reply-wire-v1'


def response_format(mode, expression=None):
    version=expression_version(expression,mode)
    if mode not in ('natural_structured','context_structured'):return None
    natural=mode=='natural_structured'
    item={'type':'object','additionalProperties':False,'properties':
          {'content':{'type':'string','minLength':1,'maxLength':V3.item_characters},
           'content_kind':{'type':'string','enum':list(V3.kinds)},
           'cancel_if_new_input':{'type':'boolean'}} if natural else
          {'text':{'type':'string','minLength':1,'maxLength':8000},
           'format':{'type':'string','enum':['plain','markdown']}}}
    item['required']=list(item['properties'])
    properties={'schema_version':{'type':'integer','enum':[3 if natural and version==3 else 2 if natural else 1]}}
    if natural:properties['behavior_intent']={'type':'string','enum':list(V3.intents)}
    properties['messages' if natural else 'segments']={'type':'array','minItems':1,'maxItems':V3.max_messages if natural and version==3 else 3,'items':item}
    return {'type':'json_schema','json_schema':{'name':'pcar_reply_v3' if natural and version==3 else 'pcar_reply_v2' if natural else 'pcar_reply_v1',
        'strict':True,'schema':{'type':'object','additionalProperties':False,
                              'properties':properties,'required':list(properties)}}}


def descriptor(mode, expression=None):
    fmt=response_format(mode,expression)
    return {'version':VERSION,'response_format':'json_schema' if fmt else 'text','mode':mode,'expression_version':expression_version(expression),
            'schema_sha256':hashlib.sha256(json.dumps(fmt,sort_keys=True,separators=(',',':')).encode()).hexdigest() if fmt else None}


def validation_failure(raw,mode,expression=None):
    """Never return body excerpts, key names supplied by a model, or exception text."""
    result={'version':VERSION,'status':'rejected','reason':'schema_invalid'}
    def reason(code,**metadata):return result|{'reason':code,**metadata}
    if not isinstance(raw,str):return reason('output_type')
    result['output_characters']=len(raw)
    if not raw.strip():return reason('empty_output')
    if '\x00' in raw:return reason('nul_character')
    if mode not in ('natural_structured','context_structured'):return reason('invalid_mode')
    if raw.lstrip().startswith(('```','~~~')):return reason('outer_code_fence')
    try:value=json.loads(raw,object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:return reason('invalid_json',json_error_position=exc.pos)
    except (ValueError,TypeError,UnicodeError):return reason('duplicate_json_key')
    if not isinstance(value,dict):return reason('envelope_type')
    version=value.get('schema_version')
    if type(version) is not int or version not in ((3,) if mode=='natural_structured' and expression_version(expression)==3 else (1,2) if mode=='natural_structured' else (1,)):
        return reason('schema_version')
    natural=version in (2,3)
    expected={'schema_version','behavior_intent','messages'} if natural else {'schema_version','segments'}
    if set(value)!=expected:return reason('envelope_fields')
    items=value['messages' if natural else 'segments']
    if not isinstance(items,list):return reason('messages_type')
    if not 1<=len(items)<=(V3.max_messages if version==3 else 3):return reason('message_count',message_count=len(items))
    bodies=[]
    for index,item in enumerate(items):
        if not isinstance(item,dict):return reason('message_type',message_index=index)
        keys={'content','content_kind','cancel_if_new_input'} if natural else {'text','format'}
        if set(item)!=keys:return reason('message_fields',message_index=index)
        text=item['content' if natural else 'text']
        if not isinstance(text,str):return reason('body_type',message_index=index)
        if not text.strip():return reason('empty_body',message_index=index)
        if '\x00' in text:return reason('nul_character',message_index=index)
        if len(text)>8000:return reason('body_capacity',message_index=index)
        if not _valid_fences(text):return reason('unclosed_body_fence',message_index=index)
        bodies.append(text)
        if mode=='context_structured' and item.get('format') not in ('plain','markdown'):return reason('expression_metadata',message_index=index)
    if sum(map(len,bodies))>12000:return reason('total_body_capacity')
    if version!=3 and len('\n\n'.join(bodies))>8000:return reason('fallback_capacity')
    return reason('expression_metadata')
