"""Explicit, isolated AstrBot reference adapter; never imported by production.

Third-party source and dependencies live outside Git. This adapter exercises the
upstream persona/agent/hooks/decorate/respond code with an injected provider and
a capturing private-message event. It does not log in to QQ or load credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

UPSTREAM_COMMIT = "fef1e99d20381423145455b278488d951ec1de1e"
ASTRBOT_TAG = "v4.29.0"
VERSION = "dafeiyu-text-reference-v1"
PATCHES = {
    "respond_stage.py": "astrbot/core/pipeline/respond/stage.py",
    "stage.py": "astrbot/core/pipeline/result_decorate/stage.py",
    "chat_layout.py": "astrbot/core/pipeline/chat_layout.py",
}


def source_status(root: Path) -> dict:
    """Inspect installed public source without importing its runtime."""
    root = root.resolve()
    failures = []
    counts = {}
    for name, expected in (("upstream", UPSTREAM_COMMIT), ("astrbot", ASTRBOT_TAG)):
        manifest_path = root / (name + "-files.json")
        if not manifest_path.is_file():
            failures.append(name + "_source_manifest_missing")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("ref") != expected:
            failures.append(name + "_source_version_mismatch")
        checked = 0
        for item in manifest["files"]:
            path = root / name / item["path"]
            if name == "astrbot" and item["path"] in PATCHES.values():
                # These three files intentionally use the pinned dafeiyu patch.
                patch_name = next(k for k, v in PATCHES.items() if v == item["path"])
                patch = root / "upstream/deploy/patches" / patch_name
                if path.is_file() and patch.is_file() and path.read_bytes() == patch.read_bytes():
                    checked += 1
                    continue
            if not path.is_file():
                failures.append(name + ":missing:" + item["path"])
                continue
            data = path.read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if actual != item["sha"]:
                failures.append(name + ":source_changed:" + item["path"])
            checked += 1
        counts[name] = checked
    patches_applied = all((root / "astrbot" / target).is_file() and
        (root / "upstream/deploy/patches" / name).is_file() and
        (root / "astrbot" / target).read_bytes() ==
        (root / "upstream/deploy/patches" / name).read_bytes()
        for name, target in PATCHES.items())
    return {"version": VERSION, "source_files": counts, "failures": failures,
            "text_patches_applied": patches_applied,
            "source_ready": not failures, "actual_qq": False,
            "semantic_quality": "not_evaluated"}


def apply_text_patches(root: Path) -> None:
    """Install the three declared text patches in the isolated reference only."""
    status = source_status(root)
    if not status["source_ready"]:
        raise ValueError("reference_source_not_ready")
    for name, target in PATCHES.items():
        shutil.copyfile(root / "upstream/deploy/patches" / name, root / "astrbot" / target)


def _setup(root: Path, runtime: Path, *, owned_home: Path | None = None) -> None:
    if "astrbot.core" in sys.modules:
        raise RuntimeError("reference_requires_fresh_process")
    status = source_status(root)
    if not status["source_ready"] or not status["text_patches_applied"]:
        raise ValueError("reference_source_not_ready")
    if owned_home is None:
        if runtime.resolve() == root.resolve() or root.resolve() not in runtime.resolve().parents:
            raise ValueError("reference_runtime_outside_owned_root")
    else:
        # Explicit disposable-copy ownership keeps private upstream databases
        # inside the same already-reviewed cleanup boundary as the chat copy.
        from persona_runtime.private_personas import safe
        owned_home = safe(owned_home)
        marker_file = owned_home / 'private-sandbox.json'
        if marker_file.is_file():
            marker = json.loads(marker_file.read_text(encoding='utf-8'))
            formal_home = Path(marker['formal_home']).resolve()
            if owned_home.parent != formal_home / 'tmp/private-acceptance':
                raise ValueError('reference_private_runtime_owner_invalid')
        else:
            trial = json.loads((owned_home.parent / 'trial.json').read_text(encoding='utf-8'))
            formal_home = Path(trial['source_home']).resolve()
            if (owned_home.name != 'artificial-home' or trial.get('version') != 'native-reference-trial-v1'
                    or trial.get('development_material') is not True or trial.get('status') != 'prepared'
                    or Path(trial['home']).resolve() != owned_home
                    or owned_home.parent.parent != formal_home / 'tmp/companion-native-reference'
                    or Path(trial['reference_root']).resolve() != root):
                raise ValueError('reference_artificial_runtime_owner_invalid')
        safe(runtime, owned_home / 'runtime/native-reference/upstream')
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRBOT_ROOT"] = str(runtime.resolve())
    # Prevent the media plugin from discovering ambient credentials.
    os.environ.pop("DASHSCOPE_API_KEY", None)
    sys.path.insert(0, str(root / "astrbot"))


async def run_reference(root: Path, *, persona: str, user: str, history: list[dict],
                        complete, output_contract: str = "",
                        runtime_name: str = "runtime-text", owned_home: Path | None = None) -> dict:
    """Run real upstream text code once; ``complete(messages)`` owns generation.

    Caller supplies approved visible persona and source-scoped history. The
    callback must own billing, retries and cancellation; AstrBot gets one logical
    provider dispatch and cannot silently regenerate a poor answer.
    """
    if not all(isinstance(value, str) for value in (persona, user, output_contract)):
        raise ValueError("reference_text_required")
    if not isinstance(history, list) or any(type(item) is not dict or
            set(item) != {"role", "content"} or item["role"] not in ("user", "assistant") or
            not isinstance(item["content"], str) for item in history):
        raise ValueError("reference_history_invalid")
    root = root.resolve()
    if owned_home is not None:
        if not runtime_name or Path(runtime_name).name != runtime_name or runtime_name in ('.', '..'):
            raise ValueError('reference_private_runtime_name_invalid')
        runtime = Path(owned_home) / 'runtime/native-reference/upstream' / runtime_name
    else:
        runtime = root / runtime_name
    _setup(root, runtime, owned_home=owned_home)
    import astrbot.core as core
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.astr_main_agent import MainAgentBuildConfig, build_main_agent
    from astrbot.core.db.po import Conversation
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.context_utils import call_handler
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
    from astrbot.core.provider.entities import LLMResponse, ProviderMeta, ProviderRequest
    from astrbot.core.provider.provider import Provider
    from astrbot.core.star.star import star_map
    from astrbot.core.star.context import Context
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    core.logger.setLevel(logging.ERROR)
    await core.db_helper.initialize()
    stages = []
    captured = []
    provider_inputs = []

    class CaptureEvent(AstrMessageEvent):
        async def send(self, message):
            captured.append([json.loads(c.json(exclude_none=True)) for c in message.chain])
            self._has_send_oper = True

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "reference-bot"
    message.sender = MessageMember(user_id="reference-user", nickname="人工用户")
    message.message = [Plain(user)]
    message.message_str = user
    message.message_id = "reference-message"
    message.session_id = "reference-session"
    message.raw_message = {}
    meta = PlatformMetadata("aiocqhttp", "captured reference, no QQ connection", "reference",
                            support_streaming_message=False, support_proactive_message=False)
    event = CaptureEvent(user, message, meta, "reference-session")
    event.is_at_or_wake_command = True
    event.plugins_name = ["astrbot_plugin_face_convert"]

    config = copy.deepcopy(dict(core.astrbot_config))
    config["provider_settings"].update(web_search=False, identifier=False,
        datetime_system_prompt=False, computer_use_runtime="none", default_personality="reference")
    config["provider_tts_settings"]["enable"] = False
    config["provider_ltm_settings"] = {}
    config["t2i"] = False
    config["content_safety"]["also_use_in_response"] = False
    config["platform_settings"].update(reply_prefix="", reply_with_mention=False, reply_with_quote=False)
    config["platform_settings"]["segmented_reply"].update(enable=True, only_llm_result=True,
        interval="0,0", interval_method="random")

    class EmptyTools:
        def get_full_tool_set(self): return ToolSet()

    acm = SimpleNamespace(default_conf=config, get_conf=lambda *a, **kw: config)
    persona_manager = PersonaManager(core.db_helper, acm)
    persona_manager.personas_v3 = [{"name": "reference", "prompt": persona, "tools": [],
        "skills": [], "custom_error_message": None, "_begin_dialogs_processed": []}]
    class ReferenceContext(Context):
        def __init__(self):
            self.persona_manager = persona_manager
            self.subagent_orchestrator = None
            self.kb_manager = SimpleNamespace()
        def get_config(self, *a, **kw): return config
        def get_llm_tool_manager(self): return EmptyTools()
        async def get_using_tts_provider_async(self, *a, **kw): return None
    ctx = ReferenceContext()

    plugin_path = root / "upstream/plugin/astrbot_plugin_face_convert/main.py"
    module_name = "pcar_reference_face_convert"
    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    plugin = module.FaceConvertPlugin(ctx)
    star_map[module_name].star_cls = plugin
    for handler in star_handlers_registry.get_handlers_by_module_name(module_name):
        handler.handler = getattr(plugin, handler.handler_name)

    class CallbackProvider(Provider):
        def __init__(self):
            super().__init__({"id": "reference", "type": "pcar_callback", "max_context_tokens": 128000}, {})
            self.set_model("configured-by-caller")
        def get_current_key(self): return ""
        def set_key(self, key): raise RuntimeError("reference_does_not_store_credentials")
        async def get_models(self): return [self.get_model()]
        def meta(self): return ProviderMeta("reference", self.get_model(), "pcar_callback")
        async def text_chat(self, **kwargs):
            if provider_inputs: raise RuntimeError("reference_unexpected_provider_repeat")
            messages = []
            for item in kwargs["contexts"]:
                value = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
                content = value.get("content", "")
                if isinstance(content, list):
                    if any(p.get("type") != "text" for p in content):
                        raise ValueError("reference_text_only")
                    content = "".join(p["text"] for p in content)
                messages.append({"role": value["role"], "content": content})
            provider_inputs.append(messages)
            stages.append("provider_callback")
            result = await complete(messages)
            if not isinstance(result, str) or not result.strip():
                raise ValueError("reference_empty_callback_result")
            self.raw_text = result
            return LLMResponse(role="assistant", completion_text=result)

    provider = CallbackProvider()
    conversation = Conversation(platform_id="reference", user_id="reference-user",
        cid="reference-conversation", persona_id="reference")
    request = ProviderRequest(prompt=user, contexts=copy.deepcopy(history),
        system_prompt=output_contract, conversation=conversation)
    try:
        async for _ in call_handler(event, plugin.inbound_face_to_text): pass
        stages.append("inbound_hooks")
        built = await build_main_agent(event=event, plugin_context=ctx, provider=provider, req=request,
            config=MainAgentBuildConfig(tool_call_timeout=30, streaming_response=False,
                llm_safety_mode=False, add_cron_tools=False, request_max_retries=1,
                provider_settings=config["provider_settings"]))
        if built is None: raise RuntimeError("reference_build_rejected")
        stages.append("build_main_agent")
        async for response in built.agent_runner.step_until_done(max_step=1):
            pass
        final = built.agent_runner.final_llm_resp
        if final is None: raise RuntimeError("reference_agent_not_completed")
        stages.append("agent_and_response_hooks")
        after_hook = final.completion_text
        native_history = []
        skipped_initial_system = False
        for message in built.agent_runner.run_context.messages:
            if message.role == 'system' and not skipped_initial_system:
                skipped_initial_system = True
                continue
            if message.role in ('assistant', 'user') and getattr(message, '_no_save', False):
                continue
            if message.role not in ('assistant', 'user'):
                raise RuntimeError('reference_native_history_non_text_role')
            content = message.content
            if isinstance(content, list):
                pieces = [part.model_dump(exclude_none=True) if hasattr(part, 'model_dump') else dict(part) for part in content]
                if any(part.get('type') != 'text' for part in pieces):
                    raise RuntimeError('reference_native_history_non_text_content')
                content = ''.join(part.get('text', '') for part in pieces)
            if not isinstance(content, str):
                raise RuntimeError('reference_native_history_non_text_content')
            native_history.append({'role': message.role, 'content': content})
        event.set_result(MessageEventResult(chain=[Plain(after_hook)], result_content_type=ResultContentType.LLM_RESULT))
        pipeline_context = PipelineContext(config, SimpleNamespace(context=ctx), "reference", core.db_helper)
        decorate, respond = ResultDecorateStage(), RespondStage()
        await decorate.initialize(pipeline_context)
        await respond.initialize(pipeline_context)
        async for _ in decorate.process(event): pass
        stages.append("result_decorate_hooks")
        processed = event.get_result().get_plain_text() if event.get_result() else ""
        await respond.process(event)
        if processed.strip() and not captured:
            raise RuntimeError("reference_delivery_not_captured")
        stages.append("respond_capture")
        suppressed = (not captured and not processed.strip()
            and bool(plugin.NO_REPLY_RE.fullmatch(after_hook.strip())))
        return {"version": VERSION, "upstream_commit": UPSTREAM_COMMIT, "astrbot_tag": ASTRBOT_TAG,
            "stages": stages, "messages": provider_inputs[0], "raw_text": provider.raw_text,
            "after_response_hook": after_hook, "processed_text": processed, "captured_messages": captured,
            "native_history": native_history,
            "suppressed": suppressed,
            "suppression_reason": "upstream_no_reply_marker" if suppressed else None,
            "provider_dispatches": len(provider_inputs), "actual_qq": False,
            "provider_adapter": "caller callback; not native OpenAI SDK",
            "config_overrides": {"tools": False, "web_search": False, "skills": False,
                "tts": False, "display_streaming": False, "bubble_wait_seconds": 0,
                "caller_output_contract": bool(output_contract)},
            "memory": "empty; external LivingMemory disabled", "proactive": False,
            "semantic_quality": "not_evaluated"}
    finally:
        await core.db_helper.engine.dispose()


async def _none(): return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "install-text-patches", "simulate"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "install-text-patches":
        apply_text_patches(args.root)
        result = source_status(args.root)
    elif args.action == "status": result = source_status(args.root)
    else:
        async def fake(messages):
            return '[{"type":"text","text":"前半段弄明白了。"},{"type":"text","text":"剩下那点明天再看。"}]'
        from unittest.mock import patch
        async def offline():
            with patch("socket.socket.connect", side_effect=RuntimeError("reference_simulation_network_blocked")):
                return await run_reference(args.root, persona="你是人工角色。对过程和条件有自己的关注，普通朋友关系。",
                    user="今天只搞定了前半段，先歇会儿。", history=[], complete=fake)
        result = asyncio.run(offline())
        result["generation_kind"] = "simulated_callback"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in {"messages", "raw_text", "after_response_hook", "processed_text", "captured_messages", "native_history"}}, ensure_ascii=False))


if __name__ == "__main__": main()
