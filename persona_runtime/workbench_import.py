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
        self._client=LocalClient(self._config,bind_instance=True)
        from .workbench_submissions import WorkbenchSubmissions
        self._submissions = WorkbenchSubmissions(self._config, self._client)
        from .workbench_presentation import WorkbenchPresentation
        self._presentation = WorkbenchPresentation(self._config,self._client)
        self._library=ImportLibrary(paths)
        self._window=window
        self._selection_lock=RLock()
        self._repair_status=None
        self._client.write_check=self._require_open

    def _require_open(self):
        from .local_repair import require_open,RepairInProgress,ACTIVE
        if self._repair_status:
            record=self._repair_status()
            if record and record['phase'] in ACTIVE:raise RepairInProgress()
        require_open(self._config.paths,self._client.expected_instance(self._config.load()))

    def pick_transcript(self):
        self._require_open()
        from webview import FileDialog
        names=self._window().create_file_dialog(FileDialog.OPEN,allow_multiple=False,file_types=("Markdown (*.md)",))
        if not names:
            return None
        require(names[0].lower().endswith(".md"),"请选择 Markdown 文件。")
        raw=read_supplied(names[0]);self._require_open()
        return self._library.add(raw)

    def import_prompt_file(self):
        self._require_open()
        from webview import FileDialog
        from .import_prompts import import_prompt_file
        names = self._window().create_file_dialog(
            FileDialog.OPEN, allow_multiple=False, file_types=("PCAR 提示词 (*.json)",))
        self._require_open()
        return import_prompt_file(self._library, names[0]) if names else None

    def import_action(self, name, args):
        require(name in ("prompt_status","list","replay","analyze","status","cancel","evidence","prepare","edit","create","style","feedback",
                         "card_preview","card_status","card_compose","card_edit","card_create","card_scene_status","card_scene_enable",
                         "update","revoke","history","history_preview","history_commit","history_status","history_update","plan","plan_evidence","plan_archive","review","review_all"),"未知导入操作。")
        pid = args.get("persona_id")
        if pid and name in ("history_preview","history_update","update","card_scene_enable"):
            self.live_activate(pid)
        return self._client.call("POST","/v1/manage",{"action":"persona.import."+name,"args":args},timeout=180)

    def live_health(self):
        from .local_repair import read,public,ACTIVE
        from .maintenance import storage_fault_pending
        repair=self._repair_status() if self._repair_status else public(read(self._config.paths))
        if repair and repair['phase'] in ACTIVE:return {'state':'repairing','repair':repair}
        if storage_fault_pending(self._config.paths):
            return {'state':'repairing','repair':{'phase':'read_only','action':'check_storage','reason':'storage_unavailable'}}
        cfg=self._config.load()
        from .desktop_client import LocalAPIError, TransportUnknown
        try:
            status=self._client.call("GET","/v1/health?view=desktop",timeout=2)
        except TransportUnknown:
            return {"state":"disconnected",**({"repair":repair} if repair else {})}
        except LocalAPIError as error:
            return {"state":"wrong_instance" if error.status in (401,403,409) else "disconnected",**({"repair":repair} if repair else {})}
        if status.get("instance_id")!=cfg["instance_id"] or status.get("service")!="persona-runtime":
            return {"state":"wrong_instance"}
        return {"state":"online","generation":status.get("service_generation"),"configured":bool(cfg["model"]["encrypted_key"] and cfg["model"]["base_url"]),
                "model":cfg["model"]["name"],"configVersion":str(self._config.file.stat().st_mtime_ns),
                "draining":bool(status.get("draining")),**({"quotaWaits":status["quota_waits"]} if status.get("quota_waits") else {}),
                **({"maintenance":status["maintenance"]} if status.get("maintenance") else {}),
                **({"repair":repair} if repair else {})}

    def live_quota_settings(self,settings=None):
        if settings is None:return self._client.manage('quota.settings.current')
        return self._client.manage('quota.configure',**settings)

    def live_backups(self):
        from .automatic_backup import Backups
        self._require_open()
        return Backups(self._config.paths).status()

    def restore_backup_copy(self,name=None):
        from pathlib import Path
        from webview import FileDialog
        from .automatic_backup import Backups
        from .models import identifier
        self._require_open()
        selection=self._window().create_file_dialog(FileDialog.FOLDER,allow_multiple=False)
        if not selection:return None
        self._require_open()
        target=Path(selection[0])/('PCAR-恢复核对-'+identifier()[:8])
        return Backups(self._config.paths).restore(target,name)

    def live_quota_decide(self,ids,allow):
        return self._client.manage('quota.workflow.decide',ids=ids,allow=allow)

    def live_activate(self,pid):
        self._client.manage("persona.use",persona_id=pid)

    def live_network_wake(self):
        return self._client.manage('desktop.network.wake')

    def live_people(self):
        config=self._config.public()["model"]
        return {"people":self._client.manage("persona.list"),"connected":bool(config.get("has_key") and config.get("base_url")),
                "model":config["name"]}

    def live_select(self,pid,fresh=False,view_session_id=None):
        self._require_open()
        with self._selection_lock:
            if fresh:
                require(view_session_id is None,"浏览已保存会话时无需新建。")
                self._client.manage("session.select",channel="desktop",fresh=True,persona_id=pid)
            return self._client.manage("desktop.browser.show",persona_id=pid)

    def live_snapshot(self,sid,version=None):
        return self._client.manage("chat.snapshot",session_id=sid,version=version,include_imports=False)

    def live_page(self,pid,sid,version=None,before=None,after=None,anchor=None):
        return self._client.manage("desktop.timeline.snapshot",pid=pid,sid=sid,version=version,before=before,after=after,anchor=anchor)

    def live_retrieved_notes(self,pid,sid,tid):
        return self._client.manage("desktop.retrieval.notes",pid=pid,sid=sid,tid=tid)

    def live_location(self,pid=None,sid=None,position=None):
        return self._submissions.location(pid,sid,position)

    def live_submit(self,pid,sid,text,request_id,reference=None,draft_revision=None,public_search=False):
        self._require_open()
        if public_search is not False:
            from .knowledge_native import public_task
            chosen=text if public_search is True else public_search
            public=public_task({'question':chosen,'intent':'search','request_search':True})
            if public['question'] not in text:raise ValueError('native_public_task_scope_changed')
        first=not sid
        if not sid:
            sid=self._client.manage("session.select",persona_id=pid,channel="desktop")["session_id"]
            self._submissions.first_session(pid,sid)
        payload = {"persona_id":pid, "session_id":sid, "channel":"desktop", "text":text,
                   "request_id":request_id, "desktop_presentation":CONTRACT}
        if reference is not None:
            payload["reference"] = reference
        if public_search is not False:payload['public_task']=public
        result=self._submissions.submit(payload, draft_revision)
        if first and result["state"]=="accepted" and draft_revision:
            empty={"previous":draft_revision,"revision":request_id,"text":"","reference":None}
            self._submissions.draft(pid,sid,empty)
        return result|{"session_id":sid}

    def live_submissions(self, pid, sid):
        return self._submissions.reconcile(pid, sid)

    def live_draft(self, pid, sid, draft=None):
        return self._submissions.draft(pid, sid, draft)

    def live_ack_submissions(self, pid, sid, ids):
        self._submissions.acknowledge(pid, sid, ids)

    def live_cancel(self,tid):
        turn=self._client.call("GET","/v1/requests/"+tid)
        return self._client.cancel_request(turn)

    def live_present(self,action,args):
        require(action in ("chat.notice.preview","chat.notice.presented","expression.v2.offer","expression.v2.ack",
                           "expression.v2.pending_receipts"),"不支持的呈现操作。")
        return self._client.manage(action,**args)

    def presentation_attach(self, page):
        self._presentation.attach(page)

    def presentation_begin(self, page, claim_id, kind, args):
        self._require_open()
        return self._presentation.begin(page,claim_id,kind,args)

    def presentation_decide(self, page, claim_id, state):
        return self._presentation.decide(page,claim_id,state)

    def presentation_reconcile(self):
        return self._presentation.reconcile()

    def template_spec(self):
        from .models import generic_persona
        return generic_persona().to_dict()

    def live_memory(self,pid,enabled=None,expected=None,expected_provider=None):
        policy=self._client.manage("organization.policy.current",persona_id=pid)
        require(policy["persona_id"]==pid,"人物已切换。")
        config = self._config.load()
        org = config["organization"]
        model = config["model"] if org["reuse_chat"] else org["model"]
        if enabled is not None:
            from .private_personas import provider_identity
            from .models import identifier
            require(type(enabled) is bool and expected==policy["id"],"记忆政策版本已变化。")
            if expected_provider is not None:
                require(expected_provider == {"name":model["name"],"base_url":model["base_url"]}, "接收记忆资料的模型已变化。")
            data=policy["data"] | {"memory_automation":enabled}
            data["provider"] = provider_identity(model)
            if enabled:
                data.update(enabled=True,memory_backfill=True)
            self.live_activate(pid)
            self._client.manage("organization.policy.update",persona_id=pid,expected_selected=pid,
                                expected_version=expected,data=data,operation_id=identifier())
            policy=self._client.manage("organization.policy.current",persona_id=pid)
        return {"enabled":bool(policy["data"]["enabled"] and policy["data"].get("memory_automation")), "version":policy["id"], "provider":{"name":model["name"],"base_url":model["base_url"]}}

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
        person=self._client.manage("persona.create",spec=spec)
        self._client.manage("session.select",persona_id=person["id"],channel="desktop")
        self.live_activate(person["id"])
        return person

    def live_model(self,name,key):
        require(name in ("deepseek-v4-pro","deepseek-flash"),"模型选项无效。")
        model={"base_url":"https://api.deepseek.com","name":name}
        if key:
            model["api_key"]=key
        self._client.call("POST","/v1/config",{"model":model})
        return True
