"""Atomically create selected identities and manage their private style bindings."""
import asyncio
import json

from .archive import fingerprint
from .import_analysis import ImportAnalysis
from .import_profile_workspace import ProfileWorkspace
from .import_card_job import CardJobs
from .import_card_publish import CardPublisher
from .import_library import require
from . import import_style as style
from .models import PersonaSpec
from .persona_management import active
from .profile import append_version
from .scene_policy import read, save
from .service import RuntimeService


class PersonaImports:
    def __init__(self, store, config, transport=None):
        self.store = store
        self.analysis = ImportAnalysis(store.paths,config,transport)
        self.library = self.analysis.library
        self.profiles = ProfileWorkspace(self.library, config, transport)

        self.cards = CardJobs(self.library, config, transport)
        self.card_publisher = CardPublisher(store, self.library, self.cards)
        from .import_history_service import HistoryService
        self.history = HistoryService(store, self.library, self.card_publisher)
        self._card_chains = {}

    def analyze(self, ident, revision, confirmed, plan_id=None, *, cached_only=False, compose_card=False):
        require(type(compose_card) is bool and (not compose_card or (plan_id and confirmed is True and not cached_only)),
                "需要确认本次导入的成卡用途。")
        if plan_id is None:
            return self.analysis.start(ident, revision, confirmed, cached_only=cached_only)
        result = self.profiles.start(ident, revision, plan_id, confirmed, cached_only=cached_only)
        if compose_card:
            self._card_chains[ident] = asyncio.create_task(
                self._after_analysis(ident, plan_id, self.profiles.tasks[ident]))
        return result

    async def _after_analysis(self, ident, plan_id, analysis_task):
        try:
            await asyncio.shield(analysis_task)
            data = self.library.status(ident)
            if data["status"] in ("ready","partial_ready") and (data["plan"] or {}).get("id") == plan_id:
                self.cards.start(ident=ident, revision=data["revision"], plan_id=plan_id, confirmed=True)
        except (OSError, ValueError):
            # Card availability, review requirements and capacity are exposed by card_status.
            pass
        finally:
            if self._card_chains.get(ident) is asyncio.current_task():
                self._card_chains.pop(ident, None)

    async def cancel(self, ident):
        chain = self._card_chains.pop(ident, None)
        if chain:
            chain.cancel()
            await asyncio.gather(chain, return_exceptions=True)
        await self.cards.cancel(ident)
        if ident in self.profiles.tasks:
            return await self.profiles.cancel(ident)
        return await self.analysis.cancel(ident)

    async def drain(self):
        chains = tuple(self._card_chains.values())
        for chain in chains:
            chain.cancel()
        await asyncio.gather(*chains, return_exceptions=True)
        await self.cards.drain()
        await self.profiles.drain()
        await self.analysis.drain()

    def create_cards(self, ident, revision, plan_id, card_revision, roles, operation_id, confirmed, enable_scene=False):
        require(confirmed is True, "请确认已审阅所选角色卡。")
        return self.card_publisher.create(ident=ident, revision=revision, plan_id=plan_id,
                                          card_revision=card_revision, roles=roles,
                                          operation_id=operation_id, enable_scene=enable_scene, confirmed=confirmed)

    def binding(self, old, version, material, enabled):
        require(type(enabled) is bool,"启用状态无效。")
        checksum=None
        if enabled:
            _, checksum=self.library.contract("style")
            require(material["samples"],"当前版本还没有表达样例。")
        preferences=dict(old.get("preferences",{}))
        if old:
            preferences[old["version"]]={"disabled":old["disabled"],"demoted":old["demoted"]}
        saved=preferences.get(version,{"disabled":[],"demoted":{}})
        return {"instance":str(self.store.paths.home.resolve()),"document":material["document"],"version":version,
                "role":material["role"],"contract":checksum,"enabled":enabled,"preferences":preferences,**saved,
                "versions":list(dict.fromkeys(old.get("versions",[])+[version])),"revision":old.get("revision",0)+1}

    def index(self, version, material):
        try:
            style.prepare_index(self.library,version,material["samples"],style.provider_for(self.store.paths))
        except (OSError,ValueError,RuntimeError):
            return False
        return True

    def create(self, ident, revision, roles, enable_style, operation_id, continue_history=False):
        require(isinstance(roles,list) and 1<=len(roles)<=2 and len(set(roles))==len(roles)
                and set(roles)<={"A","B"} and isinstance(operation_id,str) and 0<len(operation_id)<=128,
                "请选择本次创建的人物。")
        key=style.PREFIX+"create."+operation_id
        require(type(continue_history) is bool,"历史导入选项无效。")
        signature=fingerprint([ident,revision,roles,enable_style,continue_history])
        history_rows=None
        if continue_history:
            from .import_markdown import parse, assign
            row=self.library.row(ident)
            with self.library.db() as private:
                has_plan = private.execute("SELECT 1 FROM profile_current WHERE document=?", (ident,)).fetchone()
            if has_plan:
                from .import_plan import parse_source
                parsed = parse_source(row["raw"])
            else:
                parsed = parse(row["raw"])
            history_rows=assign(parsed,json.loads(row["mapping"]),json.loads(row["overrides"]))
        editions={}
        ids=[]
        with self.store.connection(write=True) as db:
            prior=read(db,key)
            if prior:
                require(prior["request"]==signature,"重复请求内容已变化。")
                return prior["people"]
            require(not read(db,style.REVOKED+ident),"来源已撤回。")
            for role in roles:
                role_key=style.PREFIX+"created."+fingerprint([ident,role])
                require(not read(db,role_key),"这个说话人已创建角色。")
                version,material=self.library.edition(ident,revision,role,legacy_create=True)
                binding=self.binding({},version,material,enable_style)
                pid=RuntimeService.insert_persona(db,PersonaSpec.from_dict(material["spec"]))
                if material["relationship"]:
                    append_version(db,pid,"relationship",{"description":material["relationship"]},actor="user")
                if continue_history:
                    from .import_history import restore
                    restore(db,pid,ident,role,history_rows)
                save(db,style.PREFIX+pid,binding)
                if material.get("adopted"):
                    save(db,style.PREFIX+"profile_origin."+pid, {"version":version,"document":ident})
                save(db,role_key,pid)
                ids.append(pid)
                editions[version]=material
            save(db,key,{"request":signature,"people":ids})
        for pid in ids:
            callback=getattr(self.store,"character_committed",None)
            if callback:
                callback(pid)
        if enable_style:
            for version,material in editions.items():
                self.index(version,material)
        return ids

    def info(self, persona_id):
        with self.store.connection() as db:
            active(db,persona_id)
            value=read(db,style.PREFIX+persona_id)
            if value is None:
                return None
            revoked = bool(read(db, style.REVOKED + value["document"]))
            affected = []
            origin = read(db, style.PREFIX+"profile_origin."+persona_id)
            profile_revoked = bool(origin and read(db, style.REVOKED+origin["document"]))
            if profile_revoked:
                with self.library.db() as private:
                    edition = private.execute("SELECT payload FROM editions WHERE id=?", (origin["version"],)).fetchone()
                if edition:
                    adopted = json.loads(edition["payload"]).get("adopted", {})
                    affected = sorted({item["field"] for item in adopted.values()})
            return value|{"revoked":revoked,"profile_source_revoked":profile_revoked,"affected_fields":affected,
                          "feedback":read(db,style.PREFIX+"feedback."+persona_id) or []}

    def update(self, persona_id, expected_revision, enabled, version=None, ident=None, revision=None, role=None):
        with self.store.connection(write=True) as db:
            active(db,persona_id)
            key=style.PREFIX+persona_id
            old=read(db,key)
            require(old and old["revision"]==expected_revision,"人物风格版本已变化。")
            if ident is not None:
                require(role in ("A","B"),"请选择追加记录中的人物。")
                version,material=self.library.edition(ident,revision,role)
            elif version is None and not enabled:
                save(db,key,old|{"enabled":False,"revision":expected_revision+1})
                return {"enabled":False}
            else:
                version=version or old["version"]
                require(version in old["versions"],"此版本不属于当前人物。")
                material=self.library.material(version)
            require(not read(db,style.REVOKED+material["document"]),"来源已撤回。")
            new=self.binding(old,version,material,enabled)
            if version==old["version"]:
                new.update(disabled=old["disabled"],demoted=old["demoted"])
            save(db,key,new)
        return {"enabled":enabled,"indexed":enabled and self.index(version,material)}

    def feedback(self, persona_id, turn_id, mode=None, sample=None):
        with self.store.connection(write=mode is not None) as db:
            active(db,persona_id)
            row=db.execute("""SELECT u.details_json FROM turns t JOIN context_uses u ON u.turn_id=t.id
                WHERE t.id=? AND t.persona_id=? AND t.channel='desktop' AND t.reply_event_id IS NOT NULL
                ORDER BY u.rowid DESC LIMIT 1""",(turn_id,persona_id)).fetchone()
            require(row,"找不到已呈现的回复。")
            proof=json.loads(row[0]).get(style.KEY) or {}
            policy=read(db,style.PREFIX+persona_id)
            require(policy,"该人物未绑定导入语料。")
            samples=[]
            if proof.get("version") in policy["versions"]:
                try:
                    material=self.library.material(proof["version"])
                    if not read(db,style.REVOKED+material["document"]):
                        samples=[s for s in material["samples"] if s["id"] in proof.get("ids",[])]
                except ValueError:
                    pass
            if mode:
                require(mode in ("review","demote","disable"),"反馈操作无效。")
                if mode!="review":
                    require(policy["version"]==proof.get("version") and sample in [s["id"] for s in samples],"样例版本已变化。")
                    ids=policy["disabled"] if mode=="disable" else policy["demoted"].setdefault(proof["act"],[])
                    if sample not in ids:
                        ids.append(sample)
                        policy["revision"]+=1
                        save(db,style.PREFIX+persona_id,policy)
                key=style.PREFIX+"feedback."+persona_id
                log=read(db,key) or []
                item={"turn":turn_id,"mode":mode,"sample":sample,"version":proof.get("version")}
                if item not in log:
                    save(db,key,log+[item])
            return samples

    async def revoke(self, ident):
        with self.store.connection(write=True) as db:
            with self.library.db() as private:
                require(private.execute("SELECT 1 FROM documents WHERE id=?", (ident,)).fetchone(), "未找到导入记录。")
                private.execute("UPDATE documents SET status='revoked',revision=revision+1 WHERE id=? AND status!='revoked'", (ident,))
            save(db,style.REVOKED+ident,True)
        await self.cancel(ident)
        return self.library.status(ident)
