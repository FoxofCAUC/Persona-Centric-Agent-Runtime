"""Semantic model inputs; authority IDs, hashes and permissions stay server-side."""
import copy
import json

PROFILE='question_focused_v4'
SEMANTIC=('article_title','section_title','source_name','source_origin','source_snapshot_at','source_snapshot_kind',
          'claim','claim_text','claim_kind','claim_state','valid_from','valid_until','time_state','effective_interval')

def information(evidence):return {k:evidence[k] for k in SEMANTIC if k in evidence}
def material(value,index):return {'id':'M'+str(index+1),'text':value['text'],'source':information(value.get('evidence',{}))}
def source(value,index):
    return {k:v for k,v in value.items() if k in ('title','section','lead','characters','url','summary','snippet','date')}|{'id':'S'+str(index+1),'source':information(value.get('evidence',{}))}
def recent(rows):return [{k:v for k,v in row.items() if k in ('user','assistant','request_state')} for row in rows]

def inputs(data):
    value=copy.deepcopy(data['input'])
    if 'recent_context' in value:value['recent_context']=recent(value['recent_context'])
    if 'materials' in value:value['materials']=[material(row,i) for i,row in enumerate(value['materials'])]
    if 'sources' in value:value['sources']=[source(row,i) for i,row in enumerate(value['sources'])]
    if 'units' in value:
        aliases={row['id']:'M'+str(i+1) for i,row in enumerate(data['input'].get('materials',[]))}
        for unit in value['units']:
            for ref in unit.get('refs',[]):
                if ref.get('material_id') in aliases:ref['material_id']=aliases[ref['material_id']]
    # Public permission/version snapshots are enforced by the program; the
    # model sees permitted choices and material, not execution contracts.
    for key in ('web_policy','source_signature','routing_policy'):value.pop(key,None)
    question=value.pop('question',None)
    if question is not None:value['question']=question
    return value

def restore(data,out):
    value=copy.deepcopy(out);stage=data['stage'];original=data['input']
    if stage in ('select','web_fetch_plan'):
        aliases={'S'+str(i+1):row['id'] for i,row in enumerate(original.get('sources',[]))}
        key='section_ids' if stage=='select' else 'fetch_ids'
        if isinstance(value.get(key),list):value[key]=[aliases.get(item,item) if isinstance(item,str) else item for item in value[key]]
    if stage in ('translate','compose'):
        aliases={'M'+str(i+1):row['id'] for i,row in enumerate(original.get('materials',[]))}
        for unit in value.get('units',[]) if isinstance(value.get('units'),list) else []:
            if not isinstance(unit,dict):continue
            for ref in unit.get('refs',[]) if isinstance(unit.get('refs'),list) else []:
                if isinstance(ref,dict) and isinstance(ref.get('material_id'),str):ref['material_id']=aliases.get(ref['material_id'],ref['material_id'])
    return value
