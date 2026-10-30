"""Versioned context policy shares the existing ingress policy authority."""
import json
from .models import LOCAL_USER_ID, identifier, timestamp

DEFAULT_CONTEXT={'context_mode':'working_set','cross_session':False}


def new_persona_defaults():
    """Fresh identities use released features; historical snapshots stay unchanged."""
    from .ingress import PARAMS
    from .character_policy import choose
    return {'mode':'natural_batch','reply_mode':'natural_structured',
            'data':{**PARAMS,'context':{**DEFAULT_CONTEXT,
                'character_retrieval':choose('hybrid_llm')}}}


def values(policy):
    data=json.loads(policy['data_json']).get('context',{})
    from .character_policy import read
    return {'context_mode':data.get('context_mode','legacy_recent'),
            'cross_session':bool(data.get('cross_session',False)), 'character_retrieval':read(data)}


def snapshot(policy):
    return {'schema_version':1,'policy_id':policy['id'],'revision':policy['revision'],**values(policy)}


def current(conn,persona_id,channel):
    row=conn.execute('SELECT p.* FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.persona_id=? AND h.channel=?',(persona_id,channel)).fetchone()
    if row:return snapshot(dict(row))
    return {'schema_version':1,'policy_id':None,'revision':0,**DEFAULT_CONTEXT}


def initialize(conn,persona_id=None,*,new_persona=False):
    from .ingress import PARAMS
    ids=[persona_id] if persona_id else [r[0] for r in conn.execute('SELECT id FROM personas')]
    for pid in ids:
        for channel in ('desktop','qq'):
            row=conn.execute('SELECT p.* FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.persona_id=? AND h.channel=?',(pid,channel)).fetchone()
            old=dict(row) if row else None
            if new_persona and old:continue
            data=json.loads(old['data_json']) if old else dict(PARAMS)
            if 'context' in data:continue
            data['context']=dict(DEFAULT_CONTEXT)
            defaults=new_persona_defaults() if new_persona else None
            if defaults:data=defaults['data']
            vid=identifier()
            mode=old['mode'] if old else (defaults['mode'] if defaults else 'legacy_per_message')
            reply_mode=old['reply_mode'] if old else (defaults['reply_mode'] if defaults else 'legacy_full')
            if defaults:
                from .reply_batch import expression_snapshot
                data['expression']=expression_snapshot(reply_mode,vid)
            conn.execute('INSERT INTO interaction_policies(id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES(?,?,?,?,?,?,?,?,?)',
                         (vid,pid,LOCAL_USER_ID,channel,old['revision']+1 if old else 1,
                          mode,json.dumps(data,ensure_ascii=False,sort_keys=True),timestamp(),reply_mode))
            conn.execute('INSERT INTO interaction_heads VALUES(?,?,?,?) ON CONFLICT(persona_id,channel) DO UPDATE SET version_id=excluded.version_id',(pid,LOCAL_USER_ID,channel,vid))
