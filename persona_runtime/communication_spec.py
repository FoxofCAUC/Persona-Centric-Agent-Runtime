"""Experimental communication specifications; never an automatic decision policy."""
from dataclasses import asdict, dataclass
import json

from .archive import fingerprint
from .dialogue_frame import Statement, require

VERSION = 'communication-spec-v1'


@dataclass(frozen=True)
class CommunicativeSpec:
    frame_hash: str
    focus: str
    stance: str
    obligations: tuple[Statement, ...] = ()
    optional_moves: tuple[str, ...] = ()
    avoid: tuple[Statement, ...] = ()
    disposition_refs: tuple[str, ...] = ()
    version: str = VERSION

    def __post_init__(self):
        require(self.version == VERSION, 'dialogue_version_unknown')
        require(type(self.focus) is str and type(self.stance) is str)
        for name in ('obligations', 'avoid'):
            require(type(getattr(self, name)) is tuple)
            require(all(type(s) is Statement and s.certainty == 'explicit' for s in getattr(self, name)))
        for name in ('optional_moves', 'disposition_refs'):
            require(type(getattr(self, name)) is tuple)
            require(all(type(v) is str and bool(v) for v in getattr(self, name)))

    def validate(self, evidence, frame):
        frame.validate(evidence)
        require(self.frame_hash == frame.digest, 'communication_spec_stale')
        for s in self.obligations + self.avoid:
            require(set(s.source_ids) <= evidence.keys, 'dialogue_source_out_of_scope')
        accepted = json.loads(evidence.accepted_json)
        require(set(self.disposition_refs) <= set(accepted.get('projection_refs', [])), 'dialogue_projection_out_of_scope')
        return self

    def document(self):
        return asdict(self)

    @property
    def digest(self):
        return fingerprint(self.document())

    @classmethod
    def load(cls, value):
        value = dict(value)
        for name in ('obligations', 'avoid'):
            value[name] = tuple(Statement(s['text'], tuple(s['source_ids']), s['certainty']) for s in value.get(name, ()))
        for name in ('optional_moves', 'disposition_refs'):
            value[name] = tuple(value.get(name, ()))
        return cls(**value)
