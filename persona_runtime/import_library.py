"""Instance-private import workspace; production identities live in the business DB."""
from contextlib import contextmanager
import json
import secrets
import sqlite3

from .archive import fingerprint
from .corpus_files import digest
from .import_markdown import assign, parse, learning_rows, valid_message_date
from .models import RuntimeErrorBase, timestamp

FIELDS = ("introduction", "core_traits", "expression_style", "values_and_boundaries", "initial_background")
CONTRACT = "pcar-dyadic-import-v1"


def _preview_summary(parsed):
    dates = [row["date"] for row in parsed["rows"] if row["date"]]
    return {"count": len(parsed["rows"]), "from": min(dates, default=None), "to": max(dates, default=None)}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def require(ok, message):
    if not ok:
        raise RuntimeErrorBase(message)


class ImportLibrary:
    def __init__(self, paths):
        self.root = paths.home / "private/persona-import"

    @contextmanager
    def db(self):
        from .character_embedding import private_directory, safe_path
        home=self.root.parent.parent
        if not self.root.exists():
            private_directory(home,self.root)
        path=safe_path(home,self.root/"imports.sqlite3")
        conn = sqlite3.connect(path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                  id TEXT PRIMARY KEY, raw BLOB NOT NULL, secret BLOB NOT NULL,
                  format TEXT NOT NULL, counts TEXT NOT NULL, mapping TEXT NOT NULL,
                  overrides TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                  status TEXT NOT NULL, draft TEXT NOT NULL, view TEXT,
                  approved TEXT, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(
                  id TEXT PRIMARY KEY, document TEXT NOT NULL, state TEXT NOT NULL,
                  raw BLOB, status INTEGER, metadata TEXT, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS editions(
                  id TEXT PRIMARY KEY, document TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_plans(
                  id TEXT PRIMARY KEY, document TEXT NOT NULL, payload TEXT NOT NULL,
                  summary TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_current(document TEXT PRIMARY KEY, plan TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_archives(
                  id TEXT PRIMARY KEY, document TEXT NOT NULL, plan TEXT, payload TEXT NOT NULL, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS profile_archives_document ON profile_archives(document);
                CREATE TABLE IF NOT EXISTS profile_results(
                  plan TEXT NOT NULL, batch TEXT NOT NULL, role TEXT NOT NULL, stage TEXT NOT NULL,
                  state TEXT NOT NULL, accepted TEXT NOT NULL, rejected TEXT NOT NULL,
                  PRIMARY KEY(plan,batch,role,stage));
                CREATE TABLE IF NOT EXISTS profile_card_jobs(
                  document TEXT PRIMARY KEY, plan TEXT NOT NULL, source_revision INTEGER NOT NULL,
                  call_id TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL, base_revision INTEGER NOT NULL,
                  candidate TEXT, outcome TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS profile_card_drafts(
                  document TEXT PRIMARY KEY, plan TEXT NOT NULL, source_revision INTEGER NOT NULL,
                  revision INTEGER NOT NULL, cards TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_card_edits(
                  document TEXT NOT NULL, plan TEXT NOT NULL, source_revision INTEGER NOT NULL,
                  role TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(document,plan,source_revision,role,path));
                CREATE TABLE IF NOT EXISTS profile_card_admissions(
                  call_id TEXT NOT NULL, algorithm TEXT NOT NULL, document TEXT NOT NULL,
                  prior_candidate TEXT, prior_draft TEXT, audit TEXT NOT NULL,
                  PRIMARY KEY(call_id, algorithm));
                CREATE TABLE IF NOT EXISTS vectors(
                  id TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            with conn:
                yield conn
        finally:
            conn.close()

    def contract(self, purpose):
        try:
            raw = (self.root / "contract.json").read_bytes()
            contract = json.loads(raw)
            approval = json.loads((self.root / "approval.json").read_bytes())
            require(approval.get('decision')=='approved' and isinstance(approval.get('purposes'),list)
                    and approval["contract_sha256"] == digest(raw) and purpose in approval["purposes"]
                    and contract["version"] == CONTRACT, "本用途的模板尚未审定。")
            key = "analysis_system" if purpose == "analysis" else "style_prefix"
            require(isinstance(contract[key], str) and contract[key].strip(), "已审装配内容为空。")
            if purpose=="style":
                require(contract.get("reply_model")=="deepseek-v4-pro" and type(contract.get("similarity_min")) in (int,float)
                        and 0<contract["similarity_min"]<=1,"风格模型或匹配阈值无效。")
            return contract, digest(raw)
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise RuntimeErrorBase("尚缺本用途已审定的装配文件。") from None

    def add(self, raw):
        ident = digest(raw)
        with self.db() as db:
            if not db.execute("SELECT 1 FROM documents WHERE id=?", (ident,)).fetchone():
                parsed = parse(raw)
                db.execute('INSERT OR IGNORE INTO previews VALUES(?,?)',(ident,encode(_preview_summary(parsed))))
                names = list(parsed["counts"])
                mapping = dict(zip(names, ("A", "B"))) if len(names)==2 else dict.fromkeys(names, "-")
                db.execute("""INSERT OR IGNORE INTO documents
                    (id,raw,secret,format,counts,mapping,overrides,status,draft,created)
                    VALUES(?,?,?,?,?,?,?,'preview','{}',?)""", (ident, raw, secrets.token_bytes(32),
                    parsed["format"], encode(parsed["counts"]), encode(mapping), "{}", timestamp()))
        return self.status(ident)

    def row(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (ident,)).fetchone()
            require(row is not None, "未找到导入记录。")
            return dict(row)

    def status(self, ident):
        with self.db() as db:
            row = db.execute("SELECT id,format,counts,mapping,overrides,revision,status,draft,approved FROM documents WHERE id=?", (ident,)).fetchone()
            from .import_profile_workspace import plan_status, requires_profile_review
            plan = plan_status(db, ident) if row and row["status"] != "revoked" else None
            cached = db.execute('SELECT metadata FROM previews WHERE id=?',(ident,)).fetchone()
            preview = json.loads(cached[0]) if cached else {}
            if row and any(preview.get(key) is not None and not valid_message_date(preview[key]) for key in ("from","to")):
                # Older imports counted malformed timestamp labels when caching the bounds.
                raw = db.execute("SELECT raw FROM documents WHERE id=?", (ident,)).fetchone()[0]
                preview = _preview_summary(parse(raw))
                db.execute("UPDATE previews SET metadata=? WHERE id=?", (encode(preview),ident))
        require(row is not None, "未找到导入记录。")
        decoded = {key: json.loads(row[key]) for key in ("counts", "mapping", "overrides", "draft")}
        if plan is not None:
            for draft in decoded["draft"].values():
                for claim in draft["claims"]:
                    claim["needs_review"] = requires_profile_review(claim)
        return dict(row) | decoded | {"plan": plan, "preview": preview}

    def list(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,created,format,status,counts FROM documents ORDER BY created DESC")]

    def evidence(self, ident, start=0, prepared=False, sections=False):
        require(type(start) is int and start>=0, "消息位置无效。")
        row = self.row(ident)
        require(row["status"] != "revoked", "来源已撤回。")
        if prepared:
            require(row["view"] is not None, "请先准备脱敏预览。")
            rows = json.loads(row["view"])
        elif sections:
            from .import_plan import parse_source
            rows = parse_source(row["raw"])["rows"]
        else:
            rows = parse(row["raw"])["rows"]
        return {"total":len(rows), "rows":rows[start:start+50]}

    def prepare(self, ident, revision, mapping, overrides):
        row = self.row(ident)
        require(row["status"] not in ("running","revoked"), "当前状态无法修改归属。")
        rows = assign(parse(row["raw"]), mapping, overrides)
        view = learning_rows(rows, row["secret"], ident)
        require({r["speaker"] for r in view} == {"A","B"}, "学习分区中需要有双方的有效原话。")
        draft = {}
        for role in ("A","B"):
            names = [r["speaker"] for r in rows if r["role"] == role]
            draft[role] = {"spec":{"format_version":1,"name":names[0], "introduction":"",
                           "core_traits":[],"expression_style":"","values_and_boundaries":[],"initial_background":""},
                           "relationship":"","claims":[],"samples":[]}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            require(not db.execute("SELECT 1 FROM profile_current WHERE document=?", (ident,)).fetchone(),
                    "请在新版分析计划中修正归属。")
            changed = db.execute("""UPDATE documents SET mapping=?,overrides=?,view=?,draft=?,approved=NULL,
                status='prepared',revision=revision+1 WHERE id=? AND revision=? AND status NOT IN ('running','revoked')""",
                (encode(mapping),encode(overrides),encode(view),encode(draft),ident,revision)).rowcount
            require(changed, "草稿已变化，请重新读取。")
        return self.status(ident)

    def edit(self, ident, revision, role, spec, relationship):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,status,draft FROM documents WHERE id=?", (ident,)).fetchone()
            require(row and row["revision"]==revision and row["status"] not in ("running","revoked"), "草稿版本已变化。")
            draft=json.loads(row["draft"])
            require(role in draft and isinstance(spec,dict) and set(spec)==set(draft[role]["spec"])
                    and spec.get("format_version")==1 and isinstance(relationship,str), "草稿字段无效。")
            for k,v in spec.items():
                if k in ("core_traits","values_and_boundaries"):
                    require(isinstance(v,list) and all(isinstance(s,str) for s in v),"列表字段无效。")
                elif k!="format_version":
                    require(isinstance(v,str),"文字字段无效。")
            prior=draft[role]["spec"]
            changed=set(draft[role].get("edited_fields",[])) | {k for k in FIELDS if prior[k]!=spec[k]}
            draft[role].update(spec=spec, relationship=relationship,edited_fields=sorted(changed))
            db.execute("UPDATE documents SET revision=revision+1,draft=? WHERE id=?", (encode(draft), ident))
        return self.status(ident)

    def edition(self, ident, revision, role, *, legacy_create=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT revision,status,draft,view IS NULL AS v2 FROM documents
                                WHERE id=?""", (ident,)).fetchone()
            require(row and row["revision"] == revision and row["status"] not in ("running","revoked"),
                    "草稿已变化。")
            target = json.loads(row["draft"])[role]
            if legacy_create and row["v2"]:
                from .import_profile_workspace import requires_profile_review
                has_card = db.execute("""SELECT 1 FROM profile_card_jobs WHERE document=?
                                         UNION ALL SELECT 1 FROM profile_card_drafts WHERE document=? LIMIT 1""",
                                      (ident, ident)).fetchone()
                require(not has_card and not any(requires_profile_review(c) for c in target["claims"]
                                                 if c.get("decision") != "reject"),
                        "请从角色卡窗口核对并保存所选人物。")
            record = {"document": ident, "role": role, "revision": revision, **target}
            version = fingerprint(record)
            db.execute("INSERT OR IGNORE INTO editions VALUES(?,?,?)", (version,ident,encode(record)))
        return version, record

    def material(self, version):
        with self.db() as db:
            row = db.execute("""SELECT e.payload FROM editions e JOIN documents d ON d.id=e.document
                WHERE e.id=? AND d.status!='revoked'""",(version,)).fetchone()
        require(row, "来源已撤回或不可用。")
        result = json.loads(row[0])
        require(fingerprint(result)==version, "语料版本校验失败。")
        return result
