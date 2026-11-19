"""Audit and preserve source pairs for a local style review. Zero provider calls."""
import argparse
from collections import Counter, defaultdict
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.corpus_views import overlap_text
from persona_runtime.import_analysis import CONTRACT, checked_result
from persona_runtime.import_style import compatible_samples, intent, negative, provider_for, rank_samples
from persona_runtime.paths import RuntimePaths
from .import_style_supplement import ROLES, match_map, pairs, read_study, reviewed_wires


def read(path):
    return json.loads(path.read_bytes())


def verify_manifest(manifest):
    expected = {key:value for key,value in manifest.items() if key != "manifest_sha256"}
    if manifest["manifest_sha256"] != fingerprint(expected):
        raise ValueError("审阅清单指纹不一致")


def scoped_wires(supplement, manifest, wires, used):
    selected = {}
    seen = set()
    permitted = {name for names in used.values() for name in names}
    for entry in manifest["requests"]:
        name = entry["name"]
        wire = wires[name]
        raw = (supplement/name).read_bytes()
        if (name in permitted or name in seen or entry["role"] != wire["role"]
                or digest(raw) != entry["sha256"] or raw != wire["payload"]
                or fingerprint(wire["wire"]) != entry["wire_hash"]):
            raise ValueError("补样片段与原始请求不一致")
        seen.add(name)
        selected[name] = wire
    for name in permitted:
        selected[name] = wires[name]
    return selected


def recovered_samples(home, supplement, manifest, wires, used, results):
    base_url = ConfigStore(RuntimePaths(home)).load()["model"]["base_url"]
    db = supplement / "study-instance/private/persona-import/imports.sqlite3"
    with sqlite3.connect(db.as_uri()+"?mode=ro", uri=True) as conn:
        receipts = dict(conn.execute("SELECT id,raw FROM calls WHERE state='received' AND status=200"))
    recovered = []
    for role in ROLES:
        names = used[role] | {
            r["name"] for r in results["requests"] if r["role"] == role and r["validated"]
        }
        previous = {sample["id"]:sample for sample in results["drafts"][role]["samples"]}
        seen = {}
        for name in sorted(names):
            packet = wires[name]
            key = fingerprint([CONTRACT,manifest["document"],manifest["contract_sha256"],base_url,packet["wire"]])
            value = json.loads(receipts[key])["choices"][0]
            if value["finish_reason"] != "stop":
                raise ValueError("已采用分段的回执不完整")
            _, samples = checked_result(json.loads(value["message"]["content"]), role, packet["records"])
            for sample in samples:
                seen[sample["id"]] = sample
                if sample["id"] not in previous:
                    recovered.append({"role":role,"request":name,"sample":sample})
        if any(seen.get(ident) != sample for ident,sample in previous.items()):
            raise ValueError("复核时已有样例发生变化")
    return recovered


def anchors(provider, view, wires, used, results, minimum, recovered):
    by_id = {m["message_id"]:m for m in view["messages"]}
    accepted = {res["name"]:res["validated"] for res in results["requests"]}
    if set(accepted) != set(wires) - {name for names in used.values() for name in names}:
        raise ValueError("补样回执范围与清单不一致")
    proposals, stats = [], {}
    for role in ROLES:
        current = {m["id"]:m for m in results["drafts"][role]["samples"]}
        restored = {m["sample"]["id"]:m["sample"] for m in recovered if m["role"] == role}
        found = match_map(provider, view, wires, used, role, minimum)
        coverage, represented, represented_after_replay = set(), set(), set()
        proposals_by_id = {}
        for name, groups in found.items():
            coverage.update(groups)
            raw = {item["id"]:item for item in wires[name]["records"]}
            for group, match in groups.items():
                cue, reply = (raw[match[key]] for key in ("cue_id","reply_id"))
                dev = by_id[match["dev_cue_id"]]
                if (cue["speaker"] not in ROLES or cue["speaker"] == role or reply["speaker"] != role
                        or cue["group"] != reply["group"] or int(cue["id"])+1 != int(reply["id"])
                        or any(by_id[row["id"]]["split"] != "learn" or by_id[row["id"]]["text"] != row["text"]
                               for row in (cue, reply))
                        or dev["split"] != "dev_review"):
                    raise ValueError("样例配对不符合原始学习分区")
                if reply["id"] in current:
                    if current[reply["id"]]["cue_id"] == cue["id"]:
                        represented.add(group)
                        represented_after_replay.add(group)
                    continue
                if reply["id"] in restored and restored[reply["id"]]["cue_id"] == cue["id"]:
                    represented_after_replay.add(group)
                    continue
                if reply["id"] not in proposals_by_id:
                    proposals_by_id[reply["id"]] = {
                        "role":role, "request":name, "request_validated":accepted[name],
                        "sample":{"id":reply["id"], "cue_id":cue["id"], "text":reply["text"], "cue":cue["text"],
                                  "act":intent(cue["text"]), "polarity":"negative" if negative(cue["text"]) else "neutral"},
                        "matches":[],
                    }
                proposals_by_id[reply["id"]]["matches"].append(match | {"dev_group_id":group})
        stats[role] = {"covered_dev_groups":len(coverage), "represented_by_receipts":len(represented),
                       "represented_after_replay":len(represented_after_replay),
                       "pending_source_pairs":len(proposals_by_id), "recovered_samples":len(restored)}
        proposals.extend(proposals_by_id.values())
    return proposals, stats


