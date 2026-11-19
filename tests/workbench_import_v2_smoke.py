"""Exercise the native import workspace with an isolated backend and opaque local model doubles."""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from persona_runtime.chat import ModelCompletion
from persona_runtime.config import ConfigStore
from persona_runtime.corpus_files import digest
from persona_runtime.import_card_job import CardJobs
from persona_runtime.import_prompts import BUNDLE_PATH, prompt_bundle_status
from persona_runtime.scene_voice import SCENES
from persona_runtime.import_library import FIELDS, ImportLibrary
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.server import create_app
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from persona_runtime.workbench_webview import DesktopControls, workbench_html


class Transport:
    def __init__(self, conflicting_age):
        self.conflicting_age = conflicting_age
        self.roles = []
        self.compositions = 0
        self.replies = []
        self.hold_next=False
        self.entered=threading.Event()
        self.release=threading.Event()

    async def complete(self, model, messages):
        self.replies.append(messages)
        if self.hold_next:
            self.hold_next=False
            self.entered.set()
            if not await asyncio.to_thread(self.release.wait,35): raise RuntimeError("412")
            return ModelCompletion("7995", {"total_tokens":1}, {"finish_reason":"stop"})
        return ModelCompletion("7991", {"total_tokens": 1}, {"finish_reason": "stop"})

    async def extract(self, model, wire, *, save_raw):
        data = json.loads(wire["messages"][1]["content"])
        if set(data) == {"A", "B"}:
            self.compositions += 1
            cards = {}
            for role, records in data.items():
                sources = {obs["field"]: obs["id"] for obs in records["observations"]}
                first = next(iter(sources.values()))
                number = 500 if role == "A" else 600
                card = {}
                for idx, key in enumerate(FIELDS):
                    value = str(number + idx)
                    card["spec."+key] = {"sources":[sources[key]], "value":[value] if key in
                                         ("core_traits", "values_and_boundaries") else value}
                card["common"] = {"sources":[first], "value":str(number + 20)}
                card["jev.instruction"] = {"sources":[first], "value":str(number + 21)}
                for idx, scene in enumerate(SCENES):
                    card["scenes."+scene] = {"sources":[first], "value":str(number + 22 + idx)}
                    card["jev.criteria."+scene] = {"sources":[first], "value":str(number + 27 + idx)}
                if role == "A":
                    card["character"] = {"biography":{"occupation":"971"}}
                else:
                    card["scenes.closing"] = "975"
                    if self.conflicting_age:
                        card["character.age.fixed_age"] = {"value":18,"sources":[first]}
                        card["character.age.birth_date"] = {"value":"2000-01-02","sources":[first]}
                cards[role] = card
            raw = json.dumps({"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(cards)}}],
                              "usage":{"total_tokens":7}}).encode()
            save_raw(raw, http_status=200, diagnostics={"response_sha256":digest(raw),"raw_complete":True})
            return
        role = data["target"]
        self.roles.append(role)
        rows = [r for r in data["records"] if r["speaker"] == role and r["kind"] == "core"]
        evidence = [{k: r[k] for k in ("id", "start", "end")} for r in [rows[0], rows[-1]]]
        observations = [{"field": k, "scope": "91", "kind": "self_report",
                         "value": str((500 if role == "A" else 600) + n), "evidence": evidence}
                        for n, k in enumerate(FIELDS)]
        body = {"target": role, "batch": data["batch"], "observations": observations}
        raw = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}],
               "usage": {"total_tokens": 7}}
        save_raw(json.dumps(raw).encode(), http_status=200, diagnostics={})


def transcript(moect):
    lines = ["# 01", "由 Moect 导出", ""] if moect else []
    for day in range(1, 11):
        for role, speaker in enumerate(("X", "Y")):
            stamp = f"2026-01-{day:02d} 10:0{role}:00"
            lines.extend([f"[{stamp}] {speaker}" if moect else f"## {speaker} [{stamp}]",
                          f"{1000+day*10+role}" + ("\n18岁\n2000-01-02" if not moect and role else ""), ""])
    return "\n".join(lines).encode("utf-8")


