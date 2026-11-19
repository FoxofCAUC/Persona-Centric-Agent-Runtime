"""Exercise normal and interrupted handoffs on disposable frozen Windows instance copies."""
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

CASES = ("空白 实例", "旧历史 实例", "导入历史 实例")


async def worker(home, phase):
    import persona_runtime
    from persona_runtime.chat import ChatEngine, ModelCompletion
    from persona_runtime.config import ConfigStore
    from persona_runtime.ingress import IngressService
    from persona_runtime.models import PersonaSpec
    from persona_runtime.paths import RuntimePaths
    from persona_runtime.service import RuntimeService
    from persona_runtime.storage import Store

    assert Path(persona_runtime.__file__).resolve().parents[1]==Path.cwd().resolve()
    paths = RuntimePaths(home)
    store = Store(paths)
    store.initialize()
    cfg = ConfigStore(paths)
    assert cfg.load()["version"] == 3
    service = RuntimeService(store)
    state = home / "joint-fixture.json"
    ledger = home / "joint-calls.jsonl"
    if phase == "normal":
        assert not cfg.load()["model"]["encrypted_key"]
        cfg.update({"model": {"name": "900", "base_url": "http://127.0.0.1:9"}})
        pid = service.create_persona(PersonaSpec(1,"901","901",["901"],"901",["901"],"901"))["id"]
        service.use_persona(pid)
        from persona_runtime.migration import activate_instance, migration_pending
        if migration_pending(paths):
            activate_instance(paths,apply=True,resume_background=False)

    class LocalTransport:
        async def complete(self, model, messages):
            with ledger.open("a", encoding="ascii") as f:
                f.write(json.dumps(phase) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if phase == "crash":
                os._exit(78)
            return ModelCompletion("902", {"total_tokens": 1}, {"finish_reason": "stop"})

    chat = ChatEngine(store, cfg, LocalTransport())
    ingress = IngressService(chat)
    if phase == "normal":
        sid = chat.session(persona_id=pid)["session_id"]
        state.write_text(json.dumps([pid,sid]),encoding="ascii")
    else:
        pid,sid = json.loads(state.read_text(encoding="ascii"))
        assert service.show_persona(pid)["id"] == pid
        chat.recover()
        ingress.recover()
        ingress.tick()
        await chat.wait()
        assert chat.lookup("desktop","79001")["reply"] == "902"
    if phase in ("old-after-crash","final"):
        assert chat.lookup("desktop","79003")["status"] == "interrupted"
    if phase == "final":
        for key in ("79002","79004"):
            assert chat.lookup("desktop",key)["reply"] == "902"
    else:
        key = {"normal":"79001","old-normal":"79002","crash":"79003","old-after-crash":"79004"}[phase]
        await chat.submit("desktop",key,key,sid,persona_id=pid)
        await chat.wait()
        assert phase != "crash" and chat.lookup("desktop",key)["reply"] == "902"
    chat.cognitive.characters.close()


def run(baseline, candidate, output):
    from scripts.pcar_upgrade_check import snapshot, compare
    old = baseline / "旧版程序"
    if output.exists():
        raise ValueError("请选择新隔离目录")
    output.mkdir(parents=True)
    result = {}
    for name in CASES:
        original = baseline / name
        original_db = original / "data/database/runtime.sqlite3"
        original_config = (original/"config/workbench.json").read_bytes()
        instance = json.loads(original_config)["instance_id"]
        before = snapshot(original)
        assert before["schema_version"] == 33
        with original_db.open("rb") as f:
            fingerprint = hashlib.file_digest(f,"sha256").hexdigest()
        target = output / name
        shutil.copytree(original, target, ignore=shutil.ignore_patterns(".venv","__pycache__"))
        for phase,program in (("normal",candidate),("old-normal",old),("crash",candidate),
                              ("old-after-crash",old),("final",candidate)):
            cmd = [sys.executable,"-B","-c",
                   "import runpy,sys; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                   str(Path(__file__).resolve()),"--worker",str(target),phase]
            env = os.environ | {"PERSONA_RUNTIME_HOME":str(target),"PYTHONPATH":str(program)}
            p = subprocess.run(cmd,cwd=program,env=env,capture_output=True,timeout=90)
            if p.returncode != (78 if phase=="crash" else 0):
                raise AssertionError((name,phase,p.returncode,p.stderr.decode("utf8",errors="replace")[-1400:]))
        comparison = compare(before,snapshot(target))
        assert comparison["passed"],comparison["failures"][:3]
        with original_db.open("rb") as f:
            assert hashlib.file_digest(f,"sha256").hexdigest() == fingerprint
        assert (original/"config/workbench.json").read_bytes()==original_config
        copied_config=json.loads((target/"config/workbench.json").read_bytes())
        assert copied_config["version"]==3 and copied_config["instance_id"]==instance
        calls = [json.loads(line) for line in (target/"joint-calls.jsonl").read_text().splitlines()]
        assert calls == ["normal","old-normal","crash","old-after-crash"],calls
        with closing(sqlite3.connect((target/"data/database/runtime.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)
            assert not db.execute("PRAGMA foreign_key_check").fetchone()
        result[name] = {"original_database_sha256":fingerprint,"readable_original_rows":sum(map(len,before["tables"].values())),
                        "calls":calls,"comparison":comparison,"unknown_replays":0,"structure":33,
                        "config_version":3,"same_instance":True}
    (output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
    print(json.dumps(result,ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1]=="--worker":
        asyncio.run(worker(Path(sys.argv[2]),sys.argv[3]))
    else:
        baseline,candidate,output=(Path(v).resolve() for v in sys.argv[1:])
        run(baseline,candidate,output)
