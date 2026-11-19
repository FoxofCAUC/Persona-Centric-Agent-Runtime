"""Prepare a local blind human review from comparable v1 and v2 import receipts."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from html import escape
import json
from pathlib import Path
import secrets
import sqlite3
from types import SimpleNamespace

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.import_admission_v2 import admit_extraction, admit_merge
from persona_runtime.import_analysis import checked_result, make_wire
from persona_runtime.import_analysis_v2 import ImportAnalysisV2, VERSION, _unique_keys
from persona_runtime.import_library import CONTRACT, FIELDS, require
from persona_runtime.import_markdown import assign, learning_rows, parse
from persona_runtime.import_plan import ROLES
from persona_runtime.import_profile_workspace import projected_claims


FORMAT = "pcar-import-profile-quality-v1"
ARMS = ("v1", "v2")
FIELD_NAMES = dict(zip(FIELDS, ("人物介绍", "核心特征", "表达习惯", "价值与边界", "初始背景"), strict=True))


def read(path):
    return json.loads(path.read_bytes(), object_pairs_hook=_unique_keys)


@contextmanager
def database(instance):
    path = instance / "private/persona-import/imports.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        yield conn


def source(conn, document):
    row = conn.execute("SELECT * FROM documents WHERE id=?", (document,)).fetchone()
    require(row and row["status"] in ("ready", "partial_ready") and digest(row["raw"]) == document,
            "缺少可核验、仍有效的已提炼草稿。")
    return dict(row)


def response(conn, document, key, model_names, receipts):
    record = conn.execute("SELECT * FROM calls WHERE id=? AND document=?", (key, document)).fetchone()
    require(record and record["state"] == "received" and record["status"] == 200 and record["raw"],
            "缺少可与请求指纹匹配的首次 HTTP 回执。")
    metadata = json.loads(record["metadata"])
    checksum = digest(record["raw"])
    require(metadata.get("network_route") == "system_proxy" and metadata.get("raw_complete") is True
            and metadata.get("request_headers_sent") is True and metadata.get("response_headers_received") is True
            and metadata.get("response_sha256") == checksum, "缺少真实派发与回执完整性记录。")
    result = json.loads(record["raw"], object_pairs_hook=_unique_keys)
    require(result.get("id") and result.get("model") and result["choices"][0].get("finish_reason") == "stop",
            "回执缺少实际模型标识或输出不完整。")
    model_names.add(result["model"])
    receipts.append((key, checksum))
    return json.loads(result["choices"][0]["message"]["content"], object_pairs_hook=_unique_keys)


def claim_signatures(claims):
    signatures = defaultdict(set)
    for c in claims:
        signatures[c["field"], c["value"].strip()].update((r["id"], r["quote"]) for r in c["evidence"])
    return dict(signatures)


def legacy_claims(instance, request_dir, document, model):
    root = instance / "private/persona-import"
    contract_bytes = (root / "contract.json").read_bytes()
    contract, checksum = json.loads(contract_bytes), digest(contract_bytes)
    approval = read(root / "approval.json")
    require(contract["version"] == CONTRACT and approval.get("decision") == "approved"
            and "analysis" in approval.get("purposes", []) and approval["contract_sha256"] == checksum
            and contract["model"] == model["name"] and isinstance(approval.get("request_hashes"), list),
            "旧版模型或批准的输入范围不匹配。")
    wanted = set(approval["request_hashes"])
    found, models, receipts = set(), set(), []
    scoped = {r: {} for r in ROLES}
    obtained = {r: [] for r in ROLES}
    with database(instance) as conn:
        row = source(conn, document)
        require(row["approved"] == checksum, "旧草稿的分析合同已变化。")
        mapping, overrides = json.loads(row["mapping"]), json.loads(row["overrides"])
        assigned = assign(parse(row["raw"]), mapping, overrides)
        learning = learning_rows(assigned, row["secret"], document)
        require(learning == json.loads(row["view"]), "旧版学习分区无法按存档复现。")
        allowed = {r["id"]: r for r in learning}
        fragments = {r["id"]: r["group"] for r in learning}
        dates = {r["id"]: r["date"] for r in assigned}
        for path in sorted(request_dir.glob("*.json")):
            wire = read(path)
            wire_hash = fingerprint(wire)
            if wire_hash not in wanted or wire_hash in found:
                continue
            payload = json.loads(wire["messages"][-1]["content"], object_pairs_hook=_unique_keys)
            role, records = payload["target"], payload["records"]
            require(role in ROLES and records and wire == make_wire(model, role, records, contract["analysis_system"])
                    and all(allowed.get(r["id"]) == r for r in records), "旧版请求的学习范围或模型参数不一致。")
            key = fingerprint([CONTRACT, document, checksum, model["base_url"], wire])
            items, _ = checked_result(response(conn, document, key, models, receipts), role, records)
            obtained[role].extend(items)
            scoped[role].update({r["id"]: (r["speaker"], r["text"]) for r in records})
            found.add(wire_hash)
        require(found and found == wanted, "旧版实际获准的提取请求未能全部核对。")
        drafts = json.loads(row["draft"])
        archived = {}
        for role in ROLES:
            signatures = claim_signatures(obtained[role])
            require(signatures == claim_signatures(drafts[role]["claims"]),
                    "旧草稿和已核验分段回执的建议不一致。")
            archived[role] = [{"field": field, "value": value,
                               "evidence": [{"id": ident, "quote": quote} for ident, quote in sorted(refs)]}
                              for (field, value), refs in sorted(signatures.items())]
    return {"scoped": scoped, "models": models, "receipts": sorted(receipts), "fragments": fragments,
            "dates": dates, "mapping": [mapping, overrides], "claims": archived}


def compare_scope(scoped, batches):
    for role in ROLES:
        coverage = defaultdict(list)
        for batch in batches:
            if batch["role"] != role:
                continue
            for record in batch["records"]:
                ident, start, end = record["id"], record["start"], record["end"]
                origin = scoped[role].get(ident)
                require(origin and record["speaker"] == origin[0] and 0 <= start < end <= len(origin[1])
                        and origin[1][start:end] == record["text"], "两版本实际送达的说话人、原文或范围不一致。")
                if record["speaker"] != role or record["kind"] == "core":
                    coverage[ident].append((start, end))
        require(set(coverage) == set(scoped[role]) and coverage, "两版本的分人消息范围不一致。")
        for ident, ranges in coverage.items():
            boundary = 0
            for start, end in sorted(ranges):
                require(start <= boundary, "新版缺少旧版曾看到的消息区间。")
                boundary = max(boundary, end)
            require(boundary == len(scoped[role][ident][1]), "两版本的消息区间未完整匹配。")


class FrozenConfig:
    def __init__(self, settings):
        self.model = {k: settings[k] for k in ("base_url", "max_tokens", "max_input_chars")}
        self.model.update(name=settings["model"], _json_output=True)

    def load(self):
        return {"model": self.model}


def verified_study(legacy, requests, candidate, document, plan_id):
    instances = [outside_repository(p) for p in (legacy, requests, candidate)]
    legacy, requests, candidate = instances
    root = candidate / "private/persona-import"
    with database(candidate) as conn:
        row = source(conn, document)
        require(row["view"] is None, "该文件当前使用旧版草稿，需重新选择新版计划。")
        result = conn.execute("""SELECT p.payload FROM profile_plans p JOIN profile_current c
            ON c.plan=p.id AND c.document=p.document WHERE p.id=? AND p.document=?""", (plan_id, document)).fetchone()
        require(result, "没有匹配的当前新版计划。")
        plan = json.loads(result[0])
        require(plan.get("id") == plan_id and fingerprint([VERSION, document, {k: v for k, v in plan.items()
                if k != "id"}]) == plan_id, "新版计划指纹不一致。")
        config = FrozenConfig(plan["settings"])
        engine = ImportAnalysisV2(SimpleNamespace(root=root), config)
        baseline = legacy_claims(legacy, requests, document, config.model)
        require(baseline["mapping"] == [json.loads(row["mapping"]), json.loads(row["overrides"])],
                "两版本的说话人映射不一致。")
        compare_scope(baseline["scoped"], plan["batches"])
        results = {r["batch"]: r for r in conn.execute(
            "SELECT * FROM profile_results WHERE plan=? AND state!='superseded'", (plan_id,))}
        models, receipts, processed = set(), [], set()

        def replay(desc, validate):
            batch = desc["id"]
            stored = results.get(batch)
            require(stored and stored["role"] == desc["role"] and stored["stage"] == desc["stage"]
                    and stored["state"] in ("accepted", "partial") and "wire" in desc,
                    "新版的计划批次或归并尚未全部取得可验收回执。")
            contract = plan["settings"]["contract_sha256"]
            require(engine._permission(document, plan, contract, desc)[0], "新版请求的用途或模型输入尚未获准。")
            key = fingerprint([VERSION, document, plan["source_id"], contract, config.model["base_url"],
                               desc["stage"], desc["wire"]])
            actual, rejected = validate(response(conn, document, key, models, receipts))
            require(actual == json.loads(stored["accepted"]) and rejected == json.loads(stored["rejected"]),
                    "新版建议与首次回执重新校验结果不一致。")
            processed.add(batch)
            return actual

        observations = []
        for b, desc in zip(plan["batches"], engine.preview(document, plan), strict=True):
            observations.extend(replay(desc, lambda reply: admit_extraction(reply, b)))
        for desc, chunk in engine._merges(plan, observations):
            replay(desc, lambda reply: admit_merge(reply, desc["role"], desc["id"], chunk))
        require(processed == set(results) and len(models) == 1 and models == baseline["models"],
                "实际模型或归档批次不一致。")
        current = {r: projected_claims([row for row in results.values() if row["role"] == r], {}) for r in ROLES}
    binding = fingerprint([document, plan_id, baseline["mapping"], config.model, sorted(models),
                           baseline["scoped"], baseline["receipts"], sorted(receipts)])
    return binding, {"v1": baseline["claims"], "v2": current}, baseline


def proof_view(ref, scoped, baseline, role):
    ident, quote = ref["id"], ref["quote"]
    require(scoped[ident][0] == role and quote in scoped[ident][1], "建议引用与目标人物不符。")
    context = []
    for index in range(max(1, int(ident) - 1), int(ident) + 2):
        number = str(index)
        if number in scoped and baseline["fragments"][number] == baseline["fragments"][ident]:
            speaker, text = scoped[number]
            context.append({"id": number, "speaker": speaker, "text": text})
    return {"id": ident, "date": baseline["dates"][ident], "quote": quote, "context": context}


def blind_pack(binding, versions, baseline, origins):
    cases, arms = [], {}
    for arm in ARMS:
        for role in ROLES:
            scoped = baseline["scoped"][role]
            for claim in versions[arm][role]:
                item = {"id": secrets.token_hex(12), "role": role, "field": claim["field"], "value": claim["value"],
                        "scope": claim.get("scope", ""), "evidence": [], "counter": []}
                for prop, source_key in (("evidence", "evidence"), ("counter", "counter_evidence")):
                    item[prop] = [proof_view(ref, scoped, baseline, role) for ref in claim.get(source_key, [])]
                require(item["field"] in FIELDS and item["evidence"] and item["id"] not in arms, "建议来源无效。")
                cases.append(item)
                arms[item["id"]] = arm
    require(all(versions[a][r] for a in ARMS for r in ROLES), "缺少可为两个人物逐项评阅的双版本建议。")
    secrets.SystemRandom().shuffle(cases)
    key = {"format": FORMAT, "study": secrets.token_hex(16), "binding": binding, "origins": origins, "arms": arms}
    packet = {"format": FORMAT, "study": key["study"], "key_hash": fingerprint(key), "cases": cases}
    packet["packet_hash"] = fingerprint(packet)
    return packet, key


def score(packet, key, ratings, binding):
    require(all(isinstance(item, dict) for item in (packet, key, ratings)), "盲审输入格式无效。")
    header = {k: packet[k] for k in ("format", "study", "packet_hash")}
    require(header["format"] == FORMAT and key["study"] == header["study"] and key["binding"] == binding
            and packet["key_hash"] == fingerprint(key)
            and header["packet_hash"] == fingerprint({k: v for k, v in packet.items() if k != "packet_hash"})
            and all(ratings.get(k) == v for k, v in header.items()), "盲审包、盲键或来源绑定不一致。")
    reviewer = ratings.get("reviewer")
    require(isinstance(reviewer, str) and reviewer.strip(), "请填写评阅者标识。")
    rows = ratings.get("ratings")
    cases = {c["id"]: c for c in packet["cases"]}
    require(isinstance(rows, list) and len(rows) == len(cases) == len(packet["cases"])
            and {r["id"] for r in rows} == set(cases) == set(key["arms"]), "评阅行缺失、重复或来自其他包。")
    observed_fields = defaultdict(set)
    counts = {role: {arm: Counter(dict(total=0, supported=0, adoptable=0, core_traits=0, expression_style=0,
                                        critical=0)) for arm in ARMS} for role in ROLES}
    for r in rows:
        require(all(type(r.get(k)) is bool for k in ("supported", "adoptable", "critical"))
                and not (r["adoptable"] and (not r["supported"] or r["critical"])), "评阅判断有空项或冲突。")
        require(r["adoptable"] or isinstance(r.get("reason"), str) and r["reason"].strip(), "未采纳项需填写理由。")
        case = cases[r["id"]]
        arm = key["arms"][r["id"]]
        observed_fields[case["role"], arm].add(case["field"])
        c = counts[case["role"]][arm]
        c["total"] += 1
        for flag in ("supported", "adoptable", "critical"):
            c[flag] += int(r[flag])
        if r["adoptable"] and case["field"] in ("core_traits", "expression_style"):
            c[case["field"]] += 1
    states = {}
    for role in ROLES:
        before, after = counts[role]["v1"], counts[role]["v2"]
        if after["critical"]:
            states[role] = "failed"
            continue
        if (min(before["total"], after["total"]) < 12 or any(
                not {"core_traits", "expression_style"} <= observed_fields[role, arm] for arm in ARMS)):
            states[role] = "insufficient"
            continue
        valid = (after["supported"] * 100 >= 90 * after["total"]
                 and after["adoptable"] * 100 >= 80 * after["total"]
                 and min(after["core_traits"], after["expression_style"]) >= 3
                 and all(after[k] * before["total"] >= before[k] * after["total"]
                         for k in ("supported", "adoptable")))
        states[role] = "passed" if valid else "failed"
    status = "passed" if all(v == "passed" for v in states.values()) else (
        "failed" if "failed" in states.values() else "insufficient")
    rates = {role: {arm: {name: round(100 * c[name] / c["total"], 2) if c["total"] else None
                          for name in ("supported", "adoptable")} for arm, c in arms.items()}
             for role, arms in counts.items()}
    return {"format": FORMAT, "study": header["study"], "status": status, "roles": states, "counts": counts,
            "percent": rates, "packet_sha256": header["packet_hash"], "ratings_sha256": fingerprint(ratings), "source_binding": binding}


def review_html(packet):
    fields = []
    for index, case in enumerate(packet["cases"], 1):
        proofs = []
        for prop, label in (("evidence", "依据"), ("counter", "反例")):
            for r in case[prop]:
                surrounding = "\n".join(f'#{m["id"]} {m["speaker"]}　{m["text"]}' for m in r["context"])
                proofs.append(f'<details><summary>{label}　#{escape(r["id"])}　{escape(r["date"] or "")}</summary>'
                              f'<blockquote>{escape(r["quote"])}</blockquote><pre>{escape(surrounding)}</pre></details>')
        controls = "".join(f'<label>{label} <select name="{name}"><option value="">待评</option>'
                           '<option value="true">是</option><option value="false">否</option></select></label>'
                           for name, label in (("supported", "原话支持且适用场景准确"),
                                               ("adoptable", "可直接采用或仅需措辞小修"), ("critical", "关键错误")))
        fields.append(f'<fieldset data-id="{escape(case["id"])}"><legend>{index}　{case["role"]} · '
                      f'{FIELD_NAMES[case["field"]]}</legend><p>{escape(case["value"])}</p>'
                      f'<p>适用范围　{escape(case["scope"] or "见所引原话的情境")}</p>{"".join(proofs)}'
                      f'{controls}<label>理由 <textarea name="reason"></textarea></label></fieldset>')
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline';script-src 'unsafe-inline'">
<title>人设建议盲审</title><style>body{max-width:56rem;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui;color:#20302a}
fieldset{margin:1rem 0;padding:1rem}label{display:inline-block;margin:.4rem}textarea{width:100%;min-width:16rem}
blockquote,pre,p{white-space:pre-wrap;overflow-wrap:anywhere}pre{background:#f4f6f4;padding:.6rem}button{padding:.4rem .8rem}</style>
<h1>人设建议盲审</h1><p>请按人物逐项展开原话。关键错误包括串人、捏造经历、错用转述、已撤回来源或无依据的心理诊断。
未采纳项请记录理由。可导出当前进度并在此页重新载入。</p>
<label>评阅者标识 <input id="reviewer"></label><label>载入已保存的评分 <input id="load" type="file" accept=".json"></label>
<button type="button" class="save">保存评分</button><output id="notice"></output>"""
    header = {k: packet[k] for k in ("format", "study", "packet_hash")}
    script = """
const header=__HEADER__, items=Array.from(document.querySelectorAll('[data-id]'));
const flags=['supported','adoptable','critical'];
function save(){
  const ratings=items.map(f=>{
    const r={id:f.dataset.id,reason:f.elements.reason.value};
    for(const k of flags){const v=f.elements[k].value;r[k]=v===''?null:v==='true'}
    return r;
  });
  const data={...header,reviewer:document.getElementById('reviewer').value,ratings};
  const a=document.createElement('a'),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
  a.href=url;a.download='ratings-'+header.study+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
for(const b of document.querySelectorAll('.save')) b.addEventListener('click',save);
document.getElementById('load').addEventListener('change',async e=>{
  try{
    const data=JSON.parse(await e.target.files[0].text());
    if(!Object.entries(header).every(([k,v])=>data[k]===v))throw Error();
    const rows=new Map(data.ratings.map(r=>[r.id,r]));
    if(rows.size!==items.length||data.ratings.length!==items.length||!items.every(f=>rows.has(f.dataset.id)))throw Error();
    for(const f of items){
      const r=rows.get(f.dataset.id);for(const k of flags) f.elements[k].value=r[k]===null?'':String(r[k]);
      f.elements.reason.value=r.reason||'';
    }
    document.getElementById('reviewer').value=data.reviewer||'';
    document.getElementById('notice').textContent='已载入本包评分';
  }catch{document.getElementById('notice').textContent='评分文件与本包不匹配'}
});
"""
    return (page + "\n".join(fields) + '<button type="button" class="save">保存评分</button><script>'
            + script.replace("__HEADER__", json.dumps(header)) + "</script></html>")


