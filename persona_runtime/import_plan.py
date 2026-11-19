"""Prepare local dyadic evidence windows with stable, decoded-source coordinates."""
from collections import Counter, defaultdict
from itertools import groupby
import json
import re
import unicodedata

from .archive import fingerprint
from .corpus_files import CorpusError, digest
from .import_library import encode, require
from .import_markdown import FENCE, PLACEHOLDER, SYSTEM, assign, learning_view, parse, partition_rows


VERSION = "dyadic-windows-v2"
ROLES = ("A", "B")
WIRE_FIELDS = ("id", "speaker", "start", "end", "text", "kind")
FORWARD = re.compile(r"^(?:【.*】|\[(?:合并转发|聊天记录|转发)(?:[:： ].*)?\]|(?:合并转发|转发内容)[:：].*)$")
ATTACHMENT = re.compile(r"^\[(?:附件|视频号|小程序|名片|位置共享)(?:[:： ].*)?\]$")


def _sections(record, moect):
    """Keep literal runs distinct even when an intervening quote is omitted."""
    original = record["original"]
    if record["speaker"].casefold() in SYSTEM:
        return [("system", 0, len(original))]
    spans = []
    fence = ""
    forward = quoted = attachment_tail = False
    start = 0
    for line in original.splitlines(keepends=True):
        end = start + len(line)
        clean = line.strip()
        marker = FENCE.match(line)
        if forward or FORWARD.fullmatch(clean) or (moect and clean.startswith("【")):
            kind = "forward"
            forward = True
        elif fence:
            kind = "code"
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence)
                    and not line[marker.end():].strip()):
                fence = ""
        elif marker:
            kind = "code"
            fence = marker[1]
        elif clean.startswith(">") or quoted:
            kind = "quote"
            quoted = moect or bool(clean)
        elif attachment_tail or PLACEHOLDER.fullmatch(clean) or ATTACHMENT.fullmatch(clean):
            kind = "attachment"
            attachment_tail = moect
        else:
            kind = "body"
        if spans and spans[-1][0] == kind:
            spans[-1] = kind, spans[-1][1], end
        else:
            spans.append((kind, start, end))
        start = end
    return spans


def _personal_rows(parsed):
    rows = []
    for row in parsed["rows"]:
        original = row["original"]
        blocks = _sections(row, parsed["format"] == "Moect Markdown")
        bodies, offsets = [], []
        position = 0
        for kind, start, end in blocks:
            if kind == "body":
                body = original[start:end].rstrip("\r\n")
                if body.strip():
                    bodies.append(body)
                    offsets.append((position, position + len(body), row["start"] + start))
                    position += len(body) + 1
        rows.append(row | {"text": "\n".join(bodies), "body_spans": offsets, "sections": [
            {"kind": kind, "start": row["start"] + start, "end": row["start"] + end}
            for kind, start, end in blocks
        ]})
    return parsed | {"rows": rows}


def parse_source(raw):
    return _personal_rows(parse(raw))


def _boundary(index, redactions, *, to_raw):
    """Translate a boundary only if it does not cut through a replacement."""
    delta = 0
    for source_start, source_end, view_start, view_end in redactions:
        a, b, x, y = ((view_start, view_end, source_start, source_end) if to_raw else
                      (source_start, source_end, view_start, view_end))
        if index <= a:
            break
        if index < b:
            raise CorpusError("引用区间切开了脱敏标记。")
        delta = y - b
    return index + delta


def source_span(record, start, end):
    require(type(start) is int and type(end) is int and record["start"] <= start < end <= record["end"],
            "引用超出已发送的原话区间。")
    absolute = record["origin"]
    return (absolute + _boundary(start, record["redactions"], to_raw=True),
            absolute + _boundary(end, record["redactions"], to_raw=True))


def _wire_record(record):
    return {k: record[k] for k in WIRE_FIELDS}


def wire_records(batch):
    """Only opaque coordinates, role flags and the approved redacted text leave the host."""
    return [_wire_record(record) for record in batch["records"]]


