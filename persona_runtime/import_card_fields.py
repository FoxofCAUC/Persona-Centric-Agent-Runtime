"""Compose structured drafts from role-local, traceable observations."""
from collections import Counter
from copy import deepcopy
from datetime import date
import json
import math
import re

from .character import FIELDS as CHARACTER_FIELDS, empty_character, validate_character
from .import_library import FIELDS, require
from .scene_voice import SCENES

ROLES = ("A", "B")
LIST_SPEC = ("core_traits", "values_and_boundaries")
TEXT_PATHS = {"relationship", "common", "jev.instruction",
              *("scenes." + key for key in SCENES), *("jev.criteria." + key for key in SCENES)}
FACT_PATHS = {"character." + group + "." + field for group, fields in CHARACTER_FIELDS.items()
              for field in fields if (group, field) != ("age", "policy")}
CARD_PATHS = TEXT_PATHS | FACT_PATHS | {"character.examples", "character.lorebook", *("spec." + k for k in FIELDS)}


def proof(claim):
    return {key: deepcopy(claim[key]) for key in ("id", "field", "value", "scope", "evidence", "counter_evidence")
            if key in claim}


def blank_card(draft):
    scenes = dict.fromkeys(SCENES, "")
    result = {"spec": deepcopy(draft["spec"]), "relationship": draft["relationship"],
              "character": empty_character(), "common": "", "scenes": scenes,
              "jev": {"instruction": "", "criteria": scenes.copy()}, "sources": {}, "issues": []}
    for claim in draft["claims"]:
        if claim.get("decision") == "accept" and claim["field"] not in draft.get("edited_fields", []):
            result["sources"].setdefault("spec." + claim["field"], []).append(proof(claim))
    return result


def build_source(drafts, stats=None):
    payload, lookup = {}, {}
    for role in ROLES:
        draft = drafts[role]
        refs, quotes, observations, ids = {}, {}, [], {}
        for claim in draft["claims"]:
            if claim.get("decision") == "reject":
                continue
            oid = role + str(len(observations) + 1)
            ids[oid] = claim
            item = {"id": oid, **{k: claim[k] for k in ("field", "kind", "value", "scope")}}
            for attr, dest in (("evidence", "e"), ("counter_evidence", "c")):
                used = []
                for ref in claim.get(attr, []):
                    require(ref["speaker"] == role, "观察来源与人物不符。")
                    key = (ref["id"], ref["start"], ref["end"])
                    if key not in refs:
                        qid = role + "q" + str(len(quotes) + 1)
                        refs[key] = qid
                        quotes[qid] = [ref.get("date"), ref["quote"]]
                        ids[qid] = ref
                    qid = refs[key]
                    require(ids[qid]["quote"] == ref["quote"], "同一证据区间的正文不一致。")
                    used.append(qid)
                if used:
                    item[dest] = list(dict.fromkeys(used))
            observations.append(item)
        require(observations, "需要两位人物各有可使用的画像观察。")
        manual = {k: draft["spec"][k] for k in draft.get("edited_fields", []) if k in FIELDS}
        if draft["relationship"]:
            manual["relationship"] = draft["relationship"]
        measures = (stats or {}).get(role, {})
        payload[role] = {"observations": observations, "quotes": quotes, "manual": manual,
                         "stats": {k: measures[k] for k in ("messages", "mean_chars", "ending_punctuation",
                                                          "burst_messages") if k in measures}}
        lookup[role] = ids
    return payload, lookup


def _at(card, path):
    node = card
    parts = path.split(".")
    for key in parts[:-1]:
        node = node[key]
    return node, parts[-1]


def _text(value, maximum=6000, *, blank=False):
    require(isinstance(value, str) and (blank or value.strip()) and len(value) <= maximum
            and "\x00" not in value, "文字字段无效。")
    value.encode("utf-8")


def _spec_field(field, value, *, blank=False):
    if field in LIST_SPEC:
        require(isinstance(value, list) and len(value) <= 50 and (blank or value), "列表字段无效。")
        for item in value:
            _text(item, 2000)
    else:
        _text(value, blank=blank)