class Screen:
    def __init__(self, window):
        self.window = window

    def eval(self, expression):
        return self.window.evaluate_js(expression)

    def wait(self, expression, *, timeout=18):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if self.eval(expression):
                    return
            except Exception:
                pass
            time.sleep(.08)
        details = self.eval("JSON.stringify([document.getElementById('dialog-title').textContent,"
                            "document.getElementById('toast').textContent,document.querySelector('dialog').open,document.getElementById('dialog-body').textContent.slice(0,500),document.getElementById('timeline').innerText.slice(-380),document.visibilityState,window.errors])")
        raise AssertionError(f"{expression} | {details}")

    def button(self, text, *, prefix=False):
        op = ".startsWith(" + json.dumps(text) + ")" if prefix else " === " + json.dumps(text)
        return "[...[...document.querySelectorAll('dialog[open]')].at(-1).querySelectorAll('button')].find(x=>x.textContent" + op + \
               " && !x.disabled && x.getClientRects().length)"

    def click(self, text, *, prefix=False):
        expr = self.button(text, prefix=prefix)
        self.wait("Boolean(" + expr + ")")
        self.eval("(" + expr + ").click(); true")

    def summary(self, label, *, contains=False):
        op = ".includes(" + json.dumps(label) + ")" if contains else " === " + json.dumps(label)
        expr = "[...document.querySelectorAll('dialog summary')].find(x=>x.textContent" + op + ")"
        self.wait("Boolean(" + expr + ")")
        self.eval("(" + expr + ").click(); true")

    def input(self, label):
        return "[...document.querySelectorAll('dialog label')].find(x=>x.textContent.includes(" + \
               json.dumps(label) + "))?.querySelector('input, textarea')"

    def check(self, label):
        expr = self.input(label)
        self.wait("Boolean(" + expr + ")")
        self.eval("(" + expr + ").click(); true")


def select_dialog(screen, label, value):
    find = "[...document.querySelectorAll('dialog label')].find(x=>x.textContent.includes("+json.dumps(label)+"))?.querySelector('select')"
    screen.wait("Boolean("+find+")")
    screen.eval("const control="+find+";control.value="+json.dumps(value)+";control.dispatchEvent(new Event('change'));true")


def confirm_history(screen):
    select_dialog(screen,"原记录中哪一方是我","A")
    screen.click("预览接续范围")
    screen.wait("document.querySelector('#dialog-title').textContent==='确认历史接续'")
    assert "deepseek" in screen.eval("document.querySelector('#dialog-body').innerText").lower()
    screen.check("确认本人、对方和消息范围")
    screen.click("保存并打开接续会话")
    screen.wait("!document.querySelector('dialog').open")
    screen.wait("document.querySelector('#timeline').textContent.includes('历史来源与用途')")


def timeline_click(screen,label):
    find = "[...document.querySelectorAll('#timeline button')].find(b=>b.textContent==="+json.dumps(label)+")"
    screen.wait("Boolean("+find+")")
    screen.eval("("+find+").click();true")


def send(screen,text):
    screen.eval("const input=document.querySelector('#message');input.value="+json.dumps(text)+";"
                "input.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#composer').requestSubmit();true")


def speech_state(store,input_text, states, *, timeout=22):
    end=time.monotonic()+timeout
    last=None
    while time.monotonic()<end:
        with store.connection() as db:
            last=db.execute("""SELECT p.state,(SELECT COUNT(*) FROM expression_speech_visible v WHERE v.utterance_id=u.id)
                FROM turns t JOIN events e ON e.id=t.user_event_id JOIN expression_speech_units u ON u.turn_id=t.id
                JOIN expression_speech_publications p ON p.utterance_id=u.id WHERE e.text=?""",(input_text,)).fetchone()
            if last and last[0] in states:return last[1]
        time.sleep(.1)
    raise AssertionError(f"{input_text}: expected {states}, got {tuple(last) if last else None}")


