"""Versioned local configuration and encrypted credentials."""
import copy
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .models import RuntimeErrorBase, identifier
from .security import protect, unprotect

DEFAULTS = {
    "version": 1, "service_port": 8765, "napcat_port": 6399,
    "astrbot_port": 6285, "onebot_port": 6299, "bot_qq": "", "owner_qq": "",
    "model": {"base_url": "", "name": "", "encrypted_key": "", "timeout": 60,
              "max_tokens": 1024, "hourly_limit": 30, "max_input_chars": 24000},
    "interaction": {"cancel_queued": False, "qq_wait_notice": False, "qq_wait_seconds": 15},
}


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigStore:
    def __init__(self, paths):
        self.paths = paths
        self.file = paths.home / "config" / "workbench.json"

    def initialize(self):
        self.paths.initialize()
        if not self.file.exists():
            value = copy.deepcopy(DEFAULTS)
            value.update(instance_id=identifier(), admin_token=protect(secrets.token_urlsafe(32)),
                         bridge_token=protect(secrets.token_urlsafe(32)))
            atomic_json(self.file, value)
        return self.load()

    def load(self):
        value = json.loads(self.file.read_text(encoding="utf-8"))
        value.setdefault("interaction", copy.deepcopy(DEFAULTS["interaction"]))
        self.validate(value)
        return value

    @staticmethod
    def validate(value):
        if value.get("version") != 1:
            raise RuntimeErrorBase("Unsupported workbench configuration version")
        ports = [value.get(key) for key in ("service_port", "napcat_port", "astrbot_port", "onebot_port")]
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 4:
            raise RuntimeErrorBase("Ports must be distinct integers between 1024 and 65535")
        for key in ("bot_qq", "owner_qq"):
            text = value.get(key, "")
            if not isinstance(text, str) or (text and (not text.isascii() or not text.isdecimal())):
                raise RuntimeErrorBase("QQ account fields must contain digits only")
        if value.get("bot_qq") and value["bot_qq"] == value.get("owner_qq"):
            raise RuntimeErrorBase("Bot and owner QQ accounts must be different")
        model = value["model"]
        base = model["base_url"]
        if base:
            url = urlsplit(base)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise RuntimeErrorBase("Model base URL must be HTTP(S), without embedded credentials or query")
            if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise RuntimeErrorBase("Remote model endpoints require HTTPS")
        for key, minimum, maximum in (("timeout", 1, 300), ("max_tokens", 1, 32768),
                                      ("hourly_limit", 1, 10000), ("max_input_chars", 1000, 200000)):
            if type(model.get(key)) is not int or not minimum <= model[key] <= maximum:
                raise RuntimeErrorBase(f"Invalid model limit: {key}")
        if not isinstance(model.get("name"), str):
            raise RuntimeErrorBase("Model name must be a string")
        interaction = value.get("interaction", DEFAULTS["interaction"])
        if not isinstance(interaction, dict) or set(interaction) != set(DEFAULTS["interaction"]):
            raise RuntimeErrorBase("Unknown interaction configuration field")
        if any(type(interaction[key]) is not bool for key in ("cancel_queued", "qq_wait_notice")):
            raise RuntimeErrorBase("Interaction flags must be boolean")
        if type(interaction["qq_wait_seconds"]) is not int or not 5 <= interaction["qq_wait_seconds"] <= 120:
            raise RuntimeErrorBase("QQ wait notice interval must be 5 to 120 seconds")

    def public(self):
        value = self.load()
        value.pop("admin_token")
        value.pop("bridge_token")
        value["model"]["has_key"] = bool(value["model"].pop("encrypted_key"))
        return value

    def update(self, changes):
        value = self.load()
        allowed = {"service_port", "napcat_port", "astrbot_port", "onebot_port", "bot_qq", "owner_qq", "model", "interaction"}
        if not isinstance(changes, dict) or not set(changes) <= allowed:
            raise RuntimeErrorBase("Unknown configuration field")
        for key, change in changes.items():
            if key == "model":
                if not isinstance(change, dict) or not set(change) <= {
                    "base_url", "name", "api_key", "timeout", "max_tokens", "hourly_limit", "max_input_chars"
                }:
                    raise RuntimeErrorBase("Unknown model configuration field")
                change = dict(change)
                if "api_key" in change:
                    key_value = change.pop("api_key")
                    if not isinstance(key_value, str):
                        raise RuntimeErrorBase("API key must be text")
                    value["model"]["encrypted_key"] = protect(key_value) if key_value else ""
                value["model"].update(change)
            elif key == "interaction":
                if not isinstance(change, dict) or not set(change) <= set(DEFAULTS["interaction"]):
                    raise RuntimeErrorBase("Unknown interaction configuration field")
                value["interaction"].update(change)
            else:
                value[key] = change
        self.validate(value)
        atomic_json(self.file, value)
        return self.public()

    def token(self, role="admin"):
        return unprotect(self.load()[role + "_token"])