def _direct_fact(path, value, support):
    explicit = [ref["quote"] for c in support if c["kind"] == "self_report" for ref in c["evidence"]]
    if path.endswith((".height_cm", ".weight_kg", ".fixed_age")):
        require(type(value) in (int, float) and math.isfinite(value), "数值字段无效。")
        unit = {".height_cm": ("cm|厘米|公分", "m|米"), ".weight_kg": ("kg|公斤|千克",),
                ".fixed_age": ("岁|周岁",)}["." + path.rsplit(".", 1)[1]]
        scales = [(value, unit[0])]
        if path.endswith(".height_cm"):
            scales.append((value / 100, unit[1]))
        patterns = [rf"(?<![\d.]){re.escape(format(n, 'g'))}\s*(?:{u})(?![a-zA-Z])" for n, u in scales]
        require(any(re.search(p, q, re.I) for q in explicit for p in patterns), "数值缺少有单位的本人陈述。")
    elif path.endswith((".birthday", ".birth_date")):
        require(isinstance(value, str), "日期无效。")
        d = date.fromisoformat(("2000-" if path.endswith(".birthday") else "") + value)
        localized = (str(d.year) + "年" if path.endswith(".birth_date") else "") + f"{d.month}月{d.day}日"
        require(any(value in q or localized in q for q in explicit), "日期缺少本人陈述。")
    elif path.startswith(("character.identity.", "character.appearance.", "character.biography.")):
        values = value if isinstance(value, list) else [value]
        require(all(isinstance(v, str) and any(v in q for q in explicit) for v in values),
                "基础事实缺少明确的本人原话。")


def _examples(value, ids, support):
    require(isinstance(value, list) and len(value) <= 50, "表达样例列表无效。")
    examples = []
    for qid in dict.fromkeys(value):
        ref = ids.get(qid)
        owner = next((c for c in support if ref in c["evidence"]), None)
        require(owner and ref and "quote" in ref, "样例没有关联到引用它的观察。")
        examples.append({"id": qid, "scenario": owner["scope"][:200], "keywords": [],
                         "text": ref["quote"], "enabled": True})
    return examples


def _apply(card, draft, role_ids, path, entry):
    require(path in CARD_PATHS, "未知角色卡字段。")
    require(isinstance(entry, dict) and set(entry) == {"value", "sources"}, "字段结构无效。")
    value, sources = entry["value"], entry["sources"]
    if value is None or value == "" or value == []:
        return
    require(isinstance(sources, list) and sources and all(isinstance(s, str) for s in sources),
            "缺少可核对的观察。")
    support = [role_ids.get(s) for s in dict.fromkeys(sources)]
    require(all(c and "field" in c for c in support), "观察编号不属于当前人物。")
    if any(c.get("counter_evidence") for c in support):
        card["issues"].append({"field":path, "reason":"支持材料含有反例，请核对后填写。"})
        card["sources"][path] = [proof(c) for c in support]
        return
    if path.startswith("spec."):
        field = path.split(".")[1]
        if field in draft.get("edited_fields", []):
            return
        _spec_field(field, value)
    elif path == "relationship" and draft["relationship"]:
        return
    elif path in TEXT_PATHS:
        _text(value)
    else:
        updated = deepcopy(card["character"])
        if path == "character.examples":
            updated["examples"] = _examples(value, role_ids, support)
        elif path == "character.lorebook":
            require(isinstance(value, list), "资料条目无效。")
            for i, row in enumerate(value):
                require(isinstance(row, dict) and set(row) == {"title","text"}, "资料条目字段无效。")
                updated["lorebook"].append({"id": str(i+1), **row, "category":"biography", "keywords":[],
                    "priority":0, "enabled":False, "visibility":"creator_only", "dependencies":[]})
        else:
            _direct_fact(path, value, support)
            _, group, field = path.split(".")
            updated[group][field] = value
            if group == "age" and field in ("birth_date", "fixed_age"):
                updated["age"]["policy"] = "calendar" if field == "birth_date" else "fixed"
        card["character"] = validate_character(updated)
        card["sources"][path] = [proof(c) for c in support]
        return
    node, key = _at(card, path)
    node[key] = value
    card["sources"][path] = [proof(c) for c in support]


