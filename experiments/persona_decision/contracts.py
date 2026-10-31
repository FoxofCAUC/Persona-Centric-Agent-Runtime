"""Immutable prototype contracts; importing this module performs no I/O."""
from dataclasses import asdict, dataclass
import hashlib
import json

VERSION = 'persona-decision-prototype-v1'
ACTIONS = ('respond', 'acknowledge', 'ask', 'clarify', 'support', 'explain',
           'challenge', 'defer', 'set_boundary')
CATEGORIES = ('acknowledgement', 'support_request', 'celebration',
              'information_request', 'advice_request', 'correction',
              'ambiguous_reference', 'unknown')
AUTHOR_KINDS = ('core', 'character', 'expression', 'relationship')


class ContractError(ValueError):
    """A body-free, fixed error code, never caller-supplied text."""


def reject(code='invalid_contract'):
    raise ContractError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        reject()


def text(value, maximum=200):
    if type(value) is not str or not value.strip() or len(value) > maximum or '\x00' in value:
        reject()
    return value


def boolean(value):
    if type(value) is not bool:
        reject()
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        reject()
    return value


def sequence(value, maximum):
    if type(value) is not list or len(value) > maximum:
        reject()
    return value


def unique(values):
    if len(set(values)) != len(values):
        reject('duplicate_id')


def digest(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        reject()
    return value


@dataclass(frozen=True)
class Scope:
    persona_id: str
    user_id: str
    channel: str
    bot: str
    peer: str


def scope(value):
    keys(value, 'persona_id user_id channel bot peer')
    text(value['persona_id']); text(value['user_id'])
    if value['channel'] == 'desktop':
        if value['bot'] != '' or value['peer'] != '':
            reject()
    elif value['channel'] == 'qq':
        text(value['bot']); text(value['peer'])
    else:
        reject()
    return Scope(**value)


@dataclass(frozen=True)
class Member:
    id: str
    text: str


@dataclass(frozen=True)
class SourceSnapshot:
    id: str
    scope: Scope
    kind: str
    version: str
    content_hash: str
    allowed: bool
    withdrawn: bool


@dataclass(frozen=True)
class SourceRef:
    id: str
    version: str
    content_hash: str


@dataclass(frozen=True)
class Predicate:
    field: str
    op: str
    value: str


@dataclass(frozen=True)
class DispositionDefinition:
    id: str
    persona_id: str
    source_refs: tuple[SourceRef, ...]
    confirmed: bool
    enabled: bool
    triggers: tuple[Predicate, ...]
    preferences: tuple[tuple[str, int], ...]
    priority: int
    revision: int


@dataclass(frozen=True)
class Context:
    relationship: str
    relationship_source_id: str
    information_gap: bool
    reference_gap: bool
    resource_unavailable: bool
    boundary_source_id: str


@dataclass(frozen=True)
class DecisionInput:
    scope: Scope
    members: tuple[Member, ...]
    sources: tuple[SourceSnapshot, ...]
    definitions: tuple[DispositionDefinition, ...]
    context: Context
    policy: str


@dataclass(frozen=True)
class Evidence:
    rule_id: str
    member_id: str
    start: int
    end: int


@dataclass(frozen=True)
class SituationFrame:
    categories: tuple[str, ...]
    requested_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    topics: tuple[str, ...]
    flags: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    context: Context
    frame_origin: str
    interpreter_version: str = VERSION


@dataclass(frozen=True)
class DecisionRecord:
    input_hash: str
    frame_origin: str
    policy_version: str
    policy_hash: str
    source_versions: tuple[tuple[str, str, str], ...]
    definition_revisions: tuple[tuple[str, int], ...]
    categories: tuple[str, ...]
    topics: tuple[str, ...]
    flags: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    activated: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]
    # action, base, clamped contribution, total
    candidates: tuple[tuple[str, int, int, int], ...]
    contributions: tuple[tuple[str, str, int], ...]
    chosen_action: str
    constraints: tuple[str, ...]
    status: str
    trace_hash: str = ''

    def to_dict(self):
        return asdict(self)


