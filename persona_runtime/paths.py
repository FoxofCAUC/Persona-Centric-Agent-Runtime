"""Single source of truth for runtime storage locations."""

from dataclasses import dataclass
import os
from pathlib import Path

class _ConfiguredHome:
    """Compatibility for historical tools; never an implicit storage location."""
    def _path(self):return RuntimePaths.from_environment().home
    def __truediv__(self,part):return self._path()/part
    def __fspath__(self):return str(self._path())
    def __str__(self):return str(self._path())
    def __getattr__(self,name):return getattr(self._path(),name)

DEFAULT_HOME = _ConfiguredHome()


@dataclass(frozen=True)
class RuntimePaths:
    home: Path

    def __post_init__(self) -> None:
        if not self.home.is_absolute():
            raise ValueError("PERSONA_RUNTIME_HOME must be an absolute path")

    @classmethod
    def from_environment(cls,explicit=None) -> "RuntimePaths":
        from .installation import resolve_home
        return cls(resolve_home(explicit))

    def directories(self) -> dict[str, Path]:
        return {
            "home": self.home,
            "config": self.home / "config",
            "database": self.home / "data" / "database",
            "personas": self.home / "data" / "personas",
            "events": self.home / "data" / "events",
            "memory": self.home / "data" / "memory",
            "knowledge": self.home / "data" / "knowledge",
            "logs": self.home / "logs",
            "backups": self.home / "backups",
            "exports": self.home / "exports",
            "downloads": self.home / "downloads",
            "models": self.home / "models",
            "uv_cache": self.home / "cache" / "uv",
            "pip_cache": self.home / "cache" / "pip",
            "pycache": self.home / "cache" / "pycache",
            "temp": self.home / "tmp",
            "venv": self.home / ".venv",
            "runtime": self.home / "runtime",
            "components": self.home / "components",
        }

    def initialize(self) -> None:
        # Never silently fall back to the system drive.
        if not Path(self.home.anchor).exists():
            raise OSError(f"Storage drive is unavailable: {self.home.anchor}")
        for name, directory in self.directories().items():
            if name != "venv":
                directory.mkdir(parents=True, exist_ok=True)
