"""Pinned, data-free prompt bundle for the released two-speaker import workflow."""
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from .character_embedding import private_directory, safe_path
from .corpus_files import digest
from .import_library import require
from .models import RuntimeErrorBase

VERSION = "pcar-import-prompts-v1"
BUNDLE_PATH = Path(__file__).with_name("import_prompt_assets") / (VERSION + ".json")
BUNDLE_SHA256 = "72aa23bcda484a474c746e8634b0601b8fb8e43f266677e3ec851d5e36e97df6"
MAX_FILE_BYTES = 64 * 1024
RELEASE_MODEL = "deepseek-flash"
OUTPUT_BUDGET = 8192
SELECTION = "released-prompts.json"
CONTRACTS = {
    "profile": ("contract-v2.json", ("version", "model", "extract_system", "merge_system"),
                "3ab23810ce09f01084b7c7dac6fd6f63707aa4136081aa96048e310a3408d03a"),
    "card": ("contract-card.json", ("version", "model", "system"),
             "b73786d71b4e5ddca94c4c72fad9bbc4fdfecea11ddc43b332286e7043023b77"),
}
PRIVATE_AUTHORITIES = ("contract-v2.json", "approval-v2.json", "contract-card.json", "approval-card.json")


def _private(library, name):
    return safe_path(library.root.parent.parent, library.root / name)


def _private_policy(library):
    return any(_private(library, name).exists() for name in PRIVATE_AUTHORITIES)


def _unique(pairs):
    values = dict(pairs)
    require(len(values) == len(pairs), "提示词文件中有重复的字段。")
    return values


def _bundle_bytes(bundle):
    return (json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _verified(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAX_FILE_BYTES, "提示词文件超过容量。")
    try:
        bundle = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique)
        canonical = _bundle_bytes(bundle)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeErrorBase("提示词文件需为有效的 UTF-8 JSON。") from None
    require(digest(canonical) == BUNDLE_SHA256, "提示词与审定发布版不符，请选择完整的发布版提示词文件。")
    return bundle


def _read_bundle(path):
    with path.open("rb") as stream:
        return _verified(stream.read(MAX_FILE_BYTES + 1))


def has_contract(library, kind):
    if _private_policy(library):
        return _private(library, CONTRACTS[kind][0]).is_file()
    return _private(library, SELECTION).is_file() or BUNDLE_PATH.is_file()


def prompt_bundle_status(library):
    info = {"version": VERSION, "label": "双人画像与角色卡 · 发布版", "model": RELEASE_MODEL,
            "sha256": BUNDLE_SHA256, "max_tokens": OUTPUT_BUDGET}
    if _private_policy(library):
        return info | {"source": "private", "active": False, "message": "此实例使用独立的模板和用途审定记录。"}
    local = _private(library, SELECTION)
    source = "file" if local.exists() else "builtin"
    try:
        _read_bundle(local if source == "file" else BUNDLE_PATH)
    except (OSError, ValueError):
        return info | {"source": "invalid", "active": False, "message": "请重新导入审定的发布版提示词文件。"}
    return info | {"source": source, "active": True}


def install_prompt_bundle(library, raw):
    bundle = _verified(raw)
    require(not _private_policy(library), "此实例使用独立的模板和授权。请在新实例导入发布版，或管理原实例的用途授权。")
    local = _private(library, SELECTION)
    if not local.parent.exists():
        private_directory(library.root.parent.parent, local.parent)
    stream = NamedTemporaryFile(dir=local.parent, delete=False)
    temp = Path(stream.name)
    try:
        with stream:
            stream.write(_bundle_bytes(bundle))
        temp.replace(local)
    finally:
        temp.unlink(missing_ok=True)
    return prompt_bundle_status(library)


def import_prompt_file(library, path):
    path = Path(path)
    require(path.is_absolute() and path.suffix.lower() == ".json", "请选择本地发布版提示词 JSON 文件。")
    try:
        require(path.is_file(), "请选择有效的发布版提示词文件。")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise RuntimeErrorBase("无法读取提示词文件。") from None
    return install_prompt_bundle(library, raw)


def contract_source(library, kind):
    """Return exact contract bytes and a release identifier for new, opted-in work."""
    filename, keys, checksum = CONTRACTS[kind]
    if _private_policy(library):
        file = _private(library, filename)
        try:
            return file.read_bytes(), None
        except FileNotFoundError:
            return None, None
    local = _private(library, SELECTION)
    try:
        bundle = _read_bundle(local if local.exists() else BUNDLE_PATH)
    except OSError:
        raise RuntimeErrorBase("发布版提示词文件无法读取。") from None
    # The two reviewed contracts used Windows JSON files. Reproduce that byte
    # representation for existing contract hashes on every operating system.
    ordered = {key: bundle[kind][key] for key in keys}
    raw = (json.dumps(ordered, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode()
    require(digest(raw) == checksum, "发布版提取或成卡模板的指纹不符。")
    return raw, BUNDLE_SHA256


def release_model(model):
    return model | {"name": RELEASE_MODEL, "base_url": model.get("base_url") or "https://api.deepseek.com",
                    "max_tokens": OUTPUT_BUDGET}


def has_release_permission(library, kind, frozen, checksum):
    marker = frozen.get("prompt_release")
    if not marker or marker != BUNDLE_SHA256 or frozen.get("model") != RELEASE_MODEL:
        return False
    raw, current = contract_source(library, kind)
    return current == marker and raw is not None and checksum == digest(raw)
