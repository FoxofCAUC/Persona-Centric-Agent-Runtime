"""Finite lexical experiment. Not a user-intent diagnosis or production policy."""
from dataclasses import asdict, dataclass, replace
import json

from .contracts import (ACTIONS, CATEGORIES, VERSION, ContractError, Context,
    DecisionRecord, Evidence, SituationFrame, fingerprint, keys, reject,
    sequence, text, unique, validate_input)


@dataclass(frozen=True)
class Policy:
    version: str
    explicit: tuple
    categories: tuple
    topics: tuple
    negations: tuple
    quote_pairs: tuple
    eligibility: tuple
    config_hash: str


def parse_policy(raw):
    keys(raw, 'version explicit categories topics negations quote_pairs eligibility scoring')
    if raw['version'] != VERSION or raw['scoring'] != {
            'activation_limit':3, 'per_definition_limit':2, 'total_limit':3, 'base':0}:
        reject('unknown_policy')
    if fingerprint(raw) != 'fe76841f7056e2fae3d04b9768203eacc5e75f4d001dff372de869ef9ae781bd':
        reject('policy_fingerprint_mismatch')
    explicit=[]
    for row in sequence(raw['explicit'], 16):
        if type(row) is not list or len(row)!=3:
            reject()
        ident, action, words=row
        if action.removeprefix('!') not in ACTIONS:
            reject('invalid_action')
        explicit.append((text(ident), action, tuple(text(w) for w in sequence(words,32))))
    unique([r[0] for r in explicit])
    def lexicon(rows, allowed):
        result=[]
        for row in sequence(rows,16):
            if type(row) is not list or len(row)!=2 or row[0] not in allowed:
                reject()
            result.append((row[0],tuple(text(w) for w in sequence(row[1],32))))
        unique([r[0] for r in result]);return tuple(result)
    categories=lexicon(raw['categories'],CATEGORIES)
    topics=lexicon(raw['topics'],('technical','facts'))
    if type(raw['eligibility']) is not dict or set(raw['eligibility'])!=set(CATEGORIES):
        reject()
    eligibility=[]
    for category in CATEGORIES:
        actions=tuple(raw['eligibility'][category])
        if not actions or not set(actions)<=set(ACTIONS) or 'respond' not in actions:
            reject()
        eligibility.append((category,actions))
    quotes=[]
    for pair in sequence(raw['quote_pairs'],8):
        if type(pair) is not list or len(pair)!=2 or any(type(x) is not str or len(x)!=1 for x in pair):
            reject()
        quotes.append(tuple(pair))
    return Policy(VERSION,tuple(explicit),categories,topics,
        tuple(text(x) for x in sequence(raw['negations'],16)),tuple(quotes),tuple(eligibility),fingerprint(raw))


def read_json(path):
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:reject('duplicate_json_key')
            result[k]=v
        return result
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _:reject('invalid_contract'))
    except (json.JSONDecodeError,UnicodeError):
        raise ContractError('invalid_json') from None


def load_policy(path):
    return parse_policy(read_json(path))


def visible_text(value, policy):
    # Mask quoted spans without changing offsets. Unknown/unclosed quotation
    # masks the remainder rather than interpreting quoted instructions.
    chars=list(value); stack=[]; flags=set(); opening=dict(policy.quote_pairs)
    closing=set(opening.values())
    for i,char in enumerate(value):
        if stack:
            chars[i]=' '
            if char==stack[-1]:stack.pop()
            elif char in opening and opening[char]!=char:stack.append(opening[char])
        elif char in opening:
            chars[i]=' ';stack.append(opening[char])
        elif char in closing:
            chars[i]=' ';flags.add('unmatched_quote')
    if stack:flags.add('unclosed_quote')
    return ''.join(chars).translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')),flags


def interpret(members, context, policy):
    categories=set(); requested=set(); forbidden=set(); topics=set(); flags=set(); evidence=[]
    seen=set()
    def capture(rule,member,start,end):
        # One contribution/evidence per rule across all members, no repetition bonus.
        if rule not in seen:
            seen.add(rule);evidence.append(Evidence(rule,member.id,start,end))
    for member in members:
        value,quote_flags=visible_text(member.text,policy);flags.update(quote_flags)
        if 'unmatched_quote' in quote_flags:
            continue
        for ident,action,words in policy.explicit:
            for word in words:
                pos=value.find(word.lower())
                if pos<0:continue
                prefix=value[max(0,pos-4):pos]
                if not action.startswith('!') and any(prefix.endswith(n) for n in policy.negations):
                    flags.add('uncertain_negation');continue
                if action.startswith('!'):forbidden.add(action[1:])
                else:
                    requested.add(action)
                    if action=='support':categories.add('support_request')
                    if action=='acknowledge':categories.add('acknowledgement')
                    if action=='explain':categories.add('information_request')
                capture(ident,member,pos,pos+len(word));break
        for category,words in policy.categories:
            for word in words:
                pos=value.find(word.lower())
                if pos<0:continue
                prefix=value[max(0,pos-4):pos]
                if any(prefix.endswith(n) for n in policy.negations):
                    flags.add('uncertain_negation');continue
                categories.add(category);capture('category_'+category,member,pos,pos+len(word));break
        for topic,words in policy.topics:
            for word in words:
                pos=value.find(word.lower())
                if pos>=0:
                    topics.add(topic);capture('topic_'+topic,member,pos,pos+len(word));break
    if len(requested)>1 or requested&forbidden:
        flags.add('conflicting_explicit_requests')
    if context.reference_gap:categories.add('ambiguous_reference')
    return SituationFrame(tuple(sorted(categories or {'unknown'})),
        tuple(a for a in ACTIONS if a in requested),tuple(a for a in ACTIONS if a in forbidden),
        tuple(sorted(topics)),tuple(sorted(flags)),tuple(evidence),context,'rules')


