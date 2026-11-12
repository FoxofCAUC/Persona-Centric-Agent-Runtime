"""Lossless citation formatting and annotation-only JSON compatibility."""
import json
import re

def strict_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        result[key]=value
    return result

def opening_key_quotes(response,stage):
    allowed={
        'knowledge_extract':{'format_version','claims','subject','predicate','value','kind','quote','window_id','valid_from','valid_until'},
        'knowledge_review':{'format_version','decisions','index','supported','temporal_ok','version_ok','kind','note'},
        'goal':{'format_version','queries','freshness'},'select':{'format_version','section_ids'},
        'translate':{'format_version','units','text','kind','refs','material_id','quote'},
        'compose':{'format_version','units','text','kind','refs','material_id','quote'},
        'verify':{'format_version','decisions','index','supported','temporal_ok','version_ok','note'},
        'web_plan':{'format_version','topic_ids'},'web_fetch_plan':{'format_version','fetch_ids'},
        'public_page_plan':{'format_version','candidates','title','kind'},
        'public_page_review':{'format_version','decisions','id','public','relevant','private_detail','note'}}.get(stage,set())
    current=response;keys=[];positions=[]
    for _ in range(13):
        try:
            result=json.loads(current,object_pairs_hook=strict_pairs)
            return result,[{'kind':'inserted_opening_key_quotes','keys':keys,'positions':positions,'values_unchanged':True}] if keys else []
        except json.JSONDecodeError as error:
            if len(keys)>=12 or error.msg!='Expecting property name enclosed in double quotes':break
            match=re.match(r'([a-z_]+)"\s*:',current[error.pos:])
            if not match or match[1] not in allowed:break
            keys.append(match[1]);positions.append(error.pos)
            current=current[:error.pos]+'"'+current[error.pos:]
        except (TypeError,ValueError):break
    return None,[]

def review_annotations(response,error_position):
    # Restrict recovery to the annotation at the end of a typed decision.
    # Truth flags and indexes are parsed as unchanged tokens, never inferred.
    head=re.match(r'\s*\{\s*"format_version"\s*:\s*1\s*,\s*"decisions"\s*:\s*\[',response)
    if not head:return None
    item=re.compile(r'\s*\{\s*"index"\s*:\s*(\d+)\s*,\s*"supported"\s*:\s*(true|false)\s*,\s*"temporal_ok"\s*:\s*(true|false)\s*,\s*"version_ok"\s*:\s*(true|false)\s*,\s*"note"\s*:\s*"([^{}]*)"\s*\}')
    offset=head.end();decisions=[];inside_note=False
    while True:
        match=item.match(response,offset)
        if not match:return None
        inside_note=inside_note or match.start(5)<=error_position<=match.end(5)
        decisions.append({'index':int(match[1]),'supported':match[2]=='true','temporal_ok':match[3]=='true','version_ok':match[4]=='true','note':match[5]})
        if len(decisions)>100:return None
        offset=match.end();end=re.match(r'\s*\]\s*\}\s*$',response[offset:])
        if end:break
        comma=re.match(r'\s*,',response[offset:])
        if not comma:return None
        offset+=comma.end()
    return {'format_version':1,'decisions':decisions} if inside_note else None

def references(refs,materials):
    available={m['id']:m for m in materials};out=[];changes=[]
    for ref in refs:
        if not isinstance(ref,dict) or set(ref)!={'material_id','quote'}:raise ValueError('reference_shape')
        material=available.get(ref['material_id']);quote=ref['quote']
        if not material or not isinstance(quote,str) or not quote.strip() or len(quote)>3000 or quote not in material['text']:
            raise ValueError('quote_not_exact')
        if len(quote)<=500:out.append(dict(ref));continue
        # Balanced slices retain the entire exact quote, with no discarded tail.
        count=(len(quote)+499)//500
        for i in range(count):out.append({'material_id':ref['material_id'],'quote':quote[len(quote)*i//count:len(quote)*(i+1)//count]})
        changes.append({'kind':'split_exact_quote','material_id':ref['material_id'],'original_characters':len(quote),'pieces':count})
    while len(out)>3:
        candidates=[]
        for i,a in enumerate(out):
            body=available[a['material_id']]['text'];left=body.index(a['quote'])
            for j,b in enumerate(out[i+1:],i+1):
                if a['material_id']!=b['material_id']:continue
                right=body.index(b['quote']);start,end=min(left,right),max(left+len(a['quote']),right+len(b['quote']))
                if end-start<=500:candidates.append((end-start,i,j,start,end))
        if not candidates:raise ValueError('references')
        _,i,j,start,end=min(candidates);ident=out[i]['material_id']
        out[i]={'material_id':ident,'quote':available[ident]['text'][start:end]};out.pop(j)
        changes.append({'kind':'coalesced_same_material_quotes','material_id':ident,'start':start,'end':end})
    return out,changes