def holdout_check(provider, holdout, results, proposals, recovered, minimum):
    base = {role:results["drafts"][role]["samples"] for role in ROLES}
    added = {role:[row["sample"] for row in proposals if row["role"] == role] for role in ROLES}
    continuations = {role:[row["sample"] for row in recovered if row["role"] == role] for role in ROLES}
    stats = {}
    for role in ROLES:
        sample_map = {sample['id']:sample for sample in [*added[role], *continuations[role], *base[role]]}
        samples = list(sample_map.values())
        base_ids={item['id'] for item in base[role]}
        replay_ids=base_ids|{item['id'] for item in continuations[role]}
        vectors = {}
        for i in range(0,len(samples),32):
            block=samples[i:i+32]
            embedded=provider.embed_documents([item["cue"] for item in block])
            vectors.update((item["id"], vector) for item,vector in zip(block,embedded,strict=True))
        role_stats = {}
        fixed=[case["input"] for case in holdout["fixed"] if case["role"]==role]
        continuous=[turn["input"] for run in holdout["continuous"] if run["role"]==role for turn in run["turns"]]
        for condition, cases in (("fixed",fixed),("continuous",continuous)):
            before=replay=after=0
            strongest=None
            for items in cases:
                if any(digest(item["text"].encode())!=item["sha256"] for item in items):
                    raise ValueError("冻结对照输入指纹不一致")
                query="\n".join(item["text"] for item in items)
                _,current=provider.embed_query([query])
                eligible=compatible_samples(samples,query,())
                ranked=rank_samples(eligible,vectors,current,[],minimum)
                before+=any(hit[1] in base_ids for hit in ranked)
                replay+=any(hit[1] in replay_ids for hit in ranked)
                after+=bool(ranked)
                peak=max((sum(x*y for x,y in zip(chunk,vectors[item["id"]],strict=True))
                          for item in eligible for chunk in current),default=None)
                if peak is not None:
                    strongest=peak if strongest is None else max(strongest,peak)
            role_stats[condition]={"positions":len(cases),"baseline":before,"with_recovered":replay,"with_pending_pairs":after,
                                   "strongest":round(float(strongest),4) if strongest is not None else None}
        stats[role]=role_stats
    return stats