def inspect_history(screen, marker, existing, store):
    if existing:
        timeline_click(screen,"查看更早的消息")
        screen.wait("document.querySelector('#timeline').textContent.includes('88000')")
    timeline_click(screen,"查找历史消息")
    first="88000" if existing else "1010"
    screen.eval("document.querySelector('dialog input[type=search]').value="+json.dumps(first)+";true")
    screen.click("查找")
    screen.click("X · "+first)
    screen.click("引用这条消息")
    screen.wait("document.getElementById('history-reference').textContent.includes("+json.dumps(first)+")")
    before=len(marker.replies)
    send(screen,"7993")
    screen.wait("[...document.querySelectorAll('#timeline .bubble.reply')].some(x=>x.textContent==='7991')")
    end=time.monotonic()+18
    while len(marker.replies)==before and time.monotonic()<end: time.sleep(.08)
    assert len(marker.replies)==before+1
    request=json.dumps(marker.replies[-1],ensure_ascii=False)
    assert "7993" in request and first in request
    timeline_click(screen,"历史来源与用途")
    screen.wait("document.querySelector('dialog').innerText.includes('未开启')")
    screen.click("允许当前模型整理本次历史")
    screen.click("暂停本次整理")
    screen.wait("Boolean("+screen.button("允许当前模型整理本次历史")+")")
    screen.eval("document.getElementById('dialog-close').click();true")
    assert speech_state(store,"7993",{"complete"})==1
    timeline_click(screen,"88095" if existing else "1101")
    screen.click("引用这条消息")
    marker.hold_next=True
    send(screen,"7994")
    assert marker.entered.wait(18), "history-dependent main request did not dispatch"
    timeline_click(screen,"历史来源与用途")
    screen.wait("Boolean("+screen.button("撤回历史使用")+")")
    marker.release.set()
    assert speech_state(store,"7994",{"pending"})==0
    screen.click("撤回历史使用")
    screen.click("确认撤回历史使用")
    screen.wait("document.querySelector('#dialog-body').textContent.includes('已撤回')")
    screen.eval("document.getElementById('dialog-close').click();true")
    screen.wait("document.querySelector('#timeline').textContent.includes("+json.dumps(first if not existing else "88080")+")")
    assert speech_state(store,"7994",{"closed"})==0
    assert not screen.eval("[...document.querySelectorAll('#timeline .bubble.reply')].some(x=>x.textContent==='7995')")
    timeline_click(screen,first if not existing else "88080")
    assert not screen.eval("[...document.querySelectorAll('dialog button')].some(b=>b.textContent==='引用这条消息')")
    screen.eval("document.getElementById('dialog-close').click();true")


def reopen_history(home,existing):
    """A fresh desktop process reads the history from the same local instance."""
    import webview
    paths=RuntimePaths(home)
    config=ConfigStore(paths)
    marker=Transport(conflicting_age=False)
    loop=asyncio.new_event_loop()
    worker=threading.Thread(target=loop.run_forever,daemon=True)
    worker.start()
    async def serve():
        runner=web.AppRunner(create_app(paths,client=marker,foreground_only=True))
        await runner.setup()
        site=web.TCPSite(runner,"127.0.0.1",0)
        await site.start()
        config.update({"service_port":runner.addresses[0][1]})
        return runner
    runner=asyncio.run_coroutine_threadsafe(serve(),loop).result(20)
    controls=DesktopControls(lambda _:None,lambda:{"label":"100"},paths,lambda:None)
    window=webview.create_window("PCAR 重开验收",html=workbench_html(),js_api=controls,width=960,height=760,hidden=True)
    failures=[]
    def check_reopened():
        screen=Screen(window)
        try:
            last="88095" if existing else "1101"
            screen.wait("document.querySelector('#timeline').textContent.includes("+json.dumps(last)+")")
            screen.wait("document.querySelector('#timeline').textContent.includes('7993')")
            timeline_click(screen,"历史来源与用途")
            screen.wait("document.querySelector('#dialog-body').textContent.includes('已撤回')")
            assert marker.replies==[]
        except BaseException as exc:
            failures.append(exc)
        finally:
            window.destroy()
    try:
        webview.start(check_reopened,gui="edgechromium",private_mode=True,storage_path=str(home/"browser"))
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(35)
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=10)
        loop.close()
    if failures:raise failures[0]


def check_restart(home, existing):
    import persona_runtime
    launch=("import sys,runpy;sys.path.insert(0,sys.argv.pop(1));"
            "h=sys.argv.pop(1);sys.argv[0]=h;runpy.run_path(h,run_name='__main__')")
    args=[sys.executable,"-B","-I","-W","ignore","-X","utf8","-c",launch,
          str(Path(persona_runtime.__file__).resolve().parents[1]),str(Path(__file__).resolve()),
          "--reopen-home",str(home),"--temp-root",str(home.parent)]
    if existing:args.append("--existing")
    subprocess.run(args,cwd=home,check=True,timeout=75)