def _sources(row):
    document = row["id"]
    require(digest(row["raw"]) == document, "文件与导入指纹不一致。")
    mapping = json.loads(row["mapping"]) if isinstance(row["mapping"], str) else row["mapping"]
    overrides = json.loads(row["overrides"]) if isinstance(row["overrides"], str) else row["overrides"]
    old = parse(row["raw"])
    assigned = assign(_personal_rows(old), mapping, overrides)
    data = partition_rows(assigned, row["secret"], document)
    fragments = {f["id"]: f["group_id"] for f in data["fragments"]}
    reserved = set(row.get("reserved_ids", []))
    archived = row.get("view")
    if archived is not None:
        previous_mapping = row.get("legacy_mapping", mapping)
        previous_overrides = row.get("legacy_overrides", overrides)
        previous = assign(old,
                          json.loads(previous_mapping) if isinstance(previous_mapping, str) else previous_mapping,
                          json.loads(previous_overrides) if isinstance(previous_overrides, str) else previous_overrides)
        legacy = partition_rows(previous, row["secret"], document)
        expected = json.loads(archived) if isinstance(archived, str) else archived
        require(learning_view(legacy) == expected, "存档学习范围与当前归属不一致。")
        reserved.update(m["message_id"] for m in legacy["messages"]
                        if m["split"] in ("dev_review", "final_holdout"))
    blocked = {fragments[m["fragment_id"]] for m in data["messages"]
               if m["message_id"] in reserved or m["split"] in ("dev_review", "final_holdout")}
    reserved.update(m["message_id"] for m in data["messages"] if fragments[m["fragment_id"]] in blocked)

    records, sections = [], Counter()
    for message, item in zip(assigned, data["messages"], strict=True):
        sections.update(b["kind"] for b in message["sections"])
        fragment = item["fragment_id"]
        group = fragments[fragment]
        if item["split"] != "learn" or not item["target_candidate"] or group in blocked:
            continue
        masks = [(m["source_start"], m["source_end"], m["view_start"], m["view_end"])
                 for m in item["redactions"]]
        for a, b, raw_at in message["body_spans"]:
            start = _boundary(a, masks, to_raw=False)
            end = _boundary(b, masks, to_raw=False)
            records.append({"id": message["id"], "speaker": item["role"], "start": start, "end": end,
                            "text": item["text"][start:end], "kind": "core", "group": group,
                            "fragment": fragment, "date": message["date"], "origin": raw_at - a,
                            "redactions": masks})
    require({r["speaker"] for r in records} == set(ROLES), "学习分区中需要有双方的有效原话。")
    view_digest = fingerprint([(r["id"], r["start"], r["end"], digest(r["text"].encode()), r["group"])
                               for r in records])
    reserved_ids = sorted(reserved, key=int)
    source_id = fingerprint([VERSION, document, mapping, overrides, view_digest, reserved_ids])
    return source_id, records, dict(sections), reserved_ids


def _wire_size(record):
    return len(encode(_wire_record(record)))


def _cut(record, start, end):
    offset = record["start"]
    return record | {"start": start, "end": end, "text": record["text"][start-offset:end-offset]}


def _parts(record, capacity):
    if _wire_size(record) + 2 <= capacity:
        yield record
        return
    start = record["start"]
    while start < record["end"]:
        low, high = start, min(start + capacity, record["end"])
        while low < high:
            middle = (low + high + 1) // 2
            if _wire_size(_cut(record, start, middle)) + 2 <= capacity:
                low = middle
            else:
                high = middle - 1
        for _, _, mask_start, mask_end in record["redactions"]:
            if mask_start < low < mask_end:
                low = mask_start
                break
        require(low > start, "容量不足以容纳一个完整字符或脱敏标记。")
        yield _cut(record, start, low)
        start = low


def _pack(records, capacity):
    current, size = [], 1
    for _, session in groupby(records, key=lambda r: r["fragment"]):
        parts = [(p, _wire_size(p) + 1) for r in session for p in _parts(r, capacity)]
        total = sum(cost for _, cost in parts)
        if current and size + total > capacity:
            yield current
            current, size = [], 1
        for record, cost in parts:
            if current and size + cost > capacity:
                prior = current[-1]
                yield current
                current, size = [], 1
                if prior["fragment"] == record["fragment"]:
                    context = prior | {"kind": "context"}
                    extra = _wire_size(context) + 1
                    if extra <= capacity // 4 and size + extra + cost <= capacity:
                        current.append(context)
                        size += extra
            current.append(record)
            size += cost
    if current:
        yield current


def _date_bounds(values):
    values = [v for v in values if v]
    if not values:
        return None, None
    def chronological(value):
        return value.replace("T", " ")
    return min(values, key=chronological), max(values, key=chronological)