def fixture_frame(raw, context):
    keys(raw,'categories requested_actions forbidden_actions topics flags')
    allowed={'categories':CATEGORIES,'requested_actions':ACTIONS,'forbidden_actions':ACTIONS,
             'topics':('technical','facts'),'flags':('unclosed_quote','unmatched_quote','uncertain_negation','conflicting_explicit_requests')}
    for field,values in allowed.items():
        seq=sequence(raw[field],16)
        if any(type(v) is not str or v not in values for v in seq):reject()
        unique(seq)
    if not raw['categories']:reject()
    return SituationFrame(tuple(sorted(raw['categories'])),tuple(a for a in ACTIONS if a in raw['requested_actions']),
        tuple(a for a in ACTIONS if a in raw['forbidden_actions']),tuple(sorted(raw['topics'])),
        tuple(sorted(raw['flags'])),(),context,'fixture')


@dataclass(frozen=True)
class Selection:
    snapshot: object
    selected: tuple
    excluded: tuple


def select_dispositions(snapshot, frame):
    selected=[];excluded=[];sources={s.id:s for s in snapshot.sources}
    for definition in sorted(snapshot.definitions,key=lambda d:(-d.priority,d.id)):
        reason=None
        if not definition.confirmed:reason='unconfirmed'
        elif not definition.enabled:reason='disabled'
        elif any(sources[r.id].withdrawn for r in definition.source_refs):reason='source_withdrawn'
        elif any(not sources[r.id].allowed for r in definition.source_refs):reason='source_unavailable'
        else:
            for trigger in definition.triggers:
                values={'category':frame.categories,'topic':frame.topics,
                        'relationship':(frame.context.relationship,)}[trigger.field]
                match=trigger.value in values
                if (trigger.op=='eq' and not match) or (trigger.op=='ne' and match):
                    reason='trigger_not_matched';break
        if reason is None and len(selected)>=3:reason='activation_limit'
        if reason:excluded.append((definition.id,reason))
        else:selected.append(definition)
    return Selection(snapshot,tuple(selected),tuple(excluded))


def eligible(frame,policy):
    candidates={'respond'}
    for category,actions in policy.eligibility:
        if category in frame.categories:candidates.update(actions)
    if 'advice_request' in frame.categories and frame.context.information_gap:candidates.add('ask')
    conflict='conflicting_explicit_requests' in frame.flags
    if conflict or frame.context.reference_gap:candidates.add('clarify')
    if 'challenge' in frame.requested_actions:candidates.add('challenge')
    if frame.context.boundary_source_id:candidates.add('set_boundary')
    if frame.context.resource_unavailable:candidates.add('defer')
    # Trusted context constraints outrank personality, then explicit requests.
    if frame.context.boundary_source_id:candidates={'set_boundary'}
    elif frame.context.resource_unavailable:candidates={'defer'}
    elif conflict or frame.context.reference_gap:candidates={'clarify'}
    elif frame.requested_actions:
        candidates &= set(frame.requested_actions)
    candidates-=set(frame.forbidden_actions)
    if not candidates:reject('no_eligible_action')
    return tuple(a for a in ACTIONS if a in candidates)


def normalized(snapshot):
    value=asdict(snapshot)
    value['sources']=sorted(value['sources'],key=lambda r:r['id'])
    value['definitions']=sorted(value['definitions'],key=lambda r:r['id'])
    for d in value['definitions']:
        d['source_refs']=sorted(d['source_refs'],key=lambda r:r['id'])
        d['triggers']=sorted(d['triggers'],key=lambda r:(r['field'],r['op'],r['value']))
    return value


def decide(frame,dispositions,policy):
    if frame.interpreter_version!=policy.version:reject('unknown_policy')
    actions=eligible(frame,policy);contributions=[];rank=[]
    for action in actions:
        total=0
        for d in dispositions.selected:
            delta=dict(d.preferences).get(action,0)
            contributions.append((d.id,action,delta));total+=delta
        delta=max(-3,min(3,total));rank.append((action,0,delta,delta))
    rank.sort(key=lambda r:(-r[3],ACTIONS.index(r[0])))
    snapshot=dispositions.snapshot
    record=DecisionRecord(fingerprint(normalized(snapshot)),frame.frame_origin,policy.version,policy.config_hash,
        tuple(sorted((s.id,s.version,s.content_hash) for s in snapshot.sources)),
        tuple(sorted((d.id,d.revision) for d in snapshot.definitions)),frame.categories,frame.topics,frame.flags,
        frame.evidence,tuple(d.id for d in dispositions.selected),dispositions.excluded,tuple(rank),
        tuple(contributions),rank[0][0],('permissions_and_sources_first','preserve_facts',
        'preserve_task_completeness','correction_not_overridden','no_side_effects'),
        'unknown_respond' if frame.categories==('unknown',) and rank[0][0]=='respond' else 'selected')
    # Include policy identity and structured observations without any original text.
    trace=fingerprint({'record':record.to_dict(),'policy_hash':policy.config_hash,
                       'frame_hash':fingerprint(asdict(frame))})
    return replace(record,trace_hash=trace)


def run_case(case,mode,policy):
    snapshot=validate_input(case['input'])
    if mode=='fixture':frame=fixture_frame(case['fixture_frame'],snapshot.context)
    elif mode in ('text','neutral'):frame=interpret(snapshot.members,snapshot.context,policy)
    else:reject('invalid_mode')
    if mode=='neutral':snapshot=replace(snapshot,definitions=())
    return decide(frame,select_dispositions(snapshot,frame),policy)
