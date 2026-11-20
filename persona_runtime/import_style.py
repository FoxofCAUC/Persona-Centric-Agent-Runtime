"""Source-bound reference selection for opted-in native desktop replies."""
import json
import math
import re

from .archive import fingerprint
from .character_embedding import SemanticFailure, provider_for
from .import_library import ImportLibrary, encode, require
from .scene_policy import read, save

KEY = "import_style"
PREFIX = "persona.import_style."
REVOKED = "persona.import_revoked."


def frozen(turn):
    state = turn.get("context_policy_snapshot") or {}
    if isinstance(state,str):
        state=json.loads(state)
    return state.get(KEY)


def snapshot(db, pid):
    policy = read(db,PREFIX+pid)
    if policy and policy["enabled"] and not read(db,REVOKED+policy["document"]):
        return {"version":policy["version"], "revision":policy["revision"], "document":policy["document"],
                "contract":policy["contract"], "instance":policy["instance"]}


def intent(text):
    if re.search(r"(?:再见|晚安|先走|回头聊|拜拜)", text):
        return "close"
    if re.search(r"(?:帮我|帮忙|麻烦你|能不能帮|怎么做|怎么办)", text):
        return "request"
    if re.search(r"[?？]|(?:怎么|为什么|多少|哪[个里]|吗)[呀啊呢]?$",text):
        return "question"
    if re.search(r"(?:难过|委屈|害怕|很累|好烦)",text):
        return "comfort"
    return "share"


def negative(text):
    return bool(re.search(r"(?:不想|不喜欢|不要|没有|不行|讨厌|不能|没法|不是)", text))


def prepare_index(library, version, samples, provider):
    model=provider.identity()["fingerprint"]
    keys=[fingerprint([model,version,s["id"],s["cue"]]) for s in samples]
    with library.db() as db:
        missing=[(key,s) for key,s in zip(keys,samples)
                 if not db.execute("SELECT 1 FROM vectors WHERE id=?",(key,)).fetchone()]
    for offset in range(0,len(missing),32):
        batch=missing[offset:offset+32]
        vectors=provider.embed_documents([s["cue"] for _,s in batch])
        with library.db() as db:
            db.executemany("INSERT OR IGNORE INTO vectors VALUES(?,?)",
                           [(key,encode([float(x) for x in vec])) for (key,_),vec in zip(batch,vectors,strict=True)])


def validate(store,db,turn,proof):
    if not proof or not proof.get("ids"):
        return
    pid=turn["persona_id"]
    binding=frozen(turn)
    require(binding and binding==snapshot(db,pid) and binding["version"]==proof["version"],
            "表达来源或授权已变化。")
    lib=ImportLibrary(store.paths)
    _,checksum=lib.contract("style")
    require(checksum==binding["contract"] and binding["instance"]==str(store.paths.home.resolve()),
            "表达用途授权已变化。")
    material=lib.material(binding["version"])
    require(material["document"]==binding["document"],"表达来源已变化。")


def compatible_samples(samples, query, disabled):
    act = intent(query)
    query_negative = negative(query)
    return [
        sample for sample in samples
        if sample["id"] not in disabled
        and (sample["act"] == act
             or (sample["act"] == "other" and act == "share" and intent(sample["cue"]) == "share"))
        and negative(sample["cue"]) == query_negative
        and (sample["polarity"] != "positive" or not query_negative)
    ]


def rank_samples(samples, sample_vectors, current_vectors, recent_vectors, minimum, demoted=()):
    def similarity(left, right):
        return sum(x * y for x, y in zip(left, right, strict=True))

    ranked = []
    for sample in samples:
        ident = sample["id"]
        vector = sample_vectors.get(ident)
        if vector is None:
            continue
        current = max((similarity(chunk, vector) for chunk in current_vectors), default=-1)
        if not math.isfinite(current) or current < minimum:
            continue
        recent = max((similarity(chunk, vector) for chunk in recent_vectors), default=current)
        score = .85 * current + .15 * max(current, recent)
        if ident in demoted:
            score -= .25
        if math.isfinite(score) and score >= minimum:
            ranked.append((score, ident, sample["text"]))
    return sorted(ranked, reverse=True)


