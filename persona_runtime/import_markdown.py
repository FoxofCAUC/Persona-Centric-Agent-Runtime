"""Parse local dyadic exports. Record positions refer to the decoded source."""
from datetime import datetime, timezone, timedelta
from collections import Counter
import re

from .corpus_files import CorpusError, inspect_bytes, digest
from .corpus_moect import MAX_RECORDS
from .corpus_views import build_dataset

VERSION = "dyadic-md-v1"
DATE = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?"
MOECT = re.compile(r"^\[(" + DATE + r")\] (.+)$")
HEADING = re.compile(r"^#{2,4}[ \t]+(.+?)\s*$")
DATED = re.compile(r"^(.*?)\s+\[(" + DATE + r")\]$")
FENCE = re.compile(r"^ {0,3}(\x60{3,}|~{3,})")
SYSTEM = {"系统", "系统消息", "system", "通知"}
PLACEHOLDER = re.compile(r"^\[(?:图片|动画表情|表情|语音|视频|文件|位置|链接|转账|红包|系统消息)(?:[:： ].*)?\]$")


def valid_message_date(value):
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    # fromisoformat accepts overflow minutes in a UTC offset and carries them into hours.
    offset = re.search(r"[+-]\d{2}:?(\d{2})$", value)
    return offset is None or int(offset[1]) < 60


def parse(raw):
    info = inspect_bytes(raw, ".md")
    encoding = info["encoding"]
    text = raw.decode("utf-16" if encoding.startswith("utf-16") else encoding)
    lines = text.splitlines(keepends=True)
    moect = len(lines) >= 2 and lines[1].strip() == "由 Moect 导出"
    rows = []
    offset = 0
    record = None
    fence = ""
    blank = True

    def finish(end):
        if record is None:
            return
        source = text[record["start"]:end].rstrip("\r\n")
        visible = []
        for line in source.splitlines(keepends=True):
            clean = line.strip()
            if (clean.startswith(">") or FENCE.match(line)
                    or (moect and clean.startswith("【")) or PLACEHOLDER.fullmatch(clean)):
                break
            visible.append(line)
        body = "".join(visible).rstrip("\r\n")
        record.update(end=record["start"] + len(source), text=body, original=source)
        rows.append(record)

    for line_number,line in enumerate(lines,1):
        value = line.rstrip("\r\n")
        match = None
        if not fence:
            match = MOECT.fullmatch(value) if moect and blank else None
            if not moect:
                match = HEADING.fullmatch(value)
        if match:
            finish(offset)
            if len(rows) >= MAX_RECORDS:
                raise CorpusError("聊天记录超过导入容量。")
            if moect:
                date, name = match.groups()
            else:
                name = match[1].strip()
                dated = DATED.fullmatch(name)
                name, date = dated.groups() if dated else (name, None)
            if len(name) > 200 or not name.strip():
                raise CorpusError("说话人标题无效。")
            record = dict(id=str(len(rows)+1), speaker=name.strip(), date=date,
                          start=offset+len(line), line=line_number)
            if date is not None and not valid_message_date(date):
                record.update(date=None, raw_date=date)
        else:
            marker = FENCE.match(value)
            if marker:
                token = marker[1]
                if not fence:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence) and not value[marker.end():].strip():
                    fence = ""
        offset += len(line)
        blank = not value.strip()
    finish(len(text))
    if not rows:
        raise CorpusError("未识别出消息标题。通用 Markdown 请用二级标题标记每条消息的说话人。")
    counts = Counter(r["speaker"] for r in rows if r["speaker"].casefold() not in SYSTEM)
    return {"format": "Moect Markdown" if moect else "Markdown", "rows": rows, "counts": dict(counts)}


def assign(parsed, mapping, overrides):
    rows = parsed["rows"]
    if set(mapping) != set(parsed["counts"]) or set(mapping.values()) - {"A", "B", "-"}:
        raise CorpusError("请将发言者归到 A、B 或排除。")
    if not isinstance(overrides, dict) or any(not k.isdecimal() or str(int(k)) != k
            or not 1 <= int(k) <= len(rows) or v not in ("A","B","-") for k,v in overrides.items()):
        raise CorpusError("逐条归属修订无效。")
    def role(r):
        if r["speaker"].casefold() in SYSTEM:
            return "-"
        return overrides.get(r["id"], mapping[r["speaker"]])
    result = [r | {"role":role(r)} for r in rows]
    if {r["role"] for r in result if r["text"] and r["role"] != "-"} != {"A", "B"}:
        raise CorpusError("两位人物都需要有可归属的发言。")
    return result


def partition_rows(rows, key, namespace):
    sources = []
    last_epoch = 0
    for i,r in enumerate(rows):
        if r["date"]:
            try:
                local = datetime.fromisoformat(r["date"])
                if local.tzinfo is None:
                    local = local.replace(tzinfo=timezone(timedelta(hours=8)))
                last_epoch = int(local.timestamp())
            except ValueError:
                raise CorpusError("消息时间无效。") from None
        elif i and i % 64 == 0:
            # Partition an undated series in order. This key is never displayed as a timestamp.
            last_epoch += 1801
        sources.append(dict(id=r["id"], ordinal=i, role={"A":"self","B":"peer","-":"system"}[r["role"]],
                            target_candidate=r["role"] != "-" and bool(r["text"].strip()),
                            text=r["text"], text_hash=digest(r["text"].encode()), kind="text",
                            created_epoch=last_epoch))
    names = {r["speaker"] for r in rows}
    identities = {name: {"source_id":"", "names":[name]} for name in names}
    return build_dataset(sources, [], identities, key=key, namespace=namespace, parse_id=namespace)


def learning_rows(rows, key, namespace):
    return learning_view(partition_rows(rows, key, namespace))


def learning_view(view):
    return [dict(id=r["message_id"], speaker=r["role"], text=r["text"], group=r["fragment_id"])
            for r in view["messages"] if r["split"] == "learn" and r["target_candidate"]]