def exercise(home, existing, baseline=None, *, history=False):
    import webview
    paths = RuntimePaths(home)
    config = ConfigStore(paths)
    config.initialize()
    if existing:
        config.update({"model": {"base_url": "https://api.deepseek.com", "api_key": "100"}})
    store = Store(paths)
    store.initialize()
    library = ImportLibrary(paths)
    raw = transcript(moect=not existing)
    file = home / "chat.md"
    file.write_bytes(raw)
    history_file = home / "older.md"
    if existing and history:
        history_file.write_text("\n".join(f"## {('X','Y')[i%2]}\n{88000+i}" for i in range(96)),encoding="utf8")
    marker = Transport(conflicting_age=existing)
    if existing:
        template = dict(format_version=1,name="900",introduction="901",core_traits=["902"],
                        expression_style="903",values_and_boundaries=["904"],initial_background="905")
        old_person=RuntimeService(store).create_persona(PersonaSpec.from_dict(template))
        if history:
            from persona_runtime.import_history import restore
            from persona_runtime.import_markdown import assign,parse
            original_file=home/"legacy.md"
            original_raw=b"## X\n97000\n## Y\n97001\n"
            original_file.write_bytes(original_raw)
            original=library.add(original_raw)
            old_rows=assign(parse(original_raw),original["mapping"],{})
            with store.connection(write=True) as db:
                old_session=restore(db,old_person["id"],original["id"],"B",old_rows)
                db.execute("UPDATE turns SET origin_json=json_remove(origin_json,'$.sources') WHERE session_id=?", (old_session,))
                original_ids=[r[0] for r in db.execute("SELECT id FROM events WHERE session_id=? ORDER BY seq",(old_session,))]
        legacy = library.add(raw)
        legacy = library.prepare(legacy["id"], legacy["revision"], legacy["mapping"], {})
        library.edit(legacy["id"], legacy["revision"], "A", template, "906")
    assert prompt_bundle_status(library)["source"] == "builtin"
    invalid = home / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    release_file = BUNDLE_PATH.resolve().parents[2] / "双人角色卡提示词.json"
    if not release_file.is_file():
        release_file = home / "双人角色卡提示词.json"
        release_file.write_bytes(BUNDLE_PATH.read_bytes())
    selections = iter((None, invalid, release_file))
    transcripts=iter([original_file,file,history_file] if existing and history else [file])
    def choose_file(*args, file_types, **kwargs):
        path = next(selections) if ".json" in file_types[0] else next(transcripts)
        return [str(path)] if path else None
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    async def serve():
        runner = web.AppRunner(create_app(paths, client=marker, foreground_only=True))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        config.update({"service_port": runner.addresses[0][1]})
        return runner
    runner = asyncio.run_coroutine_threadsafe(serve(), loop).result(20)
    controls = DesktopControls(lambda _: None, lambda: {"label": "100"}, paths,
                               lambda: SimpleNamespace(create_file_dialog=choose_file))
    markup = workbench_html().replace("<script>", "<script>window.errors=[];addEventListener('error',"
        "e=>window.errors.push(e.message))</script><script>", 1)
    window = webview.create_window("PCAR 验收", html=markup, js_api=controls, width=960, height=760, hidden=not history, focus=False)
    failures = []
    def flow():
        screen = Screen(window)
        try:
            screen.wait("Boolean(window.pcarLive?.active)")
            if existing:
                screen.wait("document.getElementById('sidebar').textContent.includes('900')")
                if history:
                    screen.wait("document.querySelector('#timeline').textContent.includes('97001')")
                    timeline_click(screen,"97000")
                    assert not screen.eval("[...document.querySelectorAll('dialog button')].some(b=>b.textContent==='引用这条消息')")
                    screen.eval("document.getElementById('dialog-close').click();true")
                    timeline_click(screen,"历史来源与用途")
                    screen.click("选择原件并核对本人")
                    confirm_history(screen)
                    timeline_click(screen,"97000")
                    screen.wait("Boolean("+screen.button("引用这条消息")+")")
                    screen.eval("document.getElementById('dialog-close').click();true")
                    with store.connection() as db:
                        assert original_ids==[r[0] for r in db.execute("SELECT id FROM events WHERE session_id=? ORDER BY seq",(old_session,))]
            screen.eval("document.querySelector('#new-person').click(); true")
            screen.wait("document.getElementById('dialog-body').textContent.includes('内置')")
            if not existing:
                for attempt in range(3):
                    screen.click("导入提示词文件")
                    if attempt == 0:
                        screen.wait("!document.querySelector('#dialog-body > .fields').inert")
                        assert prompt_bundle_status(library)["source"] == "builtin"
                    elif attempt == 1:
                        screen.wait("document.querySelector('#dialog-body small[role=alert]')?.textContent.includes('发布版')")
                    else:
                        screen.wait("document.getElementById('dialog-body').textContent.includes('已导入')")
                assert prompt_bundle_status(library)["source"] == "file"
            screen.click("从聊天记录生成")
            if existing:
                screen.click("使用新版分批提炼")
            screen.click("确认说话人并继续")
            screen.click("查看或调整本次分析范围")
            screen.click("查看第 1 批待发内容")
            screen.wait("document.querySelector('#dialog-body').innerText.includes('本人主片段')")
            screen.click("返回分析范围")
            screen.click("返回草稿")
            if not existing:
                screen.click("配置本次模型连接")
                screen.eval("document.querySelector('dialog input[type=password]').value='100';true")
                screen.click("保存连接")
                screen.wait("document.getElementById('dialog-title').textContent==='聊天人设草稿'")
            screen.wait("document.getElementById('dialog-body').textContent.includes('8,192 tokens')")
            screen.check("允许官方 DeepSeek")
            screen.click("分析并自动填写两张角色卡")
            screen.wait("document.getElementById('dialog-title').textContent.includes('核对角色卡 · A')")
            assert not screen.eval("document.querySelector('dialog').textContent.includes('还原这段历史')")
            if existing:
                screen.wait("Boolean(document.querySelector('dialog.import-conflict-dialog[open]'))")
                screen.click("返回角色卡")
                screen.click("暂存并查看 B")
                screen.click("确认创建并打开聊天")
                screen.wait("Boolean(document.querySelector('dialog.import-conflict-dialog[open]'))")
                screen.click("处理 B 的「固定年龄」")
                screen.wait("document.activeElement?.dataset.cardPath==='character.age.fixed_age'")
                screen.wait("""[...document.querySelectorAll('#dialog-body .import-conflict-row li')].some(
                    x=>x.textContent.includes('2000-01-02'))""")
                screen.wait("""[...document.querySelectorAll('#dialog-body .import-conflict-row li')].some(
                    x=>x.textContent.includes('固定年龄')&&x.textContent.includes('18'))""")
                screen.click("确认「固定年龄」当前填写")
                screen.wait("!document.querySelector('#dialog-body > .fields').inert")
                screen.click("处理严重冲突（1）")
                screen.click("处理 B 的「出生日期」")
                screen.wait("document.activeElement?.dataset.cardPath==='character.age.birth_date'")
                screen.eval("document.activeElement.value='2000-01-02';true")
                screen.click("确认「出生日期」当前填写")
                screen.wait("!document.getElementById('dialog-body').textContent.includes('处理严重冲突')")
                saved = CardJobs(library,config).status(digest(raw))["cards"]["B"]
                assert not [item for item in saved["issues"] if item.get("severity")=="conflict"]
                assert saved["character"]["age"]["birth_date"]=="2000-01-02"
                screen.click("暂存并查看 A")
                screen.click("查看原画像")
                screen.summary("查看旧计划草稿")
                screen.eval("document.querySelector('dialog details[open] button').click();true")
                screen.wait("document.getElementById('dialog-title').textContent.includes('归档草稿')")
                screen.click("返回当前草稿")
                screen.wait("document.getElementById('dialog-title').textContent.includes('核对角色卡 · A')")
            before = CardJobs(library,config).status(digest(raw))
            assert before["cards"]["A"]["character"]["biography"]["occupation"] is None
            assert before["cards"]["B"]["scenes"]["closing"]=="975"
            if existing:
                screen.summary("可选补充 · 1"); screen.summary("职业")
                screen.click("核对后填入「职业」")
                screen.wait("(" + screen.input("职业") + ")?.value === '971'")
                after = CardJobs(library,config).status(digest(raw))
                assert after["cards"]["A"]["character"]["biography"]["occupation"] == "971"
                assert "character.biography.occupation" not in after["cards"]["A"]["sources"]
                screen.summary("人物资料"); screen.summary("外貌")
                assert screen.eval("(" + screen.input("身高（厘米）") + ").value") == ""
                screen.eval("(" + screen.input("人物介绍") + ").value='989';true")
                screen.summary("预览普通发言人设正文")
                screen.wait("[...document.querySelectorAll('dialog pre')].some(p=>p.innerText.includes('989'))")
                screen.summary("场景提示与 Jev"); screen.summary("日常交流"); screen.summary("完整场景提示")
                screen.wait("[...document.querySelectorAll('dialog pre')].some(p=>p.innerText === '520\\n\\n522')")
                screen.summary("字段出处与待补项"); screen.summary("共通人设提示")
                screen.click("A · #", prefix=True)
                screen.wait("Boolean(document.querySelector('dialog mark')?.textContent)")
                screen.click("返回角色卡"); screen.click("暂存并查看 B")
                screen.wait("document.getElementById('dialog-title').textContent.includes('核对角色卡 · B')")
                assert screen.eval("(" + screen.input("人物介绍") + ").value") == "600"
                screen.summary("人物资料"); screen.summary("外貌")
                screen.eval("(" + screen.input("身高（厘米）") + ").value='177';true")
            if history and not existing:
                select_dialog(screen,"保存后打开的会话","history")
            screen.check("同时创建 A 和 B"); screen.click("确认创建并打开聊天")
            if history and not existing:
                confirm_history(screen)
                screen.wait("document.querySelector('#timeline').innerText.includes('1101')")
            else:
                screen.wait("!document.querySelector('dialog').open")
                screen.wait("document.querySelector('#timeline').innerText.includes('可以发送第一条消息')")
            screen.eval("const input=document.querySelector('#message');input.value='7992';"
                        "input.dispatchEvent(new Event('input',{bubbles:true}));"
                        "document.querySelector('#composer').requestSubmit();true")
            screen.wait("document.querySelector('#timeline').innerText.includes('7991')")
            assert len(marker.replies) == 1 and "7992" in json.dumps(marker.replies[0])
            if existing:
                screen.eval("window.pcarLive.profile();true")
                screen.summary("已保存的角色卡与场景")
                screen.wait("document.querySelector('dialog').innerText.includes('620')")
                current = CardJobs(library,config).status(digest(raw))["cards"]["B"]
                assert current["scenes"]["closing"] == "975" and "scenes.closing" in current["generated_fields"]
                screen.click("编辑来源场景")
                screen.wait("document.getElementById('dialog-title').textContent.includes('编辑来源场景')")
                screen.summary("收束")
                screen.eval("""document.querySelector('[data-card-path="scenes.closing"]').value='976';true""")
                screen.click("暂存并返回人物资料")
                screen.summary("已保存的角色卡与场景")
                screen.wait("document.querySelector('dialog').innerText.includes('来源草稿中的场景已修改')")
                assert CardJobs(library,config).status(digest(raw))["cards"]["B"]["scenes"]["closing"] == "976"
                screen.eval("document.getElementById('dialog-close').click();true")
            if history:
                if existing:
                    screen.eval("document.getElementById('import-history').click();true")
                    confirm_history(screen)
                    screen.wait("document.querySelector('#timeline').innerText.includes('88095')")
                    assert not screen.eval("document.querySelector('#timeline').textContent.includes('7992')")
                else:
                    assert "1101" in json.dumps(marker.replies[0])
                inspect_history(screen,marker,existing,store)
            with store.connection() as db:
                assert db.execute('PRAGMA user_version').fetchone()[0] == 33
                assert db.execute('SELECT count(*) FROM personas').fetchone()[0] == (3 if existing else 2)
                assert bool(db.execute("SELECT count(*) FROM turns WHERE json_extract(origin_json,'$.import_document') IS NOT NULL").fetchone()[0]) == history
                assert db.execute("SELECT count(*) FROM events WHERE id LIKE 'imported.%'").fetchone()[0] == ((98 if existing else 20) if history else 0)
            d = CardJobs(library,config).status(digest(raw))
            assert d["cards"]["A"]["spec"]["introduction"] == ("989" if existing else "500")
            assert d["cards"]["A"]["character"]["appearance"]["height_cm"] is None
            assert d["cards"]["B"]["character"]["appearance"]["height_cm"] == (177 if existing else None)
            if not existing:
                with library.db() as db:
                    assert db.execute("SELECT COUNT(*) FROM profile_card_edits").fetchone()[0] == 0
            assert sorted(marker.roles)==["A","B"] and marker.compositions == 1
            assert not screen.eval("window.errors")

        except BaseException as exc:
            with store.connection() as db:
                report=[dict(row) for row in db.execute("""SELECT t.status,t.error_code,p.state publication,p.reason,
                    coalesce(json_extract(t.origin_json,'$.history_presentation'),0) history_presentation
                    FROM turns t LEFT JOIN expression_speech_units u ON u.turn_id=t.id
                    LEFT JOIN expression_speech_publications p ON p.utterance_id=u.id
                    WHERE t.source_key NOT LIKE 'imported.%' ORDER BY t.rowid""")]
                print("Native turns",json.dumps(report,ensure_ascii=False),flush=True)
            failures.append(exc)
        finally:
            window.destroy()
    try:
        with patch("persona_runtime.corpus_learning_transport.CorpusLearningHTTPClient.complete", marker.extract):
            webview.start(flow, gui="edgechromium", private_mode=True, storage_path=str(home/"browser"))
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(35)
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=10)
        loop.close()
    if failures:
        raise failures[0]
    # Read the committed structure-33 version in its own interpreter, then reopen the v2 draft.
    if baseline is not None and not history:
        check_baseline(home, baseline, existing)
        assert CardJobs(library,config).status(digest(raw))["cards"]["A"]["spec"]["introduction"] == ("989" if existing else "500")
    if history:
        check_restart(home,existing)
    print(json.dumps({"webview": "edgechromium", "schema": 33, "reopened": history, "case": "existing" if existing else "empty",
          "roles": marker.roles, "compositions": marker.compositions, "created": 2, "history": history, "chat": bool(marker.replies),
          "exact_reference":history,"withdrawal_before_offer":history,"legacy_adopted":bool(history and existing),
          "template":prompt_bundle_status(library)["source"], "conflict_resolved":existing, "direct_save":not existing,
          "old_reader": baseline is not None and not history, "provider_calls": 0}), flush=True)


