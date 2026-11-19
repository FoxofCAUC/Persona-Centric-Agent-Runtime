"""Admission of v2 import observations against one frozen, role-scoped request."""
from collections import defaultdict

from .archive import fingerprint
from .import_library import FIELDS

KINDS = ("self_report", "pattern", "inference")


class InvalidObservation(ValueError):
    """An internal reason code safe to show without quoting private input."""


def check(condition, reason):
    if not condition:
        raise InvalidObservation(reason)


def _text(value):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 2000:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _projection(role, batch_id, field, kind, value, scope, evidence, counter=()):
    groups = sorted({ref["group"] for ref in evidence})
    explicit_field = field in ("initial_background", "introduction", "values_and_boundaries")
    return {
        "id": fingerprint([batch_id, role, field, kind, value, scope, evidence, counter]),
        "role": role, "batch": batch_id, "field": field, "kind": kind,
        "value": value.strip(), "scope": scope.strip(), "evidence": evidence,
        "support_groups": groups,
        "needs_review": bool(counter) or kind == "inference" or
            (explicit_field and kind != "self_report") or (field == "core_traits" and len(groups) < 2),
    }


def _admit_items(items, project):
    accepted, rejected = {}, []
    for index, raw in enumerate(items):
        try:
            candidate = project(raw)
        except InvalidObservation as exc:
            rejected.append({"index": index, "reason": str(exc)})
        else:
            accepted.setdefault(candidate["id"], candidate)
    return list(accepted.values()), rejected


def _envelope(result, role, batch_id, collection):
    check(isinstance(result, dict) and set(result) == {"target", "batch", collection}, "envelope")
    check(result["target"] == role and result["batch"] == batch_id, "identity")
    check(isinstance(result[collection], list), "collection")
    return result[collection]


def _reference(ref, records):
    from .import_plan import source_span

    check(isinstance(ref, dict) and set(ref) in ({"id", "start", "end"},
                                                  {"id", "start", "end", "quote"}), "ref_shape")
    ident, start, end = ref["id"], ref["start"], ref["end"]
    check(isinstance(ident, str) and type(start) is int and type(end) is int and start < end, "ref_span")
    # A source message may have several parts in this batch. One transmitted
    # target core interval must contain the complete quote.
    matches = [r for r in records.get(ident, ()) if r["start"] <= start < end <= r["end"]]
    check(len(matches) == 1, "ref_outside_target_core")
    record = matches[0]
    quote = record["text"][start - record["start"]:end - record["start"]]
    check(bool(quote.strip()) and ("quote" not in ref or ref["quote"] == quote), "ref_quote")
    try:
        raw_start, raw_end = source_span(record, start, end)
    except ValueError:
        raise InvalidObservation("ref_source_span") from None
    return {"id": ident, "start": start, "end": end, "quote": quote, "speaker": record["speaker"],
            "raw_start": raw_start, "raw_end": raw_end, "group": record["group"],
            "fragment": record["fragment"], "date": record.get("date")}


def admit_extraction(result, batch):
    role, batch_id = batch["role"], batch["id"]
    items = _envelope(result, role, batch_id, "observations")
    records = defaultdict(list)
    for record in batch["records"]:
        if record["speaker"] == role and record["kind"] == "core":
            records[record["id"]].append(record)

    def project(item):
        check(isinstance(item, dict) and set(item) == {"field", "kind", "value", "scope", "evidence"},
              "observation_shape")
        check(item["field"] in FIELDS and item["kind"] in KINDS, "observation_type")
        check(_text(item["value"]) and _text(item["scope"]), "observation_text")
        check(isinstance(item["evidence"], list) and item["evidence"], "missing_support")
        refs = {}
        for ref in item["evidence"]:
            verified = _reference(ref, records)
            refs[(verified["id"], verified["start"], verified["end"])] = verified
        return _projection(role, batch_id, item["field"], item["kind"], item["value"], item["scope"],
                           list(refs.values()))

    return _admit_items(items, project)


def admit_merge(result, role, batch_id, observations):
    items = _envelope(result, role, batch_id, "claims")
    available = {o["id"]: o for o in observations if o["role"] == role}

    def project(item):
        check(isinstance(item, dict) and set(item) == {"field", "value", "scope", "support", "counter"},
              "claim_shape")
        check(item["field"] in FIELDS and _text(item["value"]) and _text(item["scope"]), "claim_fields")
        groups = {}
        for key in ("support", "counter"):
            ids = item[key]
            check(isinstance(ids, list) and all(isinstance(i, str) for i in ids), "claim_ids")
            check(len(ids) == len(set(ids)), "duplicate_observation")
            check(all(i in available and available[i]["field"] == item["field"] for i in ids),
                  "foreign_observation")
            groups[key] = [dict(ref, observation_id=i) for i in ids for ref in available[i]["evidence"]]
        support, counter = item["support"], item["counter"]
        check(support and not set(support).intersection(counter), "conflicting_sources")
        kinds = {available[i]["kind"] for i in support}
        kind = "inference" if "inference" in kinds else "pattern" if "pattern" in kinds else "self_report"
        claim = _projection(role, batch_id, item["field"], kind, item["value"], item["scope"],
                            groups["support"], groups["counter"])
        return claim | {"support": support, "counter": counter, "counter_evidence": groups["counter"]}

    return _admit_items(items, project)
