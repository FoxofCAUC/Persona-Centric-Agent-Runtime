"""Review-only selection of already-rendered import requests. Local embeddings only."""
import argparse
from collections import defaultdict
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import numpy_module, private_directory
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.corpus_views import overlap_text
from persona_runtime.import_markdown import assign, parse, partition_rows
from persona_runtime.import_style import intent, negative, provider_for
from persona_runtime.paths import RuntimePaths

ROLES = ("A", "B")
REQUESTS_PER_ROLE = 3
MAX_CUE = 160


def read_study(instance, review):
    source = instance / "private/persona-import"
    contract = (review / "contract.json").read_bytes()
    authority = json.loads((source / "approval.json").read_bytes())
    if authority["contract_sha256"] != digest(contract):
        raise ValueError("审阅模板已有变化")
    if not authority["dispatch_closed"] or len(authority["request_hashes"]) != 2:
        raise ValueError("首批范围与已存记录不符")
    db = source / "imports.sqlite3"
    with sqlite3.connect(db.as_uri()+"?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM documents WHERE status='partial_ready'").fetchall()
        dispatched = conn.execute("SELECT count(*) FROM calls WHERE state='received' AND status=200").fetchone()[0]
        if len(rows) != 1 or dispatched != 2:
            raise ValueError("需要已回放两张首批回执的研究实例")
        row = dict(rows[0])
    if row["id"] != digest(row["raw"]) or row["approved"] != authority["contract_sha256"]:
        raise ValueError("来源或回执不符合首批绑定")
    source_rows = assign(parse(row["raw"]), json.loads(row["mapping"]), json.loads(row["overrides"]))
    view = partition_rows(source_rows, row["secret"], row["id"])
    learning = [
        dict(id=m["message_id"], speaker=m["role"], text=m["text"], group=m["fragment_id"])
        for m in view["messages"] if m["split"] == "learn" and m["target_candidate"]
    ]
    if learning != json.loads(row["view"]):
        raise ValueError("重建的学习分区与已存视图不一致")
    return row, view, learning, contract, authority


def reviewed_wires(review, learning, system, authority):
    wires = {}
    used = {role: set() for role in ROLES}
    for role in ROLES:
        cursor = 0
        positions = {m["id"]: i for i, m in enumerate(learning)}
        for path in sorted((review / "requests").glob(f"{role}-*.json")):
            payload = path.read_bytes()
            wire = json.loads(payload)
            messages = wire["messages"]
            data = json.loads(messages[-1]["content"])
            records = data["records"]
            start = positions[records[0]["id"]]
            if (len(messages) != 2 or messages[0] != {"role":"system", "content":system}
                    or messages[1]["role"] != "user" or data["target"] != role
                    or records != learning[start:start+len(records)] or start < cursor):
                raise ValueError("旧请求与学习视图不一致")
            cursor = start + len(records)
            if fingerprint(wire) in authority["request_hashes"]:
                used[role].add(path.name)
            wires[path.name] = {"payload":payload, "wire":wire, "records":records, "role":role}
    if any(len(used[role]) != 1 for role in ROLES):
        raise ValueError("找不到发言双方的首批请求")
    return wires, used


def pairs(messages, split, role):
    for cue, reply in zip(messages, messages[1:]):
        if (cue["split"] != split or reply["split"] != split or cue["role"] not in ROLES
                or cue["role"] == role or reply["role"] != role
                or cue["fragment_id"] != reply["fragment_id"]
                or not cue["target_candidate"] or not reply["target_candidate"]
                or not 0 < len(cue["text"].strip()) <= MAX_CUE or len(reply["text"]) > 600):
            continue
        yield cue, reply


def match_map(provider, view, wires, used, role, minimum):
    """Keep the strongest source pair for each request and covered dev group."""
    groups = {fragment["id"]:fragment["group_id"] for fragment in view["fragments"]}
    messages = [m | {"group_id":groups[m["fragment_id"]]} for m in view["messages"]]
    by_id = {m["message_id"]:m for m in messages}
    replay_groups = {by_id[r["id"]]["group_id"] for name in used[role] for r in wires[name]["records"]}
    request_of = {
        record["id"]:name for name, wire in wires.items()
        if wire["role"] == role and name not in used[role]
        for record in wire["records"]
    }
    by_intent = defaultdict(list)
    for cue, reply in pairs(messages, "learn", role):
        owner = request_of.get(cue["message_id"])
        if (owner and request_of.get(reply["message_id"]) == owner
                and cue["group_id"] not in replay_groups):
            by_intent[intent(cue["text"]), negative(cue["text"])].append((owner, cue, reply))
    trials = defaultdict(list)
    for cue, _ in pairs(messages, "dev_review", role):
        trials[intent(cue["text"]), negative(cue["text"])].append(cue)

    np = numpy_module()
    result = defaultdict(dict)
    for context, donors in by_intent.items():
        tests = trials[context]
        if not tests:
            continue
        test_vectors = provider.embed_documents([m["text"] for m in tests])
        test_keys = [overlap_text(m["text"]) for m in tests]
        for start in range(0, len(donors), 128):
            block = donors[start:start+128]
            values = provider.embed_documents([cue["text"] for _, cue, _ in block])
            scores = values @ test_vectors.T
            for i, j in np.argwhere(scores >= minimum):
                owner, cue, reply = block[i]
                if overlap_text(cue["text"]) == test_keys[j]:
                    continue
                test = tests[j]
                group = test["group_id"]
                score = float(scores[i, j])
                prior = result[owner].get(group)
                if prior is None or score > prior["score"]:
                    result[owner][group] = {
                        "cue_id":cue["message_id"], "reply_id":reply["message_id"],
                        "dev_cue_id":test["message_id"], "score":score,
                    }
    return dict(result)


