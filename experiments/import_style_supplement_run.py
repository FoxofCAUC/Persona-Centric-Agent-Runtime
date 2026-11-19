"""Run the separately approved supplement and retain one receipt per request."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from persona_runtime.archive import fingerprint
from persona_runtime.character_embedding import private_directory
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.corpus_files import digest
from persona_runtime.corpus_store import outside_repository
from persona_runtime.import_analysis import CONTRACT, ImportAnalysis, checked_result, make_wire
from persona_runtime.paths import RuntimePaths
from persona_runtime.provider_policy import require_generation_provider
from .import_style_supplement import ROLES, read_study, reviewed_wires


def packet(review, approval_path, row, contract, authority, wires):
    manifest = json.loads((review/"manifest.json").read_bytes())
    checksum = manifest["manifest_sha256"]
    approval = json.loads(approval_path.read_bytes())
    items = manifest["requests"]
    hashes = [item["wire_hash"] for item in items]
    if (manifest["version"] != "pcar-import-style-supplement-v1"
            or manifest["status"] != "review_only"
            or checksum != fingerprint({k:v for k,v in manifest.items() if k != "manifest_sha256"})
            or approval.get("decision") != "approved" or approval.get("purpose") != "supplement_analysis"
            or approval.get("manifest_sha256") != checksum or approval.get("request_hashes") != hashes
            or len(hashes) != len(set(hashes)) or manifest["document"] != row["id"]
            or manifest["contract_sha256"] != digest(contract)
            or manifest["prior_request_hashes"] != authority["request_hashes"]
            or (review/"contract.json").read_bytes() != contract):
        raise ValueError("补样批准与当前请求清单不一致")
    if any(sum(item["role"] == role for item in items) != 3 for role in ROLES):
        raise ValueError("本批每名人物须有三个已审补样请求")
    for item in items:
        source = wires[item["name"]]
        copied = (review/item["name"]).read_bytes()
        if (source["role"] != item["role"] or digest(copied) != item["sha256"]
                or copied != source["payload"] or fingerprint(source["wire"]) != item["wire_hash"]
                or item["wire_hash"] in authority["request_hashes"]):
            raise ValueError("补样请求与原学习视图不一致")
    return manifest


def prepare_copy(home, source, review, manifest, contract):
    root = review/"study-instance"
    copy = root/"private/persona-import"
    journal = copy/"approval.json"
    checksum = manifest["manifest_sha256"]
    if root.exists():
        binding = json.loads(journal.read_bytes())
        if binding["manifest_sha256"] != checksum or (copy/"contract.json").read_bytes() != contract:
            raise ValueError("补样实例已绑定其他审阅清单")
        return RuntimePaths(root)
    private_directory(home, copy)
    original = source/"private/persona-import/imports.sqlite3"
    with sqlite3.connect(original.as_uri()+"?mode=ro", uri=True) as src, sqlite3.connect(copy/"imports.sqlite3") as dst:
        src.backup(dst)
    (copy/"contract.json").write_bytes(contract)
    atomic_json(journal, {"decision":"approved", "purposes":["analysis"], "scope":"targeted_supplement",
                          "contract_sha256":digest(contract), "manifest_sha256":checksum,
                          "request_hashes":[item["wire_hash"] for item in manifest["requests"]]})
    return RuntimePaths(root)


async def execute(task, manifest, wires, contract, model):
    digest_value = manifest["contract_sha256"]
    doc = manifest["document"]
    system = contract["analysis_system"]
    results = []
    verified = []
    for item in manifest["requests"]:
        name, role = item["name"], item["role"]
        material = wires[name]["records"]
        received = False
        claims = samples = []
        try:
            value = await task.call(doc, role, material, system, model, digest_value)
            claims, samples = checked_result(value, role, material)
            verified.append(item["wire_hash"])
            received = True
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            pass
        key = fingerprint([CONTRACT, doc, digest_value, model["base_url"], wires[name]["wire"]])
        with task.library.db() as db:
            row = db.execute("SELECT state,status,metadata FROM calls WHERE id=?", (key,)).fetchone()
        stats = json.loads(row["metadata"] or "{}").get("usage") if row else None
        results.append({"name":name, "role":role, "validated":received,
                        "state":row["state"] if row else "not_sent", "http_status":row["status"] if row else None,
                        "usage":stats, "claims":claims, "samples":samples})
    return results, verified


async def run(args):
    source, original_review, review, approval_path, home = map(outside_repository, (
        args.instance, args.original_review, args.review, args.approval, args.home))
    row, _, learning, raw_contract, authority = read_study(source, original_review)
    contract = json.loads(raw_contract)
    wires, _ = reviewed_wires(original_review, learning, contract["analysis_system"], authority)
    manifest = packet(review, approval_path, row, raw_contract, authority, wires)
    config = ConfigStore(RuntimePaths(home))
    model = config.load()["model"] | {"name":contract["model"], "_json_output":True}
    require_generation_provider(model)
    for item in manifest["requests"]:
        value = wires[item["name"]]
        if make_wire(model,item["role"],value["records"],contract["analysis_system"]) != value["wire"]:
            raise ValueError("模型配置与已审请求不一致")
    paths = prepare_copy(home, source, review, manifest, raw_contract)
    task = ImportAnalysis(paths, config)
    results_path = review/"results.private.json"
    if results_path.exists():
        raise ValueError("补样已有结果，请读取已生成文件")
    authority_path = task.library.root/"approval.json"
    grant = json.loads(authority_path.read_bytes())
    results, verified = await execute(task,manifest,wires,contract,model)
    atomic_json(authority_path, grant | {"dispatch_closed":True,
                "request_hashes":manifest["prior_request_hashes"]+verified})
    status = task.library.status(row["id"])
    task.start(row["id"], status["revision"], True, cached_only=True)
    await task.tasks[row["id"]]
    restored = task.library.status(row["id"])
    atomic_json(results_path, {"manifest_sha256":manifest["manifest_sha256"], "requests":results,
                              "draft_status":restored["status"],"drafts":restored["draft"]})
    for r in results:
        print(r["name"], r["state"], r["http_status"], r["validated"],
              len(r["claims"]), len(r["samples"]), flush=True)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for key in ("instance", "original-review", "review", "approval", "home"):
        cli.add_argument("--"+key, type=Path, required=True)
    asyncio.run(run(cli.parse_args()))


if __name__ == "__main__":
    main()
