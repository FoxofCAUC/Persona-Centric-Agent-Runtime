"""Explicit local WebView operations; credentials and file paths remain in the host."""
from threading import RLock

from .config import ConfigStore
from .corpus_files import read_supplied
from .desktop_client import LocalClient
from .import_library import ImportLibrary, require
from .tk_notice import CONTRACT
from .models import PersonaSpec


class WorkbenchData:
    def __init__(self, paths, window):
        self._config=ConfigStore(paths)
        self._client=LocalClient(self._config)
        self._library=ImportLibrary(paths)
        self._window=window
        self._selection_lock=RLock()

    def pick_transcript(self):
        from webview import FileDialog
        names=self._window().create_file_dialog(FileDialog.OPEN,allow_multiple=False,file_types=("Markdown (*.md)",))
        if not names:
            return None
        require(names[0].lower().endswith(".md"),"请选择 Markdown 文件。")
        return self._library.add(read_supplied(names[0]))

    def import_action(self, name, args):
        require(name in ("list","replay","analyze","status","cancel","evidence","prepare","edit","create","style","feedback",
                         "update","revoke","history"),"未知导入操作。")
        return self._client.call("POST","/v1/manage",{"action":"persona.import."+name,"args":args},timeout=180)

    def live_people(self):
        config=self._config.public()["model"]
        return {"people":self._client.manage("persona.list"),"connected":bool(config.get("has_key") and config.get("base_url")),
                "model":config["name"]}

    def live_select(self,pid,fresh=False):
        with self._selection_lock:
            self._client.manage("persona.use",persona_id=pid)
            selected=self._client.manage("session.select",channel="desktop",fresh=fresh)
            require(selected["persona_id"]==pid,"当前角色已变化。")
            return {"sessions":self._client.manage("chat.sessions"),"active":selected["session_id"],
                    "person":self._client.manage("persona.show",persona_id=pid)}

    def live_snapshot(self,sid,version=None):
        return self._client.manage("chat.snapshot",session_id=sid,version=version,include_imports=False)

    def live_submit(self,pid,sid,text,request_id):
        return self._client.submit_input({"persona_id":pid,"session_id":sid,"channel":"desktop","text":text,
                                          "request_id":request_id,"desktop_presentation":CONTRACT})

    def live_cancel(self,tid):
        turn=self._client.call("GET","/v1/requests/"+tid)
        return self._client.cancel_request(turn)

    def live_present(self,action,args):
        require(action in ("chat.notice.preview","chat.notice.presented","expression.v2.offer","expression.v2.ack",
                           "expression.v2.pending_receipts"),"不支持的呈现操作。")
        return self._client.manage(action,**args)

    def template_spec(self):
        from .models import generic_persona
        return generic_persona().to_dict()

    def live_memory(self,pid,enabled=None,expected=None):
        policy=self._client.manage("organization.policy.current")
        require(policy["persona_id"]==pid,"人物已切换。")
        if enabled is not None:
            from .models import identifier
            require(type(enabled) is bool and expected==policy["id"],"记忆政策版本已变化。")
            data=policy["data"] | {"memory_automation":enabled}
            if enabled:
                data.update(enabled=True,memory_backfill=True)
            self._client.manage("organization.policy.update",persona_id=pid,expected_selected=pid,
                                expected_version=expected,data=data,operation_id=identifier())
            policy=self._client.manage("organization.policy.current")
        return {"enabled":bool(policy["data"]["enabled"] and policy["data"].get("memory_automation")), "version":policy["id"]}

    def live_package(self):
        from webview import FileDialog
        from .desktop_character import read_package
        files=self._window().create_file_dialog(FileDialog.OPEN,allow_multiple=False,file_types=("PCAR JSON (*.json)",))
        if not files:return None
        data=read_package(files[0])
        if isinstance(data,dict) and "persona_spec" in data:
            from .character import validate_character
            require(data.get("format")=="pcar_character_v1","角色包格式无效。")
            validate_character(data["character"])
            PersonaSpec.from_dict(data["persona_spec"])
            return data
        PersonaSpec.from_dict(data)
        return {"persona_spec":data}

    def live_save_package(self,package,operation_id):
        if "character" in package:
            selected=next((p["id"] for p in self._client.manage("persona.list") if p["selected"]),None)
            return self._client.manage("character.import",persona_id=selected,package=package,operation_id=operation_id)
        return self.live_create(package["persona_spec"])

    def live_create(self,spec):
        return self._client.manage("persona.create",spec=spec)

    def live_model(self,name,key):
        require(name in ("deepseek-v4-pro","deepseek-flash"),"模型选项无效。")
        model={"base_url":"https://api.deepseek.com","name":name}
        if key:
            model["api_key"]=key
        self._client.call("POST","/v1/config",{"model":model})
        return True
