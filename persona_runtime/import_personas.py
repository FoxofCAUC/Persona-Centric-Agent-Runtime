"""Atomically create selected identities and manage their private style bindings."""
import json

from .archive import fingerprint
from .import_analysis import ImportAnalysis
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
            history_rows=assign(parse(row["raw"]),json.loads(row["mapping"]),json.loads(row["overrides"]))
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
                version,material=self.library.edition(ident,revision,role)
                binding=self.binding({},version,material,enable_style)
                pid=RuntimeService.insert_persona(db,PersonaSpec.from_dict(material["spec"]))
                if material["relationship"]:
                    append_version(db,pid,"relationship",{"description":material["relationship"]},actor="user")
                if continue_history:
                    from .import_history import restore
                    restore(db,pid,ident,role,history_rows)
                save(db,style.PREFIX+pid,binding)
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
            return value|{"revoked":bool(read(db,style.REVOKED+value["document"])),
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
            self.library.row(ident)
            save(db,style.REVOKED+ident,True)
        await self.analysis.cancel(ident)
        with self.library.db() as db:
            db.execute("UPDATE documents SET status='revoked',revision=revision+1 WHERE id=? AND status!='revoked'",(ident,))
        return self.library.status(ident)
