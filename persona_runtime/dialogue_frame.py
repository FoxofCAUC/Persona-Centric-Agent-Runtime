"""Source-bound experimental dialogue contracts. No IO on import or mutation."""
from dataclasses import asdict, dataclass
import json

from .archive import encoded, fingerprint

EVIDENCE_VERSION = 'dialogue-evidence-v1'
FRAME_VERSION = 'dialogue-frame-v1'


class DialogueContractError(ValueError):
    """Fixed codes only; never include user text in diagnostics."""


def require(condition, code='dialogue_contract_invalid'):
    if not condition:
        raise DialogueContractError(code)


@dataclass(frozen=True)
class Source:
    kind: str
    id: str
    version: str
    text: str
    role: str = 'data'
    created_at: str = ''

    def __post_init__(self):
        require(all(type(v) is str for v in (self.kind, self.id, self.version, self.text)))
        require(bool(self.kind and self.id and self.version))
        require(self.role in ('user', 'assistant', 'data'))
        require(type(self.created_at) is str)

    @property
    def key(self):
        return self.kind + ':' + self.id

    @property
    def digest(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class DialogueEvidence:
    turn_id: str
    execution_revision: int
    scope_json: str
    accepted_json: str
    current: tuple[Source, ...]
    history: tuple[Source, ...] = ()
    references: tuple[Source, ...] = ()
    states: tuple[Source, ...] = ()
    memories: tuple[Source, ...] = ()
    excluded_json: str = '[]'
    version: str = EVIDENCE_VERSION

    def __post_init__(self):
        require(self.version == EVIDENCE_VERSION, 'dialogue_version_unknown')
        require(type(self.turn_id) is str and bool(self.turn_id))
        require(type(self.execution_revision) is int and self.execution_revision > 0)
        scope = json.loads(self.scope_json)
        require(set(scope) == {'persona_id', 'user_id', 'channel', 'bot', 'peer'})
        require(scope['channel'] in ('desktop', 'qq'))
        require(bool(scope['persona_id'] and scope['user_id']))
        require(bool(self.current))
        for name in ('current', 'history', 'references', 'states', 'memories'):
            require(type(getattr(self, name)) is tuple)
            require(all(type(s) is Source for s in getattr(self, name)))
        require(len(self.keys) == len(self.sources), 'dialogue_duplicate_source')
        require(type(json.loads(self.accepted_json)) is dict)
        require(type(json.loads(self.excluded_json)) is list)

    @property
    def sources(self):
        return self.current + self.history + self.references + self.states + self.memories

    @property
    def keys(self):
        return frozenset(s.key for s in self.sources)

    def document(self):
        return asdict(self)

    @property
    def digest(self):
        return fingerprint(self.document())

    @classmethod
    def load(cls, value):
        value = dict(value)
        for name in ('current', 'history', 'references', 'states', 'memories'):
            value[name] = tuple(Source(**s) for s in value.get(name, ()))
        return cls(**value)

    def check_current(self, fresh):
        require(self.digest == fresh.digest, 'dialogue_evidence_stale')


@dataclass(frozen=True)
class Statement:
    text: str
    source_ids: tuple[str, ...] = ()
    certainty: str = 'explicit'

    def __post_init__(self):
        require(type(self.text) is str and bool(self.text.strip()))
        require(type(self.source_ids) is tuple and all(type(i) is str for i in self.source_ids))
        require(len(set(self.source_ids)) == len(self.source_ids))
        require(self.certainty in ('explicit', 'interpretation', 'unknown'))
        require(self.certainty != 'explicit' or bool(self.source_ids), 'dialogue_evidence_required')


@dataclass(frozen=True)
class DialogueFrame:
    evidence_hash: str
    focus: tuple[Statement, ...] = ()
    requests: tuple[Statement, ...] = ()
    willingness: tuple[Statement, ...] = ()
    progress: tuple[Statement, ...] = ()
    missing_slots: tuple[Statement, ...] = ()
    unresolved: tuple[Statement, ...] = ()
    uncertainty: tuple[Statement, ...] = ()
    version: str = FRAME_VERSION

    def __post_init__(self):
        require(self.version == FRAME_VERSION, 'dialogue_version_unknown')
        for name in self.fields():
            require(type(getattr(self, name)) is tuple)
            require(all(type(s) is Statement for s in getattr(self, name)))

    @staticmethod
    def fields():
        return ('focus', 'requests', 'willingness', 'progress', 'missing_slots', 'unresolved', 'uncertainty')

    def validate(self, evidence):
        require(self.evidence_hash == evidence.digest, 'dialogue_frame_stale')
        for name in self.fields():
            for s in getattr(self, name):
                require(set(s.source_ids) <= evidence.keys, 'dialogue_source_out_of_scope')
        return self

    def document(self):
        return asdict(self)

    @property
    def digest(self):
        return fingerprint(self.document())

    @classmethod
    def load(cls, value):
        value = dict(value)
        for name in cls.fields():
            value[name] = tuple(Statement(s['text'], tuple(s['source_ids']), s['certainty']) for s in value.get(name, ()))
        return cls(**value)


def consumer_input(evidence, frame, consumer):
    """Shared semantic input, not a replacement for any released prompt."""
    require(consumer in ('selection', 'observation', 'expression'))
    frame.validate(evidence)
    return {'consumer': consumer, 'evidence': evidence.document(), 'frame': frame.document(),
            'evidence_hash': evidence.digest, 'frame_hash': frame.digest}
