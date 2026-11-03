"""Independently authored protocol fixtures, never derived from evaluation labels."""
from persona_runtime.archive import encoded
from persona_runtime.dialogue_context import FIELDS

def selection(case_id):
    # Empty is legal; S01 may legitimately be a poor stylistic selection in variants.
    return {'schema_version':1,'lore_ids':[],'example_ids':[]}


def joint(case_id, evidence):
    frame={k:[] for k in FIELDS}
    catalog=evidence['sources']
    current=[s for s in catalog if s['id'] in evidence['current']]
    def row(field,text,source=None,certainty='explicit',quote=None):
        src=source or current[-1]
        frame[field].append({'text':text,'certainty':certainty,'evidence':[
            {'source_id':src['id'],'quote':quote or src['text'][:180]}] if certainty!='unknown' else []})
    gap='sufficient'
    if case_id=='S09': row('requests','预订周四上午一小时。')
    elif case_id=='S10': row('willingness','现在只想吐槽，不要建议。')
    elif case_id=='S11':
        src=next(s for s in catalog if s['kind']=='history' and s['authority']=='user')
        row('requests','计算布片面积，结果带单位。')
        row('progress','已提供长18厘米和宽7厘米。',src)
    elif case_id=='S12':
        row('uncertainty','无法确定指耳机还是台灯。',certainty='unknown'); gap='unknown'
    elif case_id=='S13': row('progress','仅开头四格完成，整段追逐戏仍未完成。')
    elif case_id=='S14':
        row('progress','名单已核对。');row('willingness','不庆祝，想安静一下。')
    elif case_id=='S15': row('requests','解释防潮，而非防水。')
    else: row('uncertainty','半句未完成，不能确定具体对象。',certainty='unknown');gap='unknown'
    return {'schema_version':1,'evidence_hash':evidence['digest'],'frame':frame,
            'selection':selection(case_id),'gap_status':gap}


def reply(case_id):
    text={'S02':'432','S11':'126平方厘米','S09':'周四上午，一小时。','S10':'排到哪儿了，还看得见队尾吗？',
          'S14':'那就安静待会儿。','S13':'开头四格有了，追逐的节奏能先看一小段。',
          'S12':'你说的是耳机还是台灯？','S16':'他那半句说的是哪件东西？'}.get(case_id,'这一下还挺有画面。')
    return encoded({'schema_version':3,'behavior_intent':'respond','messages':[
        {'content':text,'content_kind':'chat','cancel_if_new_input':False}]})