def coverage(provider, view, wires, used, role, minimum):
    return {name:set(groups) for name, groups in match_map(provider, view, wires, used, role, minimum).items()}


def pick_requests(result, count=REQUESTS_PER_ROLE):
    left = {name:set(groups) for name, groups in result.items()}
    covered = set()
    selected = []
    while left and len(selected) < count:
        name = max(sorted(left), key=lambda key: (len(left[key]-covered), len(left[key])))
        new = left.pop(name)-covered
        if not new:
            break
        covered.update(new)
        item = {"name":name, "additional_dev_groups":len(new)}
        if isinstance(result[name], dict):
            item["matches"] = [{"dev_group_id":group, **result[name][group]} for group in sorted(new)]
        selected.append(item)
    return selected


def write_review(output, selected, wires, contract, authority, row):
    manifest = {"version":"pcar-import-style-supplement-v1", "document":row["id"],
                "contract_sha256":digest(contract), "status":"review_only",
                "prior_request_hashes":authority["request_hashes"], "requests":selected}
    for item in selected:
        entry = wires[item["name"]]
        item.update(role=entry["role"], sha256=digest(entry["payload"]), wire_hash=fingerprint(entry["wire"]),
                    messages=len(entry["records"]))
    manifest["manifest_sha256"] = fingerprint(manifest)
    (output/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (output/"contract.json").write_bytes(contract)
    table = []
    for item in selected:
        name = item["name"]
        (output/name).write_bytes(wires[name]["payload"])
        table.append(f'<li>{escape(item["role"])} · <a href="{escape(name)}">{escape(name)}</a> · '
                     f'{item["messages"]} 条记录 · 本地预筛新增 {item["additional_dev_groups"]} 个开发分区对话组</li>')
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline'">
<title>双人私聊补样审阅</title>
<style>body{{max-width:56rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui;color:#20302a}} li{{margin:.5rem 0}}a{{color:#11634d}}</style>
<h1>双人私聊补样审阅</h1>
<p>首批画像已通过作者审阅。现有样例未覆盖本地对照输入，本批从原有学习分区预选 {len(selected)} 个补样请求。每个链接是完整的脱敏请求，模板沿用首批版本，按同一官方 DeepSeek Flash 发送。</p>
<p>预筛以开发分区的不同对话组为参照。最终留出分区未参与补样选取。预筛统计表示已找到相近的历史片段，实际样例及其覆盖范围以补样回执为准。</p>
<ul>{"".join(table)}</ul>
<p><a href="contract.json">同版模板</a> · <a href="manifest.json">请求清单与指纹</a></p>
<p>本批用途限画像和样例补充。发言开关对照须核对最终人物字段、选中的原话与留出输入。</p></html>"""
    (output/"review.html").write_text(page, encoding="utf-8")
    return manifest


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for key in ("instance", "review", "embedding-home", "output"):
        cli.add_argument("--"+key, type=Path, required=True)
    args = cli.parse_args()
    instance, review, home, output = map(outside_repository, (args.instance, args.review, args.embedding_home, args.output))
    if output.exists():
        raise ValueError("审阅目录已存在")
    row, view, learning, contract, authority = read_study(instance, review)
    material = json.loads(contract)
    wires, used = reviewed_wires(review, learning, material["analysis_system"], authority)
    provider = provider_for(RuntimePaths(home))
    selected = []
    for role in ROLES:
        result = match_map(provider, view, wires, used, role, material["similarity_min"])
        picked = pick_requests(result)
        print(role, "positive_requests", len(result), "selected", len(picked), flush=True)
        selected.extend(picked)
    if not selected:
        raise ValueError("现有门限下没有可解释的补样片段")
    private_directory(home, output)
    manifest = write_review(output, selected, wires, contract, authority, row)
    for request in manifest["requests"]:
        print(request["name"], request["additional_dev_groups"])
    print("offline_review", output, flush=True)


if __name__ == "__main__":
    main()