def _response_slots(fields, prefix=""):
    for part, entry in fields.items():
        path = prefix + part
        if not prefix:
            path = {"scenes_fallback":"scenes.fallback", "examples":"character.examples"}.get(path, path)
        if path in CARD_PATHS:
            yield path, entry
        elif isinstance(entry, dict) and any(p.startswith(path + ".") for p in CARD_PATHS):
            yield from _response_slots(entry, path + ".")
        else:
            yield path, entry


def _linked_examples(value, ids):
    if not isinstance(value, list) or not value or not all(isinstance(q, str) for q in value):
        return None
    refs = [ids.get(q) for q in value]
    if not all(isinstance(ref, dict) and "quote" in ref for ref in refs):
        return None
    owners = [oid for oid, observation in ids.items()
              if "evidence" in observation and not observation.get("counter_evidence")
              and any(ref in observation["evidence"] for ref in refs)]
    if all(any(ref in ids[oid]["evidence"] for oid in owners) for ref in refs):
        return {"value":value, "sources":owners}
    return None


def _proposal_value(path, raw, canon):
    if path not in CARD_PATHS:
        return False
    if path.startswith("spec."):
        _spec_field(path.split(".")[1], raw)
    elif path in TEXT_PATHS:
        _text(raw)
    elif path in FACT_PATHS:
        character = deepcopy(canon)
        _, group, key = path.split(".")
        character[group][key] = raw
        if key in ("fixed_age", "birth_date"):
            character["age"]["policy"] = "fixed" if key == "fixed_age" else "calendar"
        validate_character(character)
    else:
        return False
    return True


def _keep_manual_proposal(card, path, raw):
    """An unreferenced model string can be reviewed separately from published card data."""
    if not _proposal_value(path, raw, card["character"]):
        return False
    card["issues"].append({"field":path, "reason":"回执未附本字段的观察编号，供人工核对。",
                           "proposal":deepcopy(raw)})
    return True


def _fill_from_observations(card, draft, filled):
    for field in FIELDS:
        path = "spec." + field
        if path in filled or field in draft.get("edited_fields", []):
            continue
        observations = [c for c in draft["claims"] if c["field"] == field
                        and c.get("decision") != "reject" and not c.get("counter_evidence")]
        if not observations:
            continue
        values = list(dict.fromkeys(c["value"] for c in observations))
        value = values if field in LIST_SPEC else "\n".join(values)
        try:
            _spec_field(field, value)
        except ValueError:
            card["issues"].append({"field":path,"reason":"原观察合计超出字段容量，请选取后填写。"})
            continue
        card["spec"][field] = value
        card["sources"][path] = [proof(c) for c in observations]


def admit_cards(result, drafts, lookup):
    require(isinstance(result, dict) and set(result) <= set(ROLES) and result,
            "成卡回执的角色标识无效。")
    cards = {}
    for role in ROLES:
        draft = drafts[role]
        card = blank_card(draft)
        cards[role] = card
        fields = result.get(role)
        if not isinstance(fields, dict) or not fields:
            _fill_from_observations(card, draft, set())
            card["issues"].append({"field": role, "reason":"角色内容缺失，可手工补齐。"})
            continue
        slots = list(_response_slots(fields))
        counts = Counter(key for key, _ in slots)
        filled = set()
        for path, entry in slots:
            if counts[path] != 1:
                card["issues"].append({"field":path, "reason":"同一字段出现多种表示，请核对。"})
                continue
            try:
                if path == "character.examples" and isinstance(entry, list):
                    entry = _linked_examples(entry, lookup[role]) or entry
                if not isinstance(entry, dict) and entry not in (None, "", []):
                    if _keep_manual_proposal(card, path, entry):
                        continue
                if path in ("character.age.fixed_age","character.age.birth_date") and all(
                        any(p == other and isinstance(it, dict) and it.get("value") is not None for p,it in slots)
                        for other in ("character.age.fixed_age","character.age.birth_date")):
                    card["issues"].append({"field":path, "reason":"固定年龄和出生日期需核对年龄口径。"})
                    continue
                _apply(card, draft, lookup[role], path, entry)
                if path in card["sources"] and not any(i["field"] == path for i in card["issues"]):
                    filled.add(path)
            except (TypeError, ValueError, OverflowError, UnicodeError):
                card["issues"].append({"field": path if path in CARD_PATHS else role, "reason":"字段或出处未通过校验。"})
        _fill_from_observations(card, draft, filled)
    return cards


