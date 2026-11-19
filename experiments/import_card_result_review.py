"""Export the two admitted role cards and their actual scene/decision materials for local review."""
import argparse
from contextlib import closing
from html import escape
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory, safe_path
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.import_analysis_v2 import _unique_keys
from persona_runtime.import_card_fields import ROLES, ordinary_preview
from persona_runtime.import_card_job import VERSION, _receipt_cards
from persona_runtime.import_library import require
from persona_runtime.scene_policy import JEV
from persona_runtime.scene_voice import SCENES, jev_request

LABELS = dict(zip(SCENES, ("日常交流", "情绪与关系", "明确求助", "收束", "日常兜底"), strict=True))


def _pretty(value):
    return escape(json.dumps(value, ensure_ascii=False, indent=2))


def _section(role, card):
    display = card["spec"]["name"]
    body = [f'<section id="{role}"><h2>{role} · {escape(display)}</h2>']
    proposed = {item["field"]:item["proposal"] for item in card["issues"] if "proposal" in item}
    if proposed:
        body.append("<p>模型返回的文案在此供作者评阅。工作台已预填有出处的画像与原话样例，候选文字经人工核对后可以填入。</p>")
    labels = {"introduction":"人物介绍", "core_traits":"核心特征", "expression_style":"语言风格",
              "values_and_boundaries":"价值与边界", "initial_background":"初始背景"}
    for name,label in labels.items():
        path = "spec." + name
        value = proposed.get(path, card["spec"][name])
        if value:
            text = "\n".join(value) if isinstance(value, list) else value
            body.append(f'<h3>{label}</h3><pre>{escape(text)}</pre>')
    relation = proposed.get("relationship",card["relationship"])
    body.append(f'<h3>关系说明</h3><p>{escape(relation or "待补")}</p>')
    common = proposed.get("common",card["common"])
    body.append(f'<h3>角色共通提示</h3><pre>{escape(common or "待补")}</pre>')
    for key in SCENES:
        content = card["common"] + "\n\n" + card["scenes"][key]
        if not card["common"].strip() or not card["scenes"][key].strip():
            common = proposed.get("common", card["common"])
            segment = proposed.get("scenes."+key, card["scenes"][key])
            content = ("候选文案，需人工核对后启用。\n\n" + common + "\n\n" + segment
                       if common and segment else "共通提示或本场景正文待补。")
        body.append(f'<details><summary>{LABELS[key]} · 主模型提示</summary><pre>{escape(content)}</pre></details>')
    routing = jev_request(JEV, [], proposed.get("jev.instruction",card["jev"]["instruction"]),
                          {s:proposed.get("jev.criteria."+s, card["jev"]["criteria"][s]) for s in SCENES}, draft=True)
    body.append("<h3>Jev 场景决策</h3><p>当前显示拟用判别内容，待核文案经用户确认后启用。本轮对话在实际聊天时装入 state。</p>")
    body.append(f'<pre>{_pretty(routing)}</pre>')
    extra = {k:v for k,v in proposed.items() if k.startswith("character.")}
    if extra:
        body.append(f'<details><summary>模型提及、待核实的基础资料</summary><pre>{_pretty(extra)}</pre></details>')
    body.append('<h3>工作台当前预填</h3>')
    body.append(f'<details><summary>普通发言的人设正文</summary><pre>{escape(ordinary_preview(card["spec"]))}</pre></details>')
    body.append(f'<details><summary>角色资料及核验过的原话样例</summary><pre>{_pretty(card["character"])}</pre></details>')
    errors = [i for i in card["issues"] if "proposal" not in i]
    if errors:
        body.append(f'<details><summary>待补字段</summary><pre>{_pretty(errors)}</pre></details>')
    body.append("<h3>预填字段与来源</h3>")
    for field, sources in card["sources"].items():
        body.append(f'<details><summary>{escape(field)} · {len(sources)} 项观察</summary>')
        for item in sources:
            body.append(f'<p>{escape(item["value"])} · {escape(item["scope"])}</p>')
            for ref in item["evidence"]:
                body.append(f'<blockquote>#{escape(str(ref["id"]))}　{escape(ref["quote"])}</blockquote>')
        body.append("</details>")
    return "".join(body) + "</section>"


def export_result(home, output, *, document):
    home, output = outside_repository(home), outside_repository(output)
    require(not output.exists(), "请使用新的本机审阅目录。")
    file = safe_path(home, home/"private/persona-import/imports.sqlite3")
    with closing(sqlite3.connect(file.as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = db.execute("""SELECT j.*,c.raw,c.metadata,c.status AS http,d.status AS doc_state,
                            d.revision AS current_revision,p.plan AS current_plan FROM profile_card_jobs j
                            JOIN calls c ON c.id=j.call_id AND c.document=j.document
                            JOIN documents d ON d.id=j.document JOIN profile_current p ON p.document=j.document
                            WHERE j.document=?""", (document,)).fetchone()
        require(row and row["doc_state"] != "revoked" and row["current_plan"] == row["plan"]
                and row["source_revision"] == row["current_revision"] and row["outcome"] in ("ready","partial")
                and row["http"] == 200 and row["raw"], "没有可审阅的当前成卡回执。")
        frozen = json.loads(row["snapshot"])
        packet = frozen["packet"]
        wire = packet["wire"]
        meta = json.loads(row["metadata"])
        require(packet["version"] == VERSION and packet["wire_hash"] == meta["wire_hash"] == fingerprint(wire)
                and digest(row["raw"]) == meta["response_sha256"], "成卡请求或首次回执已变化。")
        obj = json.loads(row["raw"], object_pairs_hook=_unique_keys)
        choice = obj["choices"][0]
        require(choice["finish_reason"] == "stop", "首次回执不完整。")
        cards = _receipt_cards(obj, row["http"], frozen)
        require(cards == json.loads(row["candidate"]), "回执重放与成卡候选不一致。")
        audit = {"document": document, "plan": row["plan"], "wire_hash": packet["wire_hash"],
                 "response_sha256": digest(row["raw"]), "model": obj.get("model"), "usage": meta.get("usage"),
                 "candidate_sha256": fingerprint(cards), "outcome": row["outcome"]}
    private_directory(output.parent, output)
    (output/"first-response.json").write_bytes(row["raw"])
    for role in ROLES:
        (output/(role+".json")).write_text(json.dumps(cards[role], ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    (output/"validation.private.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2)+"\n", encoding="utf8")
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';style-src 'unsafe-inline';base-uri 'none'">
<title>双人角色卡结果审阅</title><style>body{font:16px/1.65 system-ui,sans-serif;max-width:64rem;margin:2rem auto;padding:0 1rem}
pre,blockquote{white-space:pre-wrap;overflow-wrap:anywhere;padding:.8rem;background:#f3f5f4}summary{cursor:pointer}</style></head><body>
<h1>双人角色卡结果审阅</h1><nav><a href="#A">角色 A</a> · <a href="#B">角色 B</a> ·
<a href="first-response.json">首次回执</a> · <a href="validation.private.json">用量和校验记录</a></nav>"""
    (output/"review.html").write_text(html+"".join(_section(role,cards[role]) for role in ROLES)+"</body></html>", encoding="utf8")
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--document", required=True)
    args = parser.parse_args()
    result = export_result(args.home, args.output, document=args.document)
    print(json.dumps({"review":str(args.output/"review.html"), "candidate_sha256":result["candidate_sha256"]}, ensure_ascii=False))