def validate_input(raw):
    keys(raw, 'scope members sources definitions context policy')
    if raw['policy'] != VERSION:
        reject('unknown_policy')
    owner = scope(raw['scope'])
    members = []
    for row in sequence(raw['members'], 20):
        keys(row, 'id text')
        members.append(Member(text(row['id']), text(row['text'], 24000)))
    if not members or sum(len(m.text) for m in members) > 24000:
        reject()
    unique([m.id for m in members])
    sources = []
    for row in sequence(raw['sources'], 64):
        keys(row, 'id scope kind version content_hash allowed withdrawn')
        selected = scope(row['scope'])
        if selected != owner:
            reject('scope_mismatch')
        if row['kind'] not in AUTHOR_KINDS:
            reject('unsupported_source_kind')
        sources.append(SourceSnapshot(text(row['id']), selected, row['kind'],
                       text(row['version']), digest(row['content_hash']),
                       boolean(row['allowed']), boolean(row['withdrawn'])))
    unique([s.id for s in sources]); by_id = {s.id: s for s in sources}
    definitions = []
    for row in sequence(raw['definitions'], 32):
        keys(row, 'id persona_id source_refs confirmed enabled triggers preferences priority revision')
        if row['persona_id'] != owner.persona_id:
            reject('scope_mismatch')
        refs = []
        for ref in sequence(row['source_refs'], 8):
            keys(ref, 'id version content_hash')
            source = by_id.get(text(ref['id']))
            if source is None:
                reject('missing_source')
            if text(ref['version']) != source.version:
                reject('source_version_mismatch')
            if digest(ref['content_hash']) != source.content_hash:
                reject('source_fingerprint_mismatch')
            refs.append(SourceRef(**ref))
        if not refs:
            reject('missing_source')
        unique([r.id for r in refs])
        triggers = []
        for rule in sequence(row['triggers'], 8):
            keys(rule, 'field op value')
            values = {'category': CATEGORIES, 'relationship': ('neutral', 'close', 'distant'),
                      'topic': ('technical', 'facts')}
            if rule['field'] not in values or rule['op'] not in ('eq', 'ne') or rule['value'] not in values[rule['field']]:
                reject('invalid_predicate')
            triggers.append(Predicate(**rule))
        prefs = row['preferences']
        if type(prefs) is not dict or not prefs or not set(prefs) <= set(ACTIONS):
            reject('invalid_action')
        preferences = tuple((a, integer(prefs[a], -2, 2)) for a in ACTIONS if a in prefs)
        definitions.append(DispositionDefinition(text(row['id']), owner.persona_id, tuple(refs),
            boolean(row['confirmed']), boolean(row['enabled']), tuple(triggers), preferences,
            integer(row['priority'], 0, 100), integer(row['revision'], 1, 1000000)))
    unique([d.id for d in definitions])
    c = raw['context']
    keys(c, 'relationship relationship_source_id information_gap reference_gap resource_unavailable boundary_source_id')
    if c['relationship'] not in ('neutral', 'close', 'distant'):
        reject()
    for field in ('relationship_source_id', 'boundary_source_id'):
        ident = c[field]
        if ident == '':
            if field == 'relationship_source_id' and c['relationship'] != 'neutral':
                reject('missing_source')
            continue
        source = by_id.get(text(ident))
        if not source or not source.allowed or source.withdrawn:
            reject('unavailable_context_source')
        if field == 'relationship_source_id' and source.kind != 'relationship':
            reject('unavailable_context_source')
    context = Context(c['relationship'], c['relationship_source_id'], boolean(c['information_gap']),
                      boolean(c['reference_gap']), boolean(c['resource_unavailable']), c['boundary_source_id'])
    return DecisionInput(owner, tuple(members), tuple(sources), tuple(definitions), context, raw['policy'])