def development_check(provider, view, results, proposals, recovered, minimum):
    groups = {fragment["id"]:fragment["group_id"] for fragment in view["fragments"]}
    bands = (.45, .55, .65, .75, minimum)
    stats, calibration = {}, []
    for role in ROLES:
        original = results["drafts"][role]["samples"]
        continuations = [row["sample"] for row in recovered if row["role"] == role]
        candidates = [row["sample"] for row in proposals if row["role"] == role]
        samples = [*original,*continuations,*candidates]
        cue_vectors = dict(zip((sample["id"] for sample in samples),
                               provider.embed_documents([sample["cue"] for sample in samples]),strict=True))
        queries = [cue for cue,_ in pairs(view["messages"], "dev_review", role)]
        counts = Counter()
        review_bands = defaultdict(list)
        baseline_ids = {sample["id"] for sample in original}
        replay_ids = baseline_ids | {sample["id"] for sample in continuations}
        for cue, vector in zip(queries,provider.embed_documents([cue["text"] for cue in queries]),strict=True):
            query = cue["text"]
            normalized = overlap_text(query)
            eligible = [sample for sample in compatible_samples(samples,query,())
                        if overlap_text(sample["cue"]) != normalized]
            ranked = rank_samples(eligible,cue_vectors,[vector],[],minimum)
            counts["baseline"] += any(hit[1] in baseline_ids for hit in ranked)
            counts["with_recovered"] += any(hit[1] in replay_ids for hit in ranked)
            counts["with_pending_pairs"] += bool(ranked)
            if not eligible:
                continue
            # In a single-input probe the production blend equals current-cue cosine.
            score, sample = max(
                ((float(vector @ cue_vectors[entry["id"]]), entry) for entry in eligible),
                key=lambda item:(item[0],item[1]["id"]))
            band = sum(score >= floor for floor in bands)
            review_bands[band].append({
                "role":role, "dev_cue_id":cue["message_id"], "sample_id":sample["id"],
                "sample_cue_id":sample["cue_id"], "score":round(score,4),
                "dev_group_id":groups[cue["fragment_id"]],
            })
        used_groups = set()
        for band in sorted(review_bands,reverse=True):
            entries = sorted(review_bands[band],key=lambda item:(item["score"],int(item["dev_cue_id"])))
            mid = len(entries)//2
            for i in sorted(range(len(entries)),key=lambda index:abs(index-mid)):
                item=entries[i]
                if item["dev_group_id"] not in used_groups:
                    used_groups.add(item["dev_group_id"])
                    calibration.append(item)
                    break
        stats[role] = {"positions":len(queries),**dict(counts)}
    return stats, calibration