# Version the local projection separately from the model input and first receipt.
WORKFLOW_POLICY = "role-card-prefill-v1"
GENERATED_TEXT = TEXT_PATHS - {"relationship"}
_CONFLICT_REASONS = {"支持材料含有反例，请核对后填写。", "同一字段出现多种表示，请核对。",
                     "固定年龄和出生日期需核对年龄口径。"}


def unresolved_conflicts(card):
    """Also recognize conflict reasons saved before the explicit severity field."""
    return [issue for issue in card.get("issues", []) if issue.get("severity") == "conflict"
            or issue.get("reason") in _CONFLICT_REASONS]


def include_profile_conflicts(card, draft, edited_fields=()):
    conflicts = {}
    for claim in draft["claims"]:
        if (claim.get("counter_evidence") and claim.get("decision") != "reject"
                and claim["field"] not in draft.get("edited_fields", [])):
            path = "spec." + claim["field"]
            conflicts.setdefault(path, []).append(proof(claim))
    severe = {issue["field"]:issue for issue in unresolved_conflicts(card)}
    for path, observations in conflicts.items():
        if path in edited_fields:
            continue
        if path not in severe:
            severe[path] = {"field":path, "severity":"conflict", "reason":"同一结论有支持原话和相反证据。"}
            card["issues"].append(severe[path])
        severe[path]["observations"] = observations


def initial_workbench_card(draft):
    card = blank_card(draft)
    _fill_from_observations(card, draft, set())
    include_profile_conflicts(card, draft)
    return card


def _corroborates_birth(fixed, birth, ids):
    """Prefer a verified calendar date when a dated self-report agrees with it."""
    if not all(isinstance(e, dict) and set(e) == {"value", "sources"}
               and isinstance(e["sources"], list) and e["sources"] for e in (fixed, birth)):
        return False
    try:
        birthday = date.fromisoformat(birth["value"])
        birth_sources = [ids[i] for i in birth["sources"]]
        fixed_sources = [ids[i] for i in fixed["sources"]]
        if any(c.get("counter_evidence") for c in birth_sources + fixed_sources):
            return False
        _direct_fact("character.age.birth_date", birth["value"], birth_sources)
        ages = []
        for claim in fixed_sources:
            for ref in claim["evidence"]:
                try:
                    _direct_fact("character.age.fixed_age", fixed["value"], [dict(claim, evidence=[ref])])
                except ValueError:
                    continue
                when = date.fromisoformat(ref["date"][:10])
                ages.append(when.year - birthday.year - ((when.month, when.day) < (birthday.month, birthday.day)))
        return type(fixed["value"]) is int and bool(ages) and all(age == fixed["value"] for age in ages)
    except (TypeError, KeyError, IndexError, ValueError):
        return False


