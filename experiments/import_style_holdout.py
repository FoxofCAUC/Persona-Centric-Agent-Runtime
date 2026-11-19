"""Freeze intact final-holdout messages for the two native reply conditions."""
import argparse
from collections import defaultdict
from html import escape
import json
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.corpus_views import overlap_text
from persona_runtime.ingress import ROLLING_PARAMS
from .import_style_supplement import ROLES, read_study

TURNS = 6


def run_sequences(view):
    fragments = {f["id"]:f for f in view["fragments"] if f["split"] == "final_holdout"}
    groups = defaultdict(list)
    for message in view["messages"]:
        if message["fragment_id"] in fragments:
            groups[message["fragment_id"]].append(message)
    for fragment, messages in groups.items():
        runs = []
        active = None
        for message in messages:
            if (not message["target_candidate"] or message["role"] not in ROLES
                    or not message["text"].strip()):
                if runs:
                    yield fragments[fragment]["group_id"], runs
                runs, active = [], None
                continue
            if active is None or active[0] != message["role"]:
                active = (message["role"], [])
                runs.append(active)
            active[1].append({"id":message["message_id"], "text":message["text"],
                              "sha256":message["text_hash"]})
        if runs:
            yield fragments[fragment]["group_id"], runs


def acceptable(items):
    return len(items) <= ROLLING_PARAMS["max_messages"] and (
        sum(len(item["text"]) for item in items) <= ROLLING_PARAMS["max_chars"]
    )


def input_key(items):
    return overlap_text("\n".join(item["text"] for item in items))


def continuous(sequences, role, used_groups, used_inputs):
    for group, runs in sequences:
        if group in used_groups or len(runs) < 2*TURNS:
            continue
        for start in range(len(runs) - 2*TURNS + 1):
            candidate = runs[start:start + 2*TURNS]
            if candidate[0][0] == role or not all(acceptable(items) for _, items in candidate):
                continue
            turns = [{"input":query, "reference":reply} for (_, query), (_, reply) in
                     zip(candidate[::2], candidate[1::2], strict=True)]
            keys = {input_key(turn["input"]) for turn in turns}
            if len(keys) != TURNS or keys & used_inputs:
                continue
            return {"role":role, "group":group, "turns":turns}, keys
    return None


def fixed(sequences, role, used_groups, used_inputs):
    candidates = {}
    for group, runs in sequences:
        if group in used_groups or group in candidates:
            continue
        for index, (speaker, messages) in enumerate(runs[:-1]):
            if (speaker == role or not acceptable(messages) or not acceptable(runs[index+1][1])
                    or input_key(messages) in used_inputs):
                continue
            prior = runs[max(0, index-4):index]
            candidates[group] = {
                "role":role, "group":group, "input":messages,
                "preceding":[{"speaker":who, "messages":items} for who, items in prior],
                "reference":runs[index+1][1],
            }
            break
    groups = list(candidates)
    if len(groups) < TURNS:
        raise ValueError(f"{role} 的最终留出分区缺少六个独立位置")
    placed, keys = [], set(used_inputs)
    for turn in range(TURNS):
        point = turn * (len(groups)-1) // (TURNS-1)
        eligible = sorted(range(len(groups)), key=lambda index:(abs(index-point), index))
        for index in eligible:
            group = groups[index]
            candidate = candidates[group]
            key = input_key(candidate["input"])
            if group not in used_groups and key not in keys:
                used_groups.add(group)
                keys.add(key)
                placed.append(candidate)
                break
    if len(placed) != TURNS:
        raise ValueError(f"{role} 的固定输入去重后不足")
    used_inputs.update(keys)
    return placed


