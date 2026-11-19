"""Produce an offline, private author review of the single dual-card generation request."""
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
from persona_runtime.import_card_job import CardJobs
from persona_runtime.import_library import ImportLibrary, require
from persona_runtime.paths import RuntimePaths
from persona_runtime.import_prompts import contract_source


def export_review(home, output, *, document=None, plan_id=None):
    home, output = outside_repository(home), outside_repository(output)
    require(not output.exists(),"请为此次审阅选择新的私有目录。")
    paths = RuntimePaths(home)
    library = ImportLibrary(paths)
    jobs = CardJobs(library,ConfigStore(paths))
    database = safe_path(home,library.root/"imports.sqlite3")
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        rows = db.execute("""SELECT d.id,d.revision,c.plan FROM documents d JOIN profile_current c
                             ON c.document=d.id WHERE d.view IS NULL AND d.status IN ('ready','partial_ready')
                             AND (? IS NULL OR d.id=?) AND (? IS NULL OR c.plan=?) LIMIT 2""",
                          (document,document,plan_id,plan_id)).fetchall()
        require(len(rows)==1,"请指定一份已取得双方画像的当前计划。")
        row = rows[0]
        packet = jobs.preview(row["id"],row["revision"],row["plan"],db=db)
    wire = packet["wire"]
    raw = json.dumps(wire).encode("utf-8")
    template, _ = contract_source(library, "card")
    require(template is not None, "模板已撤回。")
    require(digest(template)==packet["contract_sha256"],"成卡模板已变化。")
    manifest = {k:v for k,v in packet.items() if k!="wire"}
    manifest.update(status="review_only",request_hashes=[packet["wire_hash"]],max_tokens=wire["max_tokens"],
                    request_file="request.json",body_sha256=digest(raw),bytes=len(raw))
    manifest["manifest_sha256"] = fingerprint(manifest)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_directory(output.parent,output)
    (output/"request.json").write_bytes(raw)
    (output/"contract-card.json").write_bytes(template)
    (output/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    body = json.loads(wire["messages"][1]["content"])
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline';base-uri 'none';form-action 'none'">
<title>双人角色卡合成审阅</title>
<style>body{{font:16px/1.65 system-ui,sans-serif;max-width:64rem;margin:2rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f4;padding:1rem}}code{{overflow-wrap:anywhere}}</style></head><body>
<h1>双人角色卡合成审阅</h1><p>本次申请向官方 DeepSeek 发送一次完整请求，同时生成 A、B 两张候选角色卡。
模型 {escape(packet["model"])}，每次最多输出 {wire["max_tokens"]:,} tokens。实际调用可能产生费用。</p>
<p>源草稿修订 {packet["source_revision"]} · 请求正文 {len(raw):,} 字节 · 本页生成时新增派发 0 次。</p>
<p><a href="request.json">完整请求原文件</a> · <a href="contract-card.json">模板原文件</a> · <a href="manifest.json">批准范围及校验值</a></p>
<h2>合成模板</h2><pre>{escape(wire["messages"][0]["content"])}</pre>
<h2>实际输入</h2><pre>{escape(json.dumps(body,ensure_ascii=False,indent=2))}</pre>
<h2>运行时装配</h2><p>普通聊天读取保存的人格字段和角色资料。启用场景后，主模型读取本角色的共通提示和选中的场景段。
Jev 的选择请求使用其总说明、五个判据和本轮交流。最终文字在生成后单独审阅。</p>
<details><summary>完整请求参数</summary><pre>{escape(json.dumps(wire,ensure_ascii=False,indent=2))}</pre></details>
</body></html>"""
    (output/"review.html").write_text(page,encoding="utf8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--document")
    parser.add_argument("--plan")
    args = parser.parse_args()
    result = export_review(args.home,args.output,document=args.document,plan_id=args.plan)
    print(json.dumps({"review":str(args.output/"review.html"),"manifest_sha256":result["manifest_sha256"],
                      "wire_hash":result["wire_hash"],"requests":1},ensure_ascii=False))