def normalize_workbench_reply(value, lookup=None):
    """Accept equivalent nested and flat field paths. Keep declared references strict."""
    if not isinstance(value, dict):
        return value
    normalized = {}
    for role, fields in value.items():
        if not isinstance(fields, dict):
            normalized[role] = fields
            continue
        by_path = {}
        for path, slot in _response_slots(fields):
            if isinstance(slot, dict) and (set(slot) == {"value"} or
                    set(slot) == {"value", "sources"} and slot["sources"] == []):
                slot = slot["value"]
            if slot is None or slot == "" or slot == []:
                continue
            by_path.setdefault(path, []).append(slot)
        normalized[role] = {}
        for path, items in by_path.items():
            # Distinct alternatives are resolved by the user; agreement retains
            # any references supplied on either spelling of the path.
            first = items[0].get("value") if isinstance(items[0], dict) else items[0]
            if any((s.get("value") if isinstance(s, dict) else s) != first for s in items):
                continue
            structured = [s for s in items if isinstance(s, dict)]
            if len(structured) > 1 and all(set(s) == {"value","sources"} and isinstance(s["sources"], list)
                                           and all(isinstance(i, str) for i in s["sources"]) for s in structured):
                normalized[role][path] = {"value":first, "sources": list(dict.fromkeys(
                    i for slot in structured for i in slot["sources"]))}
            else:
                normalized[role][path] = structured[0] if structured else first
        fixed = normalized[role].get("character.age.fixed_age")
        birth = normalized[role].get("character.age.birth_date")
        if lookup and role in lookup and _corroborates_birth(fixed, birth, lookup[role]):
            normalized[role].pop("character.age.fixed_age")
    return normalized


def _conflict_material(issue, slots, ids):
    paths = {issue["field"]}
    if issue["reason"] == "固定年龄和出生日期需核对年龄口径。":
        paths = {"character.age.fixed_age", "character.age.birth_date"}
    observations = {c["id"]: c for c in issue.get("observations", [])}
    alternatives = []
    for path, entry in slots:
        if path not in paths:
            continue
        if isinstance(entry, dict):
            if set(entry) - {"value", "sources"} or "value" not in entry:
                continue
            value = entry["value"]
            refs = entry.get("sources", [])
        else:
            value, refs = entry, []
        if not isinstance(refs,list) or not all(isinstance(i,str) and i in ids and "field" in ids[i] for i in refs):
            continue
        if type(value) in (int, float) and len(str(value)) > 64:
            continue
        if isinstance(value,str) and len(value) > 2000:
            continue
        if isinstance(value,list) and (len(value)>12 or any(not isinstance(v,str) or len(v)>200 for v in value)):
            continue
        try:
            if not _proposal_value(path, value, empty_character()):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        option = {"field":path, "value":value}
        if option not in alternatives and len(alternatives)<8:
            alternatives.append(option)
        if refs:
            support = [ids[i] for i in refs]
            if path in FACT_PATHS:
                try:
                    _direct_fact(path, value, support)
                except (ValueError, TypeError, OverflowError):
                    continue
            observations.update({c["id"]:proof(c) for c in support})
    if observations:
        issue["observations"] = list(observations.values())
    if alternatives:
        issue["alternatives"] = alternatives