def check_baseline(home, baseline, existing):
    probe = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from persona_runtime.import_history import page
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
store = Store(RuntimePaths(Path(sys.argv[2])))
people = RuntimeService(store).list_personas()
assert len(people) == int(sys.argv[3])
with store.connection() as db:
    assert db.execute("PRAGMA user_version").fetchone()[0] == 33
    assert db.execute("SELECT count(*) FROM events WHERE id LIKE 'imported.%'").fetchone()[0] == 0
    from persona_runtime.character import head
    heights = [head(db,p["id"])["data"]["appearance"]["height_cm"] for p in people]
    assert heights.count(177) == int(sys.argv[4]) and heights.count(None) == len(people)-int(sys.argv[4])
"""
    assert (baseline/"persona_runtime"/"import_history.py").is_file()
    subprocess.run([sys.executable, "-B", "-I", "-X", "utf8", "-c", probe, str(baseline), str(home),
                    str(3 if existing else 2), str(int(existing))], check=True, timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing", action="store_true")
    parser.add_argument("--with-history", action="store_true")
    parser.add_argument("--reopen-home",type=Path)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    args.temp_root.mkdir(parents=True, exist_ok=True)
    if args.reopen_home:
        reopen_history(args.reopen_home,args.existing)
    else:
        with tempfile.TemporaryDirectory(dir=args.temp_root, prefix="workspace-") as directory:
            exercise(Path(directory), args.existing, args.baseline, history=args.with_history)