def attach_references(messages, details, *, ranked, prefix, budget, version, checksum, act):
    selected = []
    used = set()
    available = budget - sum(len(m["content"]) for m in messages)
    for _, ident, body in ranked:
        if body in used:
            continue
        new = selected + [(ident, body)]
        if len(prefix + encode([text for _, text in new])) <= available:
            selected = new
            used.add(body)
        if len(selected) == 2:
            break
    proof = {"version":version, "act":act, "ids":[ident for ident, _ in selected]}
    if not selected:
        return messages, details | {KEY:proof}
    content = prefix + encode([text for _, text in selected])
    result = [*messages[:-1], {"role":"user", "content":content}, messages[-1]]
    assembly = dict(details["prompt_assembly"])
    blocks = [dict(block) for block in assembly["blocks"]]
    blocks[-1]["message_index"] += 1
    blocks.insert(-1, dict(name=KEY, owner=KEY, version=checksum, message_index=len(result)-2, role="user",
                           start_character=0, characters=len(content), present=True, content_hash=fingerprint(content)))
    assembly.update(blocks=blocks, message_count=len(result), input_snapshot_hash=fingerprint(result),
                    input_characters=sum(len(message["content"]) for message in result))
    return result, details | {KEY:proof, "prompt_assembly":assembly, "input_characters":assembly["input_characters"]}


def project(engine, db, turn, messages, details):
    binding=frozen(turn)
    if not binding or turn.get("_search_notice") or turn.get("channel")!="desktop":
        return messages,details
    pid=turn["persona_id"]
    from .turn_input import load_turn_input
    query="\n".join(r["text"] for r in load_turn_input(db,turn))
    details=details | {KEY:{"version":binding["version"],"ids":[],"act":intent(query)}}
    if binding!=snapshot(db,pid) or binding["instance"]!=str(engine.store.paths.home.resolve()):
        return messages,details
    library=ImportLibrary(engine.store.paths)
    from .models import RuntimeErrorBase
    try:
        contract,checksum=library.contract("style")
        model=json.loads(turn["model_snapshot"])
        from .provider_policy import require_generation_provider
        require_generation_provider(model)
        if checksum!=binding["contract"] or model["name"]!=contract["reply_model"]:
            return messages,details
        material=library.material(binding["version"])
        provider=provider_for(engine.store.paths)
        embedding=provider.identity()["fingerprint"]
        act=intent(query)
        policy=read(db,PREFIX+pid)
        samples=compatible_samples(material["samples"], query, policy["disabled"])
        if not samples:
            return messages, details
        recent=[m["content"] for m in messages[1:-1][-4:] if m["content"].strip()]
        parts, vectors=provider.embed_query([*recent, query])
        current_vectors, recent_vectors = [], []
        for part, vector in zip(parts, vectors, strict=True):
            owner = current_vectors if part["member_index"] == len(recent) else recent_vectors
            owner.append(vector)
        sample_vectors={}
        with library.db() as source:
            for sample in samples:
                key=fingerprint([embedding,binding["version"],sample["id"],sample["cue"]])
                row=source.execute("SELECT value FROM vectors WHERE id=?",(key,)).fetchone()
                if row:
                    sample_vectors[sample["id"]]=json.loads(row[0])
        ranked=rank_samples(samples, sample_vectors, current_vectors, recent_vectors,
                            contract["similarity_min"], policy["demoted"].get(act, []))
    except (RuntimeErrorBase, SemanticFailure, OSError, ValueError, KeyError):
        return messages,details
    return attach_references(messages, details, ranked=ranked, prefix=contract["style_prefix"],
                             budget=model["max_input_chars"], version=binding["version"], checksum=checksum, act=act)