def freeze(view, draft, document, contract_digest, learned):
    sequences = list(run_sequences(view))
    used_groups, used_inputs = set(), {overlap_text(m["text"]) for m in learned}
    runs, cases = [], []
    for role in ROLES:
        chosen = (continuous(sequences, role, used_groups, used_inputs)
                  or continuous(sequences, role, set(), used_inputs))
        if chosen is None:
            raise ValueError(f"{role} 的最终留出分区缺少六轮完整连续对话")
        trial, keys = chosen
        used_groups.add(trial["group"])
        used_inputs.update(keys)
        runs.append(trial)
    for role in ROLES:
        cases.extend(fixed(sequences, role, used_groups, used_inputs))
    required = ("name", "introduction", "core_traits", "expression_style", "values_and_boundaries", "initial_background")
    fields = {role:[k for k in required if not draft[role]["spec"].get(k)] for role in ROLES}
    payload = {"version":"pcar-import-style-holdout-v1", "status":"review_only",
               "document":document, "contract_sha256":contract_digest,
               "partition_sha256":fingerprint(view["groups"]),
               "reserved_groups":len(used_groups), "role_fields_pending":fields,
               "shared_continuous_group":len({run["group"] for run in runs}) != len(runs),
               "fixed":cases, "continuous":runs}
    return payload | {"manifest_sha256":fingerprint(payload)}


def snippets(items):
    return "".join(f'<pre>#{escape(item["id"])}\n{escape(item["text"])}</pre>' for item in items)


def write_review(output, packet):
    (output/"holdout.private.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    fixed_panels, runs = [], []
    for i, case in enumerate(packet["fixed"], 1):
        history = "".join(f'<strong>{escape(old["speaker"])}</strong>{snippets(old["messages"])}' for old in case["preceding"])
        fixed_panels.append(f'<details><summary>{escape(case["role"])} · 固定位置 {i}</summary>'
                            f'<h3>固定前文</h3>{history}<h3>本轮输入</h3>{snippets(case["input"])}'
                            f'<h3>原记录中的回答 · 作者比对</h3>{snippets(case["reference"])}</details>')
    for trial in packet["continuous"]:
        entries = "".join(f'<h3>第 {i} 轮</h3>{snippets(turn["input"])}'
                          f'<details><summary>原记录中的回答 · 作者比对</summary>{snippets(turn["reference"])}</details>'
                          for i, turn in enumerate(trial["turns"], 1))
        runs.append(f'<details><summary>{escape(trial["role"])} · 六轮连续输入</summary>{entries}</details>')
    missing = {"initial_background":"初始背景", "core_traits":"核心特征", "values_and_boundaries":"价值和边界"}
    fields = "".join(f'<li>{role} · {escape("、".join(missing.get(k,k) for k in keys))}</li>'
                     for role, keys in packet["role_fields_pending"].items() if keys)
    shared = ("<p>这一份语料只有一个对话组同时满足完整六轮要求。连续对照取同一组的相反人物视角，输入原话分离，并各自维护历史。该组按一组来源计数。</p>"
              if packet["shared_continuous_group"] else "")
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline'">
<title>风格对照留出输入审阅</title>
<style>body{{max-width:56rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui;color:#20302a}} pre{{background:#f4f6f4;white-space:pre-wrap;overflow-wrap:anywhere;padding:.7rem}} details{{margin:.7rem 0}} summary{{cursor:pointer}}</style>
<h1>风格对照留出输入审阅</h1>
<p>两名人物各六个固定前文位置和一组六轮连续输入。选材按最终留出分区的原始次序、完整交流和跨组去重确定。页面只含本地候选，尚未派发。</p>
<p>固定位置将两份同版人设放在相同前文下比较。连续对话将同一组对方原话依序输入两个独立会话，每一版接自己的历史回复。原记录的目标人物回复供人工比对。</p>
{shared}<h2>首批草稿待补字段</h2><ul>{fields or "<li>必填字段齐备</li>"}</ul>
<h2>固定前文</h2>{"".join(fixed_panels)}
<h2>连续对话</h2>{"".join(runs)}
<p><a href="holdout.private.json">本地留出输入和出处</a></p></html>"""
    (output/"review.html").write_text(page, encoding="utf-8")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for key in ("instance", "review", "output"):
        cli.add_argument("--"+key, type=Path, required=True)
    args = cli.parse_args()
    instance, review, output = map(outside_repository, (args.instance, args.review, args.output))
    if output.exists():
        raise ValueError("留出目录已存在")
    row, view, learned, contract, _ = read_study(instance, review)
    result = freeze(view, json.loads(row["draft"]), row["id"], digest(contract), learned)
    private_directory(output.parents[2], output)
    write_review(output, result)
    print("fixed", len(result["fixed"]), "continuous", sum(len(run["turns"]) for run in result["continuous"]),
          "distinct_groups", result["reserved_groups"], flush=True)


if __name__ == "__main__":
    main()