def write_review(output, report, messages):
    (output/"source-pairs.private.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    rows=[]
    for role, stat in report["development"].items():
        fixed=report["holdout"][role]["fixed"]
        continuous=report["holdout"][role]["continuous"]
        before=fixed["baseline"]+continuous["baseline"]
        after=fixed["with_pending_pairs"]+continuous["with_pending_pairs"]
        replay=fixed["with_recovered"]+continuous["with_recovered"]
        rows.append(f'<tr><td>{escape(role)}</td><td>{stat["covered_dev_groups"]}</td>'
                    f'<td>{stat["represented_by_receipts"]} → {stat["represented_after_replay"]}</td>'
                    f'<td>{stat["recovered_samples"]}</td><td>{stat["pending_source_pairs"]}</td>'
                    f'<td>{before} → {replay} → {after}</td></tr>')
    source_texts={m["message_id"]:m["text"] for m in messages}
    sections=[]
    for item in report["recovered"] + report["proposals"]:
        sample=item["sample"]
        traces=""
        for match in sorted(item.get("matches",[]),key=lambda row:-row["score"]):
            dev=match["dev_cue_id"]
            traces+=f'<p>开发分区 #{escape(dev)} · {match["score"]:.4f}</p><pre>{escape(source_texts[dev])}</pre>'
        if "matches" not in item:
            receipt = "从已验证回执复算，按同一段连续发言追溯对方上文"
        else:
            receipt = "该请求回执通过校验" if item["request_validated"] else "该请求回执已排除，配对来自原始记录"
        sections.append(f'<details><summary>{escape(item["role"])} · 样例 #{escape(sample["id"])}'
                        f' · {escape(item["request"])}</summary><p>{receipt}</p>'
                        f'<h3>对方上文 #{escape(sample["cue_id"])}</h3><pre>{escape(sample["cue"])}</pre>'
                        f'<h3>人物原话 #{escape(sample["id"])}</h3><pre>{escape(sample["text"])}</pre>{traces}</details>')
    calibration=[]
    for index,item in enumerate(report["calibration"],1):
        cue, source, reply = (source_texts[item[key]] for key in ("dev_cue_id","sample_cue_id","sample_id"))
        calibration.append(f'<details><summary>{index:02d} · {escape(item["role"])} · {item["score"]:.4f}</summary>'
                           f'<h3>本轮对方发言</h3><pre>{escape(cue)}</pre>'
                           f'<h3>历史样例的对方发言</h3><pre>{escape(source)}</pre>'
                           f'<h3>历史样例的目标人物原话</h3><pre>{escape(reply)}</pre></details>')
    scored=[]
    for role,value in report["development_replay"].items():
        scored.append(f'<p>{escape(role)} 的开发分区共 {value["positions"]} 个位置。'
                      f'原样例 {value["baseline"]} 个命中，恢复后 {value["with_recovered"]} 个，加原话候选后 {value["with_pending_pairs"]} 个。</p>')
    page=f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline'">
<title>表达样例来源修复审阅</title>
<style>body{{max-width:58rem;margin:2rem auto;padding:0 1rem;font:16px/1.65 system-ui;color:#20302a}} pre{{background:#f4f6f4;white-space:pre-wrap;overflow-wrap:anywhere;padding:.7rem}} th,td{{border-bottom:1px solid #ddd;padding:.35rem;text-align:left}}details{{margin:.6rem 0}}summary{{cursor:pointer}}</style>
<h1>表达样例来源修复审阅</h1>
<p>相似度门限为 {report["minimum"]}。先用已验证回执恢复连发样例，再按开发分区的匹配记录找回遗漏的原话配对。原话来自此前已审阅的脱敏请求，候选用于本机复查。</p>
<table><thead><tr><th>人物</th><th>预筛开发组</th><th>回执覆盖组（复算前后）</th><th>恢复连发样例</th><th>待审原话对</th><th>冻结命中（原样例、恢复后、加候选）</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<p>待审原话对按开发分区确定，冻结位置用于复验。场景标记只供本地检索。页面内的配对未经风格使用审定，尚未纳入发言装配。</p>
<h2>来源配对</h2>
{"".join(sections)}
<h2>开发分区复算</h2>
{"".join(scored)}
<h2>用于门限校准的人工审阅</h2>
<p>以下配对取自开发分区的不同对话组。请按编号标记历史样例是否适合参考本轮的接话方式。相似度仅供定位，评分结论需要结合原话。</p>
{"".join(calibration)}
<p><a href="source-pairs.private.json">原话编号和本地评分</a></p></html>"""
    (output/"review.html").write_text(page,encoding="utf-8")


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    for name in ("instance","review","supplement","holdout","home","output"):
        cli.add_argument("--"+name,type=Path,required=True)
    a=cli.parse_args()
    instance, review, supplement, holdout_path, home, output=map(outside_repository,
        (a.instance,a.review,a.supplement,a.holdout,a.home,a.output))
    if output.exists():
        raise ValueError("审阅目录已存在")
    row, view, learning, contract, authority=read_study(instance,review)
    cfg=json.loads(contract)
    manifest, holdout=read(supplement/"manifest.json"),read(holdout_path)
    results=read(supplement/"results.private.json")
    verify_manifest(manifest)
    verify_manifest(holdout)
    if (manifest["document"]!=row["id"] or holdout["document"]!=row["id"]
            or results["manifest_sha256"]!=manifest["manifest_sha256"]
            or manifest["prior_request_hashes"]!=authority["request_hashes"]
            or holdout["partition_sha256"]!=fingerprint(view["groups"])
            or digest(contract)!=manifest["contract_sha256"] or digest(contract)!=holdout["contract_sha256"]):
        raise ValueError("学习、补样和留出材料绑定不一致")
    wires, used=reviewed_wires(review,learning,cfg["analysis_system"],authority)
    provider=provider_for(RuntimePaths(home))
    scope=scoped_wires(supplement,manifest,wires,used)
    minimum=cfg["similarity_min"]
    recovered=recovered_samples(home,supplement,manifest,scope,used,results)
    proposals, stats=anchors(provider,view,scope,used,results,minimum,recovered)
    dev_replay, calibration=development_check(provider,view,results,proposals,recovered,minimum)
    measured=holdout_check(provider,holdout,results,proposals,recovered,minimum)
    report={"version":"pcar-import-style-source-repair-v1","status":"review_only",
            "supplement_manifest_sha256":manifest["manifest_sha256"],"holdout_manifest_sha256":holdout["manifest_sha256"],
            "minimum":minimum,"development":stats,"development_replay":dev_replay,"calibration":calibration,
            "holdout":measured,"recovered":recovered,"proposals":proposals,"model_calls":0}
    private_directory(home,output)
    write_review(output,report,view["messages"])
    print(json.dumps({"development":stats,"development_replay":dev_replay,"calibration_count":len(calibration),
                      "holdout":measured,"model_calls":0},ensure_ascii=False),flush=True)


if __name__=="__main__":
    main()