def prepare(args):
    paths = [outside_repository(p) for p in (args.legacy, args.legacy_requests, args.candidate)]
    binding, versions, baseline = verified_study(*paths, args.document, args.plan)
    origins = [str(p) for p in paths] + [args.document, args.plan]
    packet, key = blind_pack(binding, versions, baseline, origins)
    output = outside_repository(args.output)
    require(not output.exists(), "请指定新的私有盲审目录，已有评阅包不能覆盖。")
    output.mkdir(parents=True, exist_ok=False)
    private_directory(output, output)
    control = private_directory(output, output / "control")
    reviewer = private_directory(output, output / "reviewer")
    (control / "key.private.json").write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    (reviewer / "packet.private.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    (reviewer / "review.html").write_text(review_html(packet), encoding="utf-8")
    return {"status": "awaiting_review", "suggestions": len(packet["cases"])}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    create = sub.add_parser("prepare")
    for name in ("legacy", "legacy-requests", "candidate", "output"):
        create.add_argument("--" + name, type=Path, required=True)
    for name in ("document", "plan"):
        create.add_argument("--" + name, required=True)
    check = sub.add_parser("score")
    check.add_argument("--pack", type=Path, required=True)
    check.add_argument("--ratings", type=Path, required=True)
    args = cli.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args)
        else:
            pack = outside_repository(args.pack)
            packet = read(pack / "reviewer/packet.private.json")
            key = read(pack / "control/key.private.json")
            require(fingerprint(key) == packet["key_hash"], "盲键不匹配。")
            binding, _, _ = verified_study(*key["origins"])
            result = score(packet, key, read(outside_repository(args.ratings)), binding)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, RecursionError, OSError, sqlite3.Error) as exc:
        # Only fixed diagnostic strings from our explicit validation reach the terminal.
        from persona_runtime.models import RuntimeErrorBase
        message = str(exc) if isinstance(exc, RuntimeErrorBase) else "需要完整、可核验的来源、回执和评分文件。"
        cli.exit(2, json.dumps({"status": "unverified", "reason": message}, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] in ("insufficient", "failed"):
        cli.exit(1)


if __name__ == "__main__":
    main()
