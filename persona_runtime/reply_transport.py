"""Deterministic QQ rendering and lossless bounded transport, without model repair."""
import re
from .qq_text import plain_qq_text
from .reply_batch import ReplySchemaError, validate_reply


def render_parts(text, content_kind="chat", limit=450):
    # A code-kind body is already data even without Markdown fences.
    fenced = bool(re.search(r'(?m)^\s*(`{3,}|~{3,})', text))
    rendered = text if content_kind == 'code' and not fenced else plain_qq_text(text)
    if not rendered.strip():
        raise ReplySchemaError("qq_plain_text_empty")
    # Fence contents become plain text; preserve each original code line atomically.
    code_lines = set()
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            fence = None if fence == marker[1][0] else marker[1][0]
        elif fence or content_kind == "code":
            if line:
                code_lines.add(line)
    protected = [(m.start(), m.end()) for m in re.finditer(
        r"https?://[^\s<>（）“”，。！？；]+|[vV]?[+-]?\d+(?:[.,:/+%=-]\d+)*(?:[eE][+-]?\d+)?(?:[-+][A-Za-z][A-Za-z0-9.-]*)?", rendered)]
    for line in code_lines:
        start = 0
        while (start := rendered.find(line, start)) >= 0:
            protected.append((start, start+len(line)))
            start += len(line)
    if any(end-start > limit for start, end in protected):
        raise ReplySchemaError("qq_indivisible_content_exceeds_limit")
    parts = []
    start = 0
    while start < len(rendered):
        end = min(start+limit, len(rendered))
        if end < len(rendered):
            boundaries = [p for p in range(start+1, end+1)
                          if not any(a < p < b for a, b in protected)]
            if not boundaries:
                raise ReplySchemaError("qq_indivisible_content_exceeds_limit")
            preferred = [p for p in boundaries if rendered[p-1] in "\n。！？!?；; "]
            end = max(preferred or boundaries)
        parts.append(rendered[start:end])
        start = end
    assert "".join(parts) == rendered
    return rendered, parts


def preview(raw, mode):
    batch = validate_reply(raw, mode)
    messages = []
    for index, item in enumerate(batch.segments, 1):
        rendered, parts = render_parts(item.text, item.content_kind)
        messages.append({"semantic_index": index, "text": item.text, "rendered": rendered,
                         "parts": parts, "wait_ms": item.wait_ms, "content_kind": item.content_kind,
                         "cancel_if_new_input": item.cancel_if_new_input})
    return {"schema_version": batch.schema_version, "mode": batch.mode, "messages": messages,
            "fallback_reason": batch.fallback_reason, "raw_fingerprint": batch.raw_fingerprint,
            "planned_wait_ms": sum(s.wait_ms for s in batch.segments)}