def workflow_cards(cards, drafts, response, lookup):
    """Apply release defaults after strict receipt validation, before manual overlays."""
    for role in ROLES:
        card = cards[role]
        draft = drafts[role]
        include_profile_conflicts(card, draft)
        fields = response.get(role)
        foreign = set()
        distinct = {}
        slots = list(_response_slots(fields)) if isinstance(fields, dict) else []
        if slots:
            for path, entry in slots:
                value = entry.get("value") if isinstance(entry, dict) else entry
                if path in CARD_PATHS and value is not None and value != "" and value != []:
                    previous = distinct.setdefault(path, value)
                    if previous != value and not any(i["field"] == path and i.get("severity") == "conflict"
                                                     for i in card["issues"]):
                        card["issues"].append({"field":path, "severity":"conflict",
                                               "reason":"同一字段出现互相不符的内容。"})
                if path in CARD_PATHS and isinstance(entry, dict) and isinstance(entry.get("sources"), list):
                    if any(isinstance(oid, str) and re.fullmatch(r"[AB]q?\d+", oid)
                           and not oid.startswith(role) for oid in entry["sources"]):
                        foreign.add(path)
        for issue in card["issues"]:
            if issue.get("reason") in _CONFLICT_REASONS or issue["field"] in foreign:
                issue["severity"] = "conflict"
                if issue["field"] in foreign:
                    issue["reason"] = "字段引用了另一位说话人的材料。"
        conflicts = unresolved_conflicts(card)
        for issue in conflicts:
            _conflict_material(issue, slots, lookup[role])
        blocked = {issue["field"] for issue in conflicts}
        # Keep only prose whose subject is implicated by unresolved evidence out
        # of automatic prefill. An ambiguous measurement does not own scene routing.
        if blocked & {"spec.core_traits", "spec.expression_style"}:
            blocked.update(("common", "scenes.daily", "scenes.fallback"))
        if blocked & {"relationship", "spec.values_and_boundaries"}:
            blocked.add("scenes.relationship")
        kept = []
        generated = []
        for issue in card["issues"]:
            path = issue["field"]
            if path in GENERATED_TEXT and path not in blocked and isinstance(issue.get("proposal"), str):
                node, key = _at(card, path)
                if not node[key]:
                    node[key] = issue["proposal"]
                    generated.append(path)
                    continue
            kept.append(issue)
        card["issues"] = kept
        if generated:
            card["generated_fields"] = sorted(set(generated))
    return cards


def validate_edit(card, previous, resolved_fields=()):
    require(isinstance(card, dict) and set(card) == set(previous), "角色卡表单无效。")
    require(isinstance(card["spec"], dict) and set(card["spec"]) == set(previous["spec"])
            and type(card["spec"]["format_version"]) is int and card["spec"]["format_version"] == 1,
            "人设字段无效。")
    for key in ("name", *FIELDS):
        _spec_field(key, card["spec"][key], blank=True)
    require(isinstance(card["scenes"], dict) and set(card["scenes"]) == set(SCENES)
            and isinstance(card["jev"], dict) and set(card["jev"]) == {"instruction", "criteria"}
            and isinstance(card["jev"]["criteria"], dict) and set(card["jev"]["criteria"]) == set(SCENES),
            "场景或判别字段无效。")
    for path in TEXT_PATHS:
        node, key = _at(card, path)
        _text(node[key], blank=True)
    validate_character(card["character"])
    require(isinstance(resolved_fields, (list, tuple)) and all(isinstance(path, str) for path in resolved_fields),
            "冲突字段选择无效。")
    allowed = {issue["field"] for issue in unresolved_conflicts(previous)}
    require(set(resolved_fields) <= allowed and set(resolved_fields) <= CARD_PATHS,
            "请刷新后处理当前版本的冲突。")
    result = deepcopy(card)
    result["sources"] = deepcopy(previous["sources"])
    result["issues"] = deepcopy(previous["issues"])
    corrected = set(resolved_fields)
    for path in CARD_PATHS:
        old, key = _at(previous, path)
        new, _ = _at(result, path)
        if old[key] != new[key]:
            corrected.add(path)
    for path in corrected:
        result["sources"].pop(path, None)
    if "generated_fields" in previous:
        result["generated_fields"] = [path for path in previous["generated_fields"] if path not in corrected]
    result["issues"] = [item for item in result["issues"] if item["field"] not in corrected]
    return result


def ordinary_preview(spec):
    """Native persona_expression block for a new ordinary v8 desktop character."""
    from .companion_im import INSTRUCTION
    require(isinstance(spec, dict) and set(spec) == {"format_version", "name", *FIELDS}
            and type(spec["format_version"]) is int and spec["format_version"] == 1, "人设字段无效。")
    for field in ("name", *FIELDS):
        _spec_field(field, spec[field], blank=True)
    core = {k: spec[k] for k in sorted(set(spec) - {"format_version", "expression_style"})}
    ordered = {"format_version": 1, **core, "expression_style": spec["expression_style"]}
    return INSTRUCTION + json.dumps(ordered, ensure_ascii=False)
