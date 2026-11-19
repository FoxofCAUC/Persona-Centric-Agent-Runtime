"""Export the complete model-visible v2 requests for private, offline author review."""
import argparse
from contextlib import closing
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory, safe_path
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.import_analysis_v2 import ImportAnalysisV2, VERSION
from persona_runtime.import_library import ImportLibrary, require
from persona_runtime.paths import RuntimePaths
from persona_runtime.import_prompts import contract_source


def _active_plan(library, document, plan_id):
    path = safe_path(library.root.parent.parent, library.root / "imports.sqlite3")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = db.execute("""SELECT p.id,p.document,p.payload FROM profile_plans p
                             JOIN profile_current c ON c.plan=p.id AND c.document=p.document
                             JOIN documents d ON d.id=p.document
                             WHERE d.status NOT IN ('running','revoked') AND d.view IS NULL AND (? IS NULL OR d.id=?)
                             AND (? IS NULL OR p.id=?) LIMIT 2""", (document, document, plan_id, plan_id)).fetchall()
        require(len(rows) == 1, "请指定一份已冻结且空闲的当前计划。")
        row = rows[0]
        plan = json.loads(row["payload"])
        expected = fingerprint([VERSION, row["document"], {k: v for k, v in plan.items() if k != "id"}])
        require(plan["id"] == row["id"] == expected, "计划指纹与存档不一致。")
        extracts = {r["batch"]: json.loads(r["accepted"]) for r in db.execute(
            """SELECT batch,accepted FROM profile_results WHERE plan=? AND stage='extract'
               AND state IN ('accepted','partial')""", (plan["id"],))}
    observations = [o for batch in plan["batches"] for o in extracts.get(batch["id"], ())]
    return row["document"], plan, observations


def _select(packets, batch_ids):
    available = {p["id"]: p for p in packets if "wire" in p}
    if batch_ids:
        require(len(set(batch_ids)) == len(batch_ids) and all(b in available for b in batch_ids),
                "所选批次不属于本次可审的请求。")
        return [available[b] for b in batch_ids]
    selected = []
    for role in ("A", "B"):
        first = next((p for p in available.values() if p["role"] == role), None)
        if first:
            selected.append(first)
    require(selected, "当前阶段没有可供审阅的请求。")
    return selected


