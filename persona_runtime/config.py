"""Versioned local configuration and encrypted credentials."""
import copy
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from .models import RuntimeErrorBase, identifier
from .security import protect, unprotect

SPEECH_BASELINE_MODEL = "deepseek-v4-pro"
SELECTOR_TIMEOUT_MAX = 240

SELECTOR_DEFAULTS = {"enabled": False, "base_url": "", "name": "", "encrypted_key": "",
                     "timeout": 60, "max_tokens": 512, "max_input_chars": 48000,
                     "hourly_limit": 0, "daily_limit": 0}

DEFAULTS = {
    "call_limits": {"enabled":False,"hourly":0,"daily":0},
    "organization_parallel_tasks":2,
    "qq_backend": "napcat", "snowluma_port": 5099,
    "version": 3, "character_selector": SELECTOR_DEFAULTS, "service_port": 8765, "napcat_port": 6399,
    "runtime": {"revision":1,"components":{"service":True,"astrbot":False,"napcat":False},"qq_persona_id":""},
    "astrbot_port": 6285, "onebot_port": 6299, "bot_qq": "", "owner_qq": "",
    "model": {"base_url": "", "name": SPEECH_BASELINE_MODEL, "encrypted_key": "", "timeout": 60,
              "max_tokens": 1024, "hourly_limit": 0, "max_input_chars": 24000},
    "interaction": {"cancel_queued": False, "qq_wait_notice": False, "qq_wait_seconds": 15},
    "organization": {"reuse_chat": False, "hourly_limit": 0, "daily_limit": 0,
                     "daily_enabled": True,
                     "model": {"base_url": "", "name": "", "encrypted_key": "", "timeout": 60,
                               "max_tokens": 2048, "hourly_limit": 0, "max_input_chars": 24000}},
}


def replace_closed(source,destination):
    """Windows readers may hold a non-share-delete handle briefly."""
    import time
    for delay in (0,.025,.05,.1,.2,.4):
        if delay:time.sleep(delay)
        try:os.replace(source,destination);return
        except PermissionError as error:
            if getattr(error,'winerror',None) not in (5,32,33) or delay==.4:raise


