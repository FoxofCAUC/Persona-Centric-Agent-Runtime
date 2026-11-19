"""Publish reviewed import cards to native character profiles and desktop scenes."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

from . import character, import_style, scene_policy
from .archive import fingerprint
from .character_embedding import private_directory, safe_path
from .config import ConfigStore, atomic_json
from .import_library import require
from .import_card_fields import unresolved_conflicts
from .models import PersonaSpec, RuntimeErrorBase, identifier
from .persona_management import active as require_live_persona
from .private_personas import provider_identity
from .profile import append_version
from .provider_policy import require_generation_provider
from .scene_voice import SCENES
from .service import RuntimeService


ORIGIN = "imported-role-card-v1"
BINDING = "persona.import_card.published."


def _published_path(paths, version):
    require(scene_policy.digest(version), "角色卡版本无效。")
    return safe_path(paths.home, paths.home/"private"/"published-cards"/(version + ".json"))


def _published(paths, binding):
    data = json.loads(_published_path(paths, binding["material"]).read_bytes())
    require(fingerprint(data) == binding["material"] and data["document"] == binding["document"]
            and data["role"] == binding["role"], "已保存角色卡校验失败。")
    return data


def _source_current(store, db, binding):
    if scene_policy.read(db, import_style.REVOKED + binding["document"]):
        return False
    path = safe_path(store.paths.home, store.paths.home/"private/persona-import/imports.sqlite3")
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as private:
        row = private.execute("""SELECT d.status, c.plan, d.revision, COALESCE(cards.revision,0),
                               CASE WHEN COALESCE(cards.revision,0) != ? THEN cards.cards END
                               FROM documents d JOIN profile_current c ON c.document=d.id
                               LEFT JOIN profile_card_drafts cards ON cards.document=d.id
                               AND cards.plan=c.plan AND cards.source_revision=d.revision
                               WHERE d.id=? AND d.view IS NULL""",
                              (binding["card_revision"],binding["document"])).fetchone()
    if not row or row[0] == "revoked" or row[1] != binding["plan"] or row[2] != binding["source_revision"]:
        return False
    if row[3] == binding["card_revision"]:
        return True
    # The editor revisions a pair. Editing the other role leaves this role's approval valid.
    return bool(row[4] and fingerprint(json.loads(row[4]).get(binding["role"])) == binding["card_hash"])


def _providers(store, db, pid):
    config = ConfigStore(store.paths).load()
    main = config["model"]
    require_generation_provider(main)
    if main["name"] != scene_policy.PRO or not main.get("encrypted_key"):
        raise _SceneUnavailable("主回复需要已配置的 DeepSeek Pro。")
    jev = scene_policy.configured_jev(store.paths)
    from .private_personas import guard_cloud_dispatch
    guard_cloud_dispatch(store, pid, "main", main, db=db)
    guard_cloud_dispatch(store, pid, "knowledge_router", jev, db=db)
    return {"instance":config["instance_id"], "main":provider_identity(main), "jev":provider_identity(jev)}


class _SceneUnavailable(RuntimeErrorBase):
    """A scene prerequisite with a display-safe diagnostic."""


def _readiness(store, db, pid, binding, asset):
    require(not unresolved_conflicts(asset), "请先手动处理角色卡的重大冲突。")
    from .dialogue_generation import DialogueGeneration
    from .style_study import KEY as STUDY_KEY
    if not _source_current(store, db, binding):
        raise _SceneUnavailable("请核对来源与当前角色卡版本。")
    if scene_policy.head(db, pid) != (binding["character_version"],binding["profile_sha256"]):
        raise _SceneUnavailable("人物设定已变化，请重新核对场景材料。")
    jev = asset["jev"]
    required = [asset["common"], jev["instruction"], *asset["scenes"].values(), *jev["criteria"].values()]
    if (set(asset["scenes"]) != set(SCENES) or set(jev["criteria"]) != set(SCENES)
            or not all(isinstance(v, str) and v.strip() for v in required)):
        raise _SceneUnavailable("请补齐共通提示、五份场景和 Jev 判别内容。")
    policy = db.execute("""SELECT p.reply_mode,p.data_json FROM interaction_heads h
                           JOIN interaction_policies p ON p.id=h.version_id
                           WHERE h.persona_id=? AND h.channel='desktop'""", (pid,)).fetchone()
    settings = json.loads(policy["data_json"]) if policy else {}
    if (not policy or policy["reply_mode"] != "context_full"
            or (settings.get("companion") or {}).get("version") != scene_policy.BASELINE or STUDY_KEY in settings):
        raise _SceneUnavailable("场景路线需要桌面原生发言配置。")
    competing = db.execute("""SELECT 1 FROM expression_runtime_policies WHERE persona_id=?
                              AND json_extract(data_json,'$.enabled')=1""", (pid,)).fetchone()
    if competing or DialogueGeneration._policy(db,pid)["data"]["enabled"]:
        raise _SceneUnavailable("请先暂停该角色的其他发言路线。")
    elected = scene_policy.read(db, scene_policy.PREFIX + pid)
    if elected:
        raise _SceneUnavailable("该角色已绑定其他场景材料。")
    return _providers(store, db, pid)


def _available(store, db, pid, binding, asset):
    try:
        return _readiness(store, db, pid, binding, asset), None
    except _SceneUnavailable as exc:
        return None, str(exc)
    except (ValueError, OSError, sqlite3.Error) as exc:
        if str(exc) in ("jev_configuration_required", "scene_jev_model_changed"):
            return None, "请配置并授权 Jev 1.14 场景判别连接。"
        return None, "请核对模型连接、本机资料和该角色的用途授权。"


def imported_scene_available(store, db, pid, policy):
    """Recheck the confirmed source and provider identities before freezing a new turn."""
    binding = scene_policy.read(db, BINDING + pid)
    if not binding or binding["material"] != policy.get("source_card"):
        return False
    try:
        asset = _published(store.paths, binding)
        providers = _readiness(store, db, pid, binding, asset)
        if any(policy.get(k) != value for k, value in providers.items()):
            return False
        document, directory = scene_policy.material(store.paths, policy)
        for scene in SCENES:
            body = asset["common"] + "\n\n" + asset["scenes"][scene]
            if document["cards"][scene] != fingerprint(body) or (directory/(scene+".withdrawn")).exists():
                return False
            path = scene_policy.safe(directory/(scene+".json"),store.paths.home)
            if json.loads(path.read_bytes()).get("body") != body:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def _write_once(path, value):
    if path.exists():
        require(json.loads(path.read_bytes()) == value, "已保存的场景材料不一致。")
    else:
        atomic_json(path, value)


def _retain(paths, asset):
    material = fingerprint(asset)
    path = _published_path(paths,material)
    if not path.parent.is_dir():
        private_directory(paths.home,path.parent)
    _write_once(path,asset)
    return material


def _activate(store, db, pid, binding, asset, providers):
    bodies = {s:asset["common"] + "\n\n" + asset["scenes"][s] for s in SCENES}
    catalog = {"version":scene_policy.VERSION, "persona":pid, "character_version":binding["character_version"],
               "profile_sha256":binding["profile_sha256"], "origin":ORIGIN, "source_card":binding["material"],
               "instruction":asset["jev"]["instruction"], "criteria":asset["jev"]["criteria"],
               "cards":{s:fingerprint(body) for s,body in bodies.items()}, "providers":providers}
    revision = fingerprint(catalog)
    directory = private_directory(store.paths.home, store.paths.home/"private"/"scene-voice"/revision)
    require(not (directory/"approval.withdrawn").exists(), "场景批准已撤回。")
    for scene, body in bodies.items():
        require(not (directory/(scene+".withdrawn")).exists(), "场景材料已撤回。")
        _write_once(safe_path(store.paths.home, directory/(scene+".json")), {"body":body})
    _write_once(safe_path(store.paths.home, directory/"catalog.json"), catalog)
    approval = {"version":"import-card-scene-confirmation-v1", "decision":"approved",
                "catalog_sha256":revision, "source_card":binding["material"], "persona_id":pid, **providers}
    approval_path = safe_path(store.paths.home, directory/"author-approval.private.json")
    _write_once(approval_path, approval)
    policy = {"version":scene_policy.VERSION, "origin":ORIGIN, "persona_id":pid, **providers,
              "activation_id":identifier(), "catalog_sha256":revision,
              "source_card":binding["material"], "approval_sha256":sha256(approval_path.read_bytes()).hexdigest()}
    scene_policy.material(store.paths,policy)
    scene_policy.save(db, scene_policy.IMPORTED_PREFIX + pid, policy)


class CardPublisher:
    def __init__(self, store, library, card_jobs):
        self.store = store
        self.library = library
        self.jobs = card_jobs

    def create(self, ident, revision, plan_id, card_revision, roles, operation_id, enable_scene=False, confirmed=False):
        require(confirmed is True, "请确认当前版本的角色卡。")
        require(type(revision) is int and type(card_revision) is int and revision > 0 and card_revision >= 0
                and scene_policy.digest(ident) and scene_policy.digest(plan_id), "角色卡身份或修订无效。")
        require(isinstance(roles, list) and 1 <= len(roles) <= 2 and all(r in ("A","B") for r in roles)
                and len(set(roles)) == len(roles), "请选择要创建的人物。")
        require(isinstance(operation_id,str) and 0 < len(operation_id) <= 128 and type(enable_scene) is bool,
                "创建请求无效。")
        signature = fingerprint([ORIGIN, ident, revision, plan_id, card_revision, roles, enable_scene])
        op = import_style.PREFIX+"create."+operation_id
        people = []
        with self.store.connection(write=True) as db:
            previous = scene_policy.read(db, op)
            if previous:
                require(previous["request"] == signature, "重复请求内容已变化。")
                return previous["people"]
            require(not scene_policy.read(db,import_style.REVOKED+ident), "来源已撤回。")
            with self.library.db() as private:
                private.execute("BEGIN IMMEDIATE")
                people = self.create_in_transaction(db, private, ident, revision, plan_id, card_revision, roles, enable_scene)
                scene_policy.save(db,op,{"request":signature,"people":people})
                # Keep the frozen card revision locked through the business commit.
                db.commit()
        callback = getattr(self.store,"character_committed",None)
        if callback:
            for pid in people:
                callback(pid)
        return people

    def create_in_transaction(self, db, private, ident, revision, plan_id, card_revision, roles, enable_scene=False):
        require(isinstance(roles, list) and 1 <= len(roles) <= 2 and all(r in ("A", "B") for r in roles)
                and len(set(roles)) == len(roles) and type(enable_scene) is bool, "请选择要创建的人物。")
        require(not scene_policy.read(db, import_style.REVOKED + ident), "来源已撤回。")
        people = []
        cards = self.jobs.material(ident, revision=revision, plan_id=plan_id, card_revision=card_revision, db=private)
        for role in roles:
            role_key = import_style.PREFIX+"created."+fingerprint([ident,role])
            require(not scene_policy.read(db,role_key), "这个说话人已创建角色。")
            card = cards[role]
            require(not unresolved_conflicts(card), role + " 的角色卡还有重大冲突，请先手动处理。")
            spec = PersonaSpec.from_dict(card["spec"])
            canon = character.validate_character(card["character"])
            pid = RuntimeService.insert_persona(db, spec)
            character.append(db,pid,canon,"user")
            if card["relationship"]:
                append_version(db,pid,"relationship",{"description":card["relationship"]},actor="user")
            asset = {**card, "document":ident, "role":role, "plan":plan_id, "card_revision":card_revision}
            material = _retain(self.store.paths,asset)
            char, profile = scene_policy.head(db,pid)
            binding = {"material":material,"revision":1,"document":ident,"plan":plan_id,"role":role,
                       "source_revision":revision, "card_revision":card_revision, "card_hash":fingerprint(card),
                       "character_version":char,"profile_sha256":profile}
            scene_policy.save(db,BINDING+pid,binding)
            if enable_scene:
                providers, _ = _available(self.store,db,pid,binding,asset)
                if providers:
                    _activate(self.store,db,pid,binding,asset,providers)
            scene_policy.save(db,role_key,pid)
            people.append(pid)
        return people

    def _review(self, db, private, pid, saved):
        asset = _published(self.store.paths,saved)
        proposed = dict(saved)
        source = private.execute("""SELECT d.revision,cards.revision AS card_revision,cards.cards
                                    FROM documents d JOIN profile_current c ON c.document=d.id
                                    LEFT JOIN profile_card_drafts cards ON cards.document=d.id
                                    AND cards.plan=c.plan AND cards.source_revision=d.revision
                                    WHERE d.id=? AND c.plan=? AND d.view IS NULL
                                    AND d.status IN ('ready','partial_ready')""",
                                 (saved["document"],saved["plan"])).fetchone()
        editable = bool(source and source["revision"] == saved["source_revision"])
        if editable and source["cards"] and source["card_revision"] != saved["card_revision"]:
            new_card = json.loads(source["cards"])[saved["role"]]
            # Scene consent covers speech materials. A change to the ordinary persona
            # or its canon requires its own formal update before the two routes agree.
            same_persona = all(new_card[k] == asset[k] for k in ("spec","character","relationship"))
            new_hash = fingerprint(new_card)
            if same_persona and new_hash != saved["card_hash"]:
                asset = {**new_card, "document":saved["document"],"role":saved["role"],"plan":saved["plan"],
                         "card_revision":source["card_revision"]}
                proposed.update(material=fingerprint(asset),card_hash=new_hash,card_revision=source["card_revision"])
        providers, reason = _available(self.store,db,pid,proposed,asset)
        policy = scene_policy.read(db, scene_policy.IMPORTED_PREFIX+pid)
        enabled = bool(policy and not policy.get("paused") and providers
                       and imported_scene_available(self.store,db,pid,policy))
        revoked = bool(scene_policy.read(db,import_style.REVOKED+saved["document"]))
        info = {"enabled":enabled, "available":bool(providers), "reason":reason, "revoked":revoked,
                "affected_fields":sorted(asset.get("sources", {})) if revoked else [],
                "document":saved["document"], "role":saved["role"], "can_edit_source":editable and not revoked,
                "plan_id":saved["plan"], "source_revision":saved["source_revision"],
                "updated":proposed["material"] != saved["material"],
                "revision":saved["revision"], "version":fingerprint([proposed,providers]),
                "main":scene_policy.PRO, "jev_model":scene_policy.JEV,
                "common":asset["common"], "scenes":asset["scenes"], "jev":asset["jev"]}
        return info, proposed, asset, providers

    def info(self, persona_id):
        with self.store.connection() as db:
            require_live_persona(db, persona_id)
            binding = scene_policy.read(db,BINDING+persona_id)
            if not binding:
                return None
            with self.library.db() as private:
                return self._review(db,private,persona_id,binding)[0]

    def enable(self, persona_id, enabled, confirmed=False, card_revision=None, version=None):
        require(confirmed is True and type(enabled) is bool, "请确认该角色的场景设置。")
        with self.store.connection(write=True) as db:
            require_live_persona(db,persona_id)
            saved = scene_policy.read(db,BINDING+persona_id)
            require(saved and type(card_revision) is int and saved["revision"]==card_revision, "场景版本已变化。")
            with self.library.db() as private:
                private.execute("BEGIN IMMEDIATE")
                info, proposed, asset, providers = self._review(db,private,persona_id,saved)
                require(info["version"]==version, "场景或提供商已变化，请重新核对。")
                if enabled:
                    require(providers,info["reason"] or "该角色的场景材料尚未就绪。")
                    if info["enabled"]:
                        return info
                    require(_retain(self.store.paths,asset) == proposed["material"], "场景版本已变化。")
                    _activate(self.store,db,persona_id,proposed,asset,providers)
                    saved = proposed
                else:
                    policy = scene_policy.read(db,scene_policy.IMPORTED_PREFIX+persona_id)
                    if policy:
                        scene_policy.save(db,scene_policy.IMPORTED_PREFIX+persona_id,policy | {"paused":True})
                saved["revision"] += 1
                scene_policy.save(db,BINDING+persona_id,saved)
                result = self._review(db,private,persona_id,saved)[0]
                db.commit()
                return result