def _page(manifest, templates, packets):
    snippets = []
    for item, packet in zip(manifest["requests"], packets, strict=True):
        wire = packet["wire"]
        body = json.loads(wire["messages"][1]["content"])
        snippets.append(
            f'<section><h3>{escape(item["role"])} · {escape(item["batch"])}</h3>'
            f'<p>{item["bytes"]:,} 字节，{item["records"]} 个输入片段／观察。</p>'
            f'<p>批准指纹 <code>{escape(item["wire_hash"])}</code><br>'
            f'传输正文 SHA-256 <code>{item["body_sha256"]}</code></p>'
            f'<p><a href="{escape(item["file"], quote=True)}">完整请求原文件</a></p>'
            f'<details open><summary>本请求的完整模型输入</summary>'
            f'<h4>系统文字</h4><pre>{escape(wire["messages"][0]["content"])}</pre>'
            f'<h4>目标与输入材料</h4><pre>{escape(json.dumps(body, ensure_ascii=False, indent=2))}</pre></details>'
            f'<details><summary>请求参数及完整 JSON</summary>'
            f'<pre>{escape(json.dumps(wire, ensure_ascii=False, indent=2))}</pre></details></section>')
    title = "分段提取" if manifest["stage"] == "extract" else "按人汇总"
    templates_html = "".join(
        f'<h3>{label}</h3><pre>{escape(templates.get(key) or "本版未提供")}</pre>'
        for label, key in (("分段提取", "extract_system"), ("按人汇总", "merge_system")))
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline';base-uri 'none';form-action 'none'">
<title>双人画像 · {title}审阅</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:68rem;margin:2rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f5f5;padding:1rem}}code{{overflow-wrap:anywhere}}
section{{border-top:1px solid #bbb;margin-top:2rem}}h2{{margin-top:2rem}}summary{{cursor:pointer}}</style></head><body>
<h1>双人画像 · {title}审阅</h1><p>本次清单含 {len(packets)} 个 {escape(manifest["model"])} 生成请求。
用途限于{title}，正式派发按模型输入和输出用量计费。此页在本机生成，实际派发 0 次。</p>
<p>模板 SHA-256 <code>{manifest["contract_sha256"]}</code><br>
计划 <code>{manifest["plan"]}</code></p>
<p><a href="manifest.json">完整清单</a> · <a href="contract-v2.json">模板原文件</a></p>
<h2>本版模板</h2>{templates_html}<h2>待审请求</h2>{"".join(snippets)}</body></html>"""


def export_review(home, output, *, stage="extract", document=None, plan_id=None, batch_ids=None):
    require(stage in ("extract", "merge"), "未知提炼阶段。")
    home, output = outside_repository(home), outside_repository(output)
    require(not output.exists(), "审阅目录已存在，请使用新的目录。")
    paths = RuntimePaths(home)
    library = ImportLibrary(paths)
    document, plan, observations = _active_plan(library, document, plan_id)
    engine = ImportAnalysisV2(library, ConfigStore(paths))
    packets = engine.preview(document, plan) if stage == "extract" else engine.preview_merge(document, plan, observations)
    selected = _select(packets, batch_ids)
    contract, _ = contract_source(library, "profile")
    require(contract is not None, "模板已撤回。")
    checksum = digest(contract)
    require(checksum == plan["settings"]["contract_sha256"], "模板已变化，请重新规划。")

    manifest = {"version": VERSION, "status": "review_only", "stage": stage, "document": document,
                "plan": plan["id"], "source_id": plan["source_id"], "contract_sha256": checksum,
                "model": plan["settings"]["model"], "base_url": plan["settings"]["base_url"], "requests": []}
    bodies = []
    for i, packet in enumerate(selected, 1):
        wire = packet["wire"]
        # CorpusLearningHTTPClient uses aiohttp's default json.dumps serializer.
        body = json.dumps(wire).encode("utf-8")
        name = f'requests/{i:02d}-{packet["role"]}.json'
        require(packet["wire_hash"] == fingerprint(wire), "请求指纹无效。")
        manifest["requests"].append({
            "file": name, "batch": packet["id"], "role": packet["role"], "stage": stage,
            "wire_hash": packet["wire_hash"], "body_sha256": digest(body), "bytes": len(body),
            "records": len(json.loads(wire["messages"][1]["content"])["records"]),
        })
        bodies.append(body)
    manifest["request_hashes"] = [item["wire_hash"] for item in manifest["requests"]]
    manifest["manifest_sha256"] = fingerprint(manifest)
    page = _page(manifest, json.loads(contract), selected)

    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_directory(output.parent, output)
    (output / "requests").mkdir(mode=0o700)
    for entry, body in zip(manifest["requests"], bodies, strict=True):
        (output / entry["file"]).write_bytes(body)
    (output / "contract-v2.json").write_bytes(contract)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "review.html").write_text(page, encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="导出双人画像完整模型输入的本机审阅页。")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["extract", "merge"], default="extract")
    parser.add_argument("--document")
    parser.add_argument("--plan")
    parser.add_argument("--batch", nargs="+", action="extend")
    args = parser.parse_args()
    manifest = export_review(args.home, args.output, stage=args.stage, document=args.document,
                             plan_id=args.plan, batch_ids=args.batch)
    print(json.dumps({"review": str(args.output / "review.html"), "requests": len(manifest["requests"]),
                      "request_hashes": manifest["request_hashes"], "manifest_sha256": manifest["manifest_sha256"]}))


if __name__ == "__main__":
    main()