def atomic_json(path: Path, value, *, durable=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("w",encoding="utf-8") as stream:
            stream.write(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        replace_closed(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def same_credential(original,current):
    if original==current:return True
    if not original or not current:return False
    import hmac
    try:return hmac.compare_digest(unprotect(original).encode(),unprotect(current).encode())
    except (OSError,ValueError):return False


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
        value=self.load()
        raw=json.loads(self.file.read_text(encoding='utf-8'))
        if raw.get('version') in (1,2):
            backup=self.paths.home/'backups'/('workbench-before-v3-'+identifier()+'.dpapi.json')
            atomic_json(backup,{'format':'windows-dpapi','original':protect(self.file.read_text(encoding='utf-8'))})
            atomic_json(self.file,value)
        return value

    def load(self):
        value = json.loads(self.file.read_text(encoding="utf-8"))
        value.setdefault("qq_backend", "napcat")
        value.setdefault("snowluma_port", 5099)
        value.setdefault("interaction", copy.deepcopy(DEFAULTS["interaction"]))
        value.setdefault("organization", copy.deepcopy(DEFAULTS["organization"]))
        value.setdefault("organization_parallel_tasks",2)
        if value.get("version") in (1,2):
            value["version"] = 3
            value.setdefault('runtime',copy.deepcopy(DEFAULTS['runtime']))
        value.setdefault("character_selector", copy.deepcopy(SELECTOR_DEFAULTS))
        if "call_limits" not in value:
            value["call_limits"]=copy.deepcopy(DEFAULTS["call_limits"])
            for section,old in ((value["model"],{"hourly_limit":300}),
                    (value["organization"],{"hourly_limit":60,"daily_limit":240}),
                    (value["organization"]["model"],{"hourly_limit":60}),
                    (value["character_selector"],{"hourly_limit":300,"daily_limit":2000})):
                if all(section.get(k)==v for k,v in old.items()):
                    section.update(dict.fromkeys(old,0))
        self.validate(value)
        record=self.quota_override()
        if record.get('status')=='active' and record.get('purpose')=='development' and record.get('instance_id',value['instance_id'])==value['instance_id']:
            value['model']['hourly_limit']=0
            value['organization']['hourly_limit']=value['organization']['daily_limit']=0
            value['character_selector']['hourly_limit']=value['character_selector']['daily_limit']=0
        return value

    def quota_override(self):
        journal=self.paths.home/'runtime/acceptance-quotas.json'
        return json.loads(journal.read_text(encoding='utf-8')) if journal.exists() else {}

    @staticmethod
    def validate(value):
        limits=value.get("call_limits",DEFAULTS["call_limits"])
        if (not isinstance(limits,dict) or set(limits)-{'enabled','hourly','daily','saved_automatic'} or not {'enabled','hourly','daily'}<=limits.keys()
                or type(limits['enabled']) is not bool or any(type(limits[k]) is not int or not 0<=limits[k]<=1000000 for k in ('hourly','daily'))
                or limits['enabled'] and not (limits['hourly'] or limits['daily'])):
            raise RuntimeErrorBase("invalid_call_limits")
        saved=limits.get('saved_automatic',{})
        if (not isinstance(saved,dict) or set(saved)-{'project_hourly','background_hourly','background_daily','selector_hourly','selector_daily'}
                or any(type(v) is not int or not 0<=v<=10000 for v in saved.values())):
            raise RuntimeErrorBase("invalid_call_limits")
        if value.get("version") != 3:
            raise RuntimeErrorBase("Unsupported workbench configuration version")
        if value.get('qq_backend', 'napcat') not in ('napcat', 'snowluma'):
            raise RuntimeErrorBase('QQ connection must be napcat or snowluma')
        if type(value.get('snowluma_port', 5099)) is not int or not 1024 <= value.get('snowluma_port', 5099) <= 65535:
            raise RuntimeErrorBase('Invalid SnowLuma management port')
        runtime=value.get('runtime')
        if (not isinstance(runtime,dict) or set(runtime)!=set(DEFAULTS['runtime']) or
                type(runtime['revision']) is not int or runtime['revision']<1 or
                not isinstance(runtime['qq_persona_id'],str) or
                not isinstance(runtime['components'],dict) or set(runtime['components'])!={'service','astrbot','napcat'} or
                any(type(v) is not bool for v in runtime['components'].values())):
            raise RuntimeErrorBase('Invalid application runtime settings')
        selector = value.get("character_selector")
        if not isinstance(selector, dict) or set(selector) != set(SELECTOR_DEFAULTS) or type(selector['enabled']) is not bool:
            raise RuntimeErrorBase('Invalid character selector configuration')
        for key in ('base_url','name','encrypted_key'):
            if not isinstance(selector[key],str):raise RuntimeErrorBase('Invalid character selector text')
        if selector['base_url']:
            url=urlsplit(selector['base_url'])
            if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment or (url.scheme=='http' and url.hostname not in ('localhost','127.0.0.1','::1')):
                raise RuntimeErrorBase('Invalid character selector endpoint')
        for field,low,high in (('timeout',1,SELECTOR_TIMEOUT_MAX),('max_tokens',64,2048),('max_input_chars',1000,64000),('hourly_limit',0,10000),('daily_limit',0,10000)):
            if type(selector[field]) is not int or not low<=selector[field]<=high:raise RuntimeErrorBase('Invalid character selector limit')
        ports = [value.get(key) for key in ("service_port", "napcat_port", "astrbot_port", "onebot_port")]
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or len(set(ports)) != 4:
            raise RuntimeErrorBase("Ports must be distinct integers between 1024 and 65535")
        if value.get('qq_backend', 'napcat') == 'snowluma' and value.get('snowluma_port', 5099) in ports:
            raise RuntimeErrorBase('SnowLuma management port conflicts with a PCAR port')
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
                                      ("hourly_limit", 0, 10000), ("max_input_chars", 1000, 200000)):
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
        org = value["organization"]
        research_account = value.get('private_research_account', 'main')
        if research_account not in ('main', 'organization', 'native_web'):
            raise RuntimeErrorBase('invalid_private_research_account')
        if type(value.get('organization_parallel_tasks',2)) is not int or value.get('organization_parallel_tasks',2) not in (1,2):
            raise RuntimeErrorBase('invalid_organization_parallelism')
        if not isinstance(org, dict) or set(org) != set(DEFAULTS["organization"]) or any(type(org[k]) is not bool for k in ("reuse_chat", "daily_enabled")):
            raise RuntimeErrorBase("Invalid organization configuration")
        if research_account == 'organization' and org['reuse_chat']:
            raise RuntimeErrorBase('invalid_private_research_account')
        for field in ("hourly_limit", "daily_limit"):
            if type(org[field]) is not int or not 0 <= org[field] <= 10000:
                raise RuntimeErrorBase("Invalid organization budget")
        # Reuse the endpoint and numeric validation without recursive organization validation.
        background = org["model"]
        if not isinstance(background, dict) or set(background) != set(DEFAULTS["model"]):
            raise RuntimeErrorBase("Invalid organization model")
        url = urlsplit(background["base_url"])
        if background["base_url"] and (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment
                or (url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise RuntimeErrorBase("Invalid organization endpoint")
        if not isinstance(background["name"], str) or not isinstance(background["encrypted_key"], str):
            raise RuntimeErrorBase("Invalid organization model text")
        for field, low, high in (("timeout", 1, 300), ("max_tokens", 1, 32768), ("hourly_limit", 0, 10000), ("max_input_chars", 1000, 200000)):
            if type(background[field]) is not int or not low <= background[field] <= high:
                raise RuntimeErrorBase("Invalid organization model limit")
        if research_account == 'native_web' or 'native_web_model' in value:
            from .deepseek_native_contract import official_endpoint
            native = value.get('native_web_model')
            try:
                if not isinstance(native, dict) or set(native) != set(DEFAULTS['model']):
                    raise ValueError()
                official_endpoint(native)
                if not isinstance(native['encrypted_key'], str) or not native['encrypted_key']:
                    raise ValueError()
                for field, low, high in (('timeout',1,300),('max_tokens',1,32768),
                                         ('hourly_limit',0,10000),('max_input_chars',1000,200000)):
                    if type(native[field]) is not int or not low <= native[field] <= high:
                        raise ValueError()
            except ValueError:
                raise RuntimeErrorBase('invalid_native_web_model') from None

    def public(self):
        value = self.load()
        value["character_selector"]["has_key"] = bool(value["character_selector"].pop("encrypted_key"))
        value.pop("admin_token")
        value.pop("bridge_token")
        value["model"]["has_key"] = bool(value["model"].pop("encrypted_key"))
        value["organization"]["model"]["has_key"] = bool(value["organization"]["model"].pop("encrypted_key"))
        if 'native_web_model' in value:
            value['native_web_model']['has_key'] = bool(value['native_web_model'].pop('encrypted_key'))
        return value

    def update(self, changes):
        from .instance import InstanceLock
        lock=InstanceLock(self.paths.home/'runtime/config-write.lock')
        try:return self._update(changes)
        finally:lock.close()

    def _update(self, changes):
        value = self.load()
        allowed = {"call_limits","organization_parallel_tasks","service_port", "napcat_port", "astrbot_port", "onebot_port", "bot_qq", "owner_qq", "model", "interaction", "organization", "character_selector", "qq_backend", "snowluma_port", "private_research_account", "native_web_model"}
        if not isinstance(changes, dict) or not set(changes) <= allowed:
            raise RuntimeErrorBase("Unknown configuration field")
        for key, change in changes.items():
            if key in ("model", "native_web_model"):
                if not isinstance(change, dict) or not set(change) <= {
                    "base_url", "name", "api_key", "timeout", "max_tokens", "hourly_limit", "max_input_chars"
                }:
                    raise RuntimeErrorBase("Unknown model configuration field")
                change = dict(change)
                if "api_key" in change:
                    key_value = change.pop("api_key")
                    if not isinstance(key_value, str):
                        raise RuntimeErrorBase("API key must be text")
                    change["encrypted_key"] = protect(key_value) if key_value else ""
                value.setdefault(key, copy.deepcopy(DEFAULTS['model'])).update(change)
            elif key == "character_selector":
                if not isinstance(change,dict) or not set(change) <= (set(SELECTOR_DEFAULTS)-{'encrypted_key'})|{'api_key'}:
                    raise RuntimeErrorBase('Unknown character selector field')
                change=copy.deepcopy(change)
                if 'api_key' in change:
                    secret=change.pop('api_key')
                    if not isinstance(secret,str):raise RuntimeErrorBase('Invalid character selector key')
                    change['encrypted_key']=protect(secret) if secret else ''
                value[key].update(change)
            elif key == "organization":
                if not isinstance(change, dict) or not set(change) <= set(DEFAULTS["organization"]):
                    raise RuntimeErrorBase("Unknown organization field")
                change = copy.deepcopy(change)
                model = change.pop("model", None)
                value[key].update(change)
                if model is not None:
                    if not isinstance(model, dict) or not set(model) <= (set(DEFAULTS["model"])-{"encrypted_key"}) | {"api_key"}:
                        raise RuntimeErrorBase("Unknown organization model field")
                    if "api_key" in model:
                        secret = model.pop("api_key")
                        if not isinstance(secret, str):
                            raise RuntimeErrorBase("API key must be text")
                        value[key]["model"]["encrypted_key"] = protect(secret) if secret else ""
                    value[key]["model"].update(model)
            elif key == "interaction":
                if not isinstance(change, dict) or not set(change) <= set(DEFAULTS["interaction"]):
                    raise RuntimeErrorBase("Unknown interaction configuration field")
                value["interaction"].update(change)
            else:
                value[key] = change
        self.validate(value)
        atomic_json(self.file, value)
        return self.public()

    def copy_selector_model(self):
        value=self.load()
        for key in ('base_url','name','encrypted_key'):
            value['character_selector'][key]=value['model'][key]
        self.validate(value)
        atomic_json(self.file,value)
        return self.public()

    def token(self, role="admin"):
        return unprotect(self.load()[role + "_token"])
