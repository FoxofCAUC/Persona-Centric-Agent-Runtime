"""Channel-independent data contracts and input validation."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import uuid4


class RuntimeErrorBase(ValueError):
    """An actionable runtime input or state error."""


CATEGORIES = ("user_fact", "preference", "shared_experience", "relationship", "world_knowledge")
ROLES = ("user", "persona", "note")
LOCAL_USER_ID = "00000000-0000-4000-8000-000000000001"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def identifier() -> str:
    return str(uuid4())


def require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeErrorBase(f"{field} must be a non-empty string")
    return value


@dataclass(frozen=True)
class PersonaSpec:
    format_version: int
    name: str
    introduction: str
    core_traits: list[str]
    expression_style: str
    values_and_boundaries: list[str]
    initial_background: str

    @classmethod
    def from_dict(cls, data: object) -> "PersonaSpec":
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise RuntimeErrorBase("Persona JSON must contain exactly the documented template fields")
        if type(data["format_version"]) is not int or data["format_version"] != 1:
            raise RuntimeErrorBase("Unsupported persona format_version; expected 1")
        for field in ("name", "introduction", "expression_style", "initial_background"):
            require_text(data[field], field)
        for field in ("core_traits", "values_and_boundaries"):
            if not isinstance(data[field], list) or not data[field]:
                raise RuntimeErrorBase(f"{field} must be a non-empty list of strings")
            for value in data[field]:
                require_text(value, field)
        return cls(**data)

    def to_dict(self) -> dict:
        data = asdict(self)
        self.from_dict(data)
        return data


def generic_persona() -> PersonaSpec:
    return PersonaSpec(
        format_version=1,
        name="通用人格模板",
        introduction="用于验证人格与记忆持久化的可编辑模板。",
        core_traits=["坦诚", "尊重他人", "保持好奇"],
        expression_style="使用自然、清楚的中文，根据交流内容调整语气。",
        values_and_boundaries=["不虚构共同经历", "区分已知事实与不确定信息"],
        initial_background="尚未设定具体个人背景，与用户的关系从实际互动开始记录。",
    )