def _choose(candidates, limit, selected):
    if selected is not None:
        require(isinstance(selected, list) and len(selected) <= limit and
                all(isinstance(i, str) for i in selected) and len(set(selected)) == len(selected) and
                set(selected) <= {b["id"] for b in candidates}, "所选批次不属于本次计划。")
        return set(selected)
    if len(candidates) <= limit:
        return {b["id"] for b in candidates}
    picked, groups = set(), set()
    for n in range(limit):
        left, right = n * len(candidates) // limit, (n+1) * len(candidates) // limit
        bucket = candidates[left:right]
        index = max(range(len(bucket)), key=lambda i: (
            len(bucket[i]["groups"] - groups), -abs(2*i - len(bucket) + 1)))
        winner = bucket[index]
        picked.add(winner["id"])
        groups.update(winner["groups"])
    return picked


def _stats(complete, source, role):
    lengths, endings, occurrences, first = [], Counter(), Counter(), {}
    burst = 0
    by_id = defaultdict(list)
    for record in source:
        if record["speaker"] == role and record["id"] in complete:
            by_id[record["id"]].append(record)
    previous = None
    for ident, segments in by_id.items():
        text = "\n".join(r["text"] for r in segments).strip()
        lengths.append(len(text))
        if text and unicodedata.category(text[-1]).startswith("P"):
            endings[text[-1]] += 1
        if previous and previous["fragment"] == segments[0]["fragment"] and int(previous["id"])+1 == int(ident):
            burst += 1
        previous = segments[-1]
        if 1 <= len(text) <= 120:
            occurrences[text] += 1
            first.setdefault(text, ident)
    return {"messages": len(lengths), "mean_chars": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "ending_punctuation": dict(endings), "burst_messages": burst,
            "common_messages": [{"id": first[t], "count": c} for t, c in occurrences.most_common(8) if c > 1]}


def build_plan(row, capacity, *, limit=6, selection=None):
    """Build bounded windows. Capacity counts Unicode characters in the JSON records array."""
    require(type(capacity) is int and capacity > 0 and type(limit) is int and 1 <= limit <= 6,
            "分批容量或批次数量无效。")
    require(selection is None or isinstance(selection, dict) and set(selection) == set(ROLES),
            "请分别提供 A、B 的批次选择。")
    require(row.get("status") != "revoked", "来源已撤回。")
    source_id, records, sections, reserved_ids = _sources(row)
    packed = list(_pack(records, capacity))
    batches, choices, roles = [], {}, {}
    available_lengths = Counter()
    for r in records:
        available_lengths[r["speaker"], r["id"]] += r["end"] - r["start"]
    for role in ROLES:
        candidates = []
        for index, window in enumerate(packed):
            targets = [r for r in window if r["speaker"] == role and r["kind"] == "core"]
            if not targets:
                continue
            first_date, last_date = _date_bounds(r["date"] for r in targets)
            batch_id = fingerprint([source_id, capacity, role, index])
            candidates.append({"id": batch_id, "ordinal": len(candidates)+1, "messages": len({r["id"] for r in targets}),
                               "first_date": first_date, "last_date": last_date,
                               "groups": {r["group"] for r in targets}, "index": index})
        selected = _choose(candidates, limit, selection[role] if selection is not None else None)
        choices[role] = []
        lengths, groups = Counter(), set()
        for candidate in candidates:
            active = candidate["id"] in selected
            choices[role].append({k: v for k, v in candidate.items() if k not in ("groups", "index")}
                                 | {"selected": active})
            if active:
                window = packed[candidate["index"]]
                batches.append({"id": candidate["id"], "role": role, "ordinal": candidate["ordinal"],
                                "records": window})
                groups.update(candidate["groups"])
                for r in window:
                    if r["speaker"] == role and r["kind"] == "core":
                        lengths[r["id"]] += r["end"] - r["start"]
        complete = {ident for ident, length in lengths.items() if length == available_lengths[role, ident]}
        first_date, last_date = _date_bounds(r["date"] for r in records if r["speaker"] == role)
        roles[role] = {"messages": sum(1 for owner, _ in available_lengths if owner == role),
                       "first_date": first_date, "last_date": last_date,
                       "eligible": len(candidates), "selected": len(selected),
                       "selected_messages": len(lengths), "selected_groups": len(groups),
                       "stats": _stats(complete, records, role)}
    require(batches, "请至少选择一个有效批次。")
    return {"version": VERSION, "source_id": source_id, "roles": roles, "sections": sections,
            "choices": choices, "batches": batches, "reserved_ids": reserved_ids}
