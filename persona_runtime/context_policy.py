"""Versioned context policy shares the existing ingress policy authority."""
import json
from .models import LOCAL_USER_ID, identifier, timestamp

DEFAULT_CONTEXT={'context_mode':'working_set','cross_session':False}


def new_persona_defaults():
    """Fresh identities use released features; historical snapshots stay unchanged."""
    from .ingress import ROLLING_PARAMS
    from .character_policy import choose
    return {'mode':'natural_batch','reply_mode':'natural_structured',
            'data':{**ROLLING_PARAMS,'context':{**DEFAULT_CONTEXT,
                'character_retrieval':choose('hybrid_llm')}}}


def values(policy):
    data=json.loads(policy['data_json']).get('context',{})
    from .character_policy import read
    return {'context_mode':data.get('context_mode','legacy_recent'),
            'cross_session':bool(data.get('cross_session',False)), 'character_retrieval':read(data), 'disposition_mode':data.get('disposition_mode','baseline')}


def snapshot(policy,conn=None):
    result = {'schema_version':1,'policy_id':policy['id'],'revision':policy['revision'],**values(policy)}
    if conn is not None:
        from .disposition import accepted_snapshot, SHADOW_VERSION, COMPILER_VERSION
        from .disposition_core.contracts import VERSION
        try:
            result['disposition']=accepted_snapshot(conn,policy['persona_id'],result['disposition_mode'])
            if result['disposition_mode']=='assist':
                from .decision_capsule import VERSION
                result['disposition']['renderer']=VERSION
        except Exception:
            if result['disposition_mode']=='assist':raise
            from .disposition_shadow import gap
            gap('shadow_snapshot_failed')
            result['disposition']={'mode':'shadow','policy':SHADOW_VERSION,'kernel':VERSION,'compiler':COMPILER_VERSION,
                'version_id':None,'document_hash':None,'status':'snapshot_invalid'}
    return result


def current(conn,persona_id,channel):
    row=conn.execute('SELECT p.* FROM interaction_policies p JOIN interaction_heads h ON h.version_id=p.id WHERE h.persona_id=? AND h.channel=?',(persona_id,channel)).fetchone()
    if row:return snapshot(dict(row),conn)
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
                data['expression']=expression_snapshot(reply_mode,vid,version=3)
            conn.execute('INSERT INTO interaction_policies(id,persona_id,user_id,channel,revision,mode,data_json,created_at,reply_mode) VALUES(?,?,?,?,?,?,?,?,?)',
                         (vid,pid,LOCAL_USER_ID,channel,old['revision']+1 if old else 1,
                          mode,json.dumps(data,ensure_ascii=False,sort_keys=True),timestamp(),reply_mode))
            conn.execute('INSERT INTO interaction_heads VALUES(?,?,?,?) ON CONFLICT(persona_id,channel) DO UPDATE SET version_id=excluded.version_id',(pid,LOCAL_USER_ID,channel,vid))
