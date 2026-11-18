"""Opt-in, versioned projections for the E.N5 fixed-history style study."""
import json
import re

from .archive import fingerprint, read_exact, turn_scope
from .archive_query import lexical, normal
from .corpus_files import digest
from .models import RuntimeErrorBase
from .profile import encode
from .turn_input import load_turn_input


VERSION = 'en5-style-factors-v1'
KEY = 'en5_style_study'
ARMS = ('B', 'P', 'H', 'PH')
FIELD = 'social.context_rules'


def require(condition, detail):
    if not condition:
        raise RuntimeErrorBase('style_study_' + detail)


def policy_for(turn):
    cfg = json.loads(turn.get('context_policy_snapshot') or '{}').get(KEY)
    if cfg is None:
        return None
    require(isinstance(cfg, dict) and cfg.get('version')==VERSION and cfg.get('arm') in ARMS,
            'invalid_revision')
    require(isinstance(cfg.get('sessions'), list) and cfg['sessions'] and all(isinstance(s,str) and s for s in cfg['sessions'])
            and all(isinstance(cfg.get(k),str) and cfg[k] for k in ('study_id','persona_id','character_version')),
            'invalid_scope')
    character = json.loads(turn.get('character_snapshot') or '{}')
    model = json.loads(turn.get('model_snapshot') or '{}')
    require(turn.get('channel')=='desktop' and turn.get('reply_mode_snapshot')=='context_full'
            and model.get('name')=='deepseek-v4-pro' and model.get('_companion_profile',{}).get('version')=='companion-runtime-v8'
            and json.loads(turn['context_policy_snapshot']).get('companion',{}).get('version')=='companion-runtime-v8'
            and cfg['persona_id']==turn.get('persona_id')==character.get('persona_id')
            and turn.get('session_id') in cfg['sessions'] and cfg['character_version']==character.get('id'),
            'scope_changed')
    return cfg


def project_parts(parts, projection):
    """Remove one reviewed interval in one structured character field."""
    require(isinstance(projection,dict) and projection.get('field')==FIELD
            and isinstance(projection.get('span'),list) and len(projection['span'])==2
            and all(isinstance(projection.get(k),str) for k in ('source_sha256','removed_sha256')), 'wrong_field')
    result = []
    changes = 0
    for part in parts:
        if part['kind']!='anchor' or part['id']!=FIELD:
            result.append(part)
            continue
        item = json.loads(part['text'])
        original = item['data']
        require(item.get('kind')=='anchor' and item.get('id')==FIELD and isinstance(original,str)
                and encode(item)==part['text'] and digest(original.encode())==projection['source_sha256'],
                'field_changed')
        start,end = projection['span']
        require(type(start) is int and type(end) is int and 0<=start<end<=len(original)
                and digest(original[start:end].encode())==projection['removed_sha256'], 'span_changed')
        item['data'] = original[:start]+original[end:]
        result.append(dict(part,text=encode(item)))
        changes += 1
    require(changes<=1,'duplicate_field')
    return result, bool(changes)


def weak_persona_match(group, targets, terms, literals, recent_terms, explicit):
    if group['turn_id'] in explicit or not targets:
        return None
    docs = {('event',m['id']):('user',m['text']) for m in group['members']}
    for r in group['replies']:
        docs[('part' if r.get('kind')=='part' else 'event',r['id'])]=('persona',r['text'])
    hits=set()
    for target in targets:
        doc=docs.get((target['kind'],target['id']))
        if doc is None or doc[0]!='persona' or any(lit in normal(doc[1]) for lit in literals):
            return None
        matched=terms.intersection(lexical(doc[1],document=True)[0])
        if not matched:
            return None
        hits.update(matched)
    if len(hits)==1:
        term,=hits
        if re.fullmatch(r'[\u3400-\u9fff]{1,2}',term) and term not in recent_terms:
            return term
    return None


def filter_history(db, turn, items, evidence, references):
    from .context_builders import query_terms
    terms,literals,_=query_terms(load_turn_input(db,turn))
    terms=set(terms)
    recent=set()
    for item in evidence['items']:
        if item['kind']=='recent':
            for doc in [*item['group']['members'], *item['group']['replies']]:
                recent.update(lexical(doc['text'],document=True)[0])
    kept,omitted=[],[]
    explicit=set(references['turn_ids'])
    matches=evidence['retrieval'].get('matched_targets',{})
    for item in items:
        group=item['group']
        tid=group['turn_id']
        word=weak_persona_match(group,matches.get(tid,[]),terms,literals,recent,explicit) if item['kind']=='retrieval' else None
        if word is None:
            kept.append(item)
        else:
            omitted.append({'turn_id':tid,'term':word,'reason':'single_unsupported_persona_term'})
    return kept,omitted


def verify_dispatch(engine, db, turn, details, messages):
    cfg=policy_for(turn)
    if cfg is None:
        return
    from .storage import Store
    from .character import head
    require(Store.current(db,required=False)==turn['persona_id'] and head(db,turn['persona_id'])['id']==cfg['character_version'],
            'authority_changed')
    from .profile import source_blocked
    from .turn_input import render_input, input_text
    from .references import prepare as verify_references
    from .context_policy import current
    require(current(db,turn['persona_id'],turn['channel']).get(KEY)==cfg,'policy_withdrawn')
    require(not source_blocked(db,[m['id'] for m in load_turn_input(db,turn)]),'input_withdrawn')
    verify_references(db,turn)
    exact=[]
    for item in details.get('history_groups',[]):
        found=read_exact(db,turn_scope(db,turn),{'kind':'turn','id':item['turn_id']})
        require(found.get('usable'), 'history_withdrawn')
        exact.append({'role':'user','content':render_input(found['members'],bool(found['batch_id']))})
        if found['replies']:
            exact.append({'role':'assistant','content':'\n'.join(r['text'] for r in found['replies'])})
    exact.append({'role':'user','content':input_text(db,turn)})
    require(messages[1:]==exact,'frozen_conversation_changed')
    guard=getattr(engine,'style_study_dispatch_guard',None)
    require(callable(guard),'specific_input_approval_required')
    guard(db,turn,cfg,fingerprint(messages))
