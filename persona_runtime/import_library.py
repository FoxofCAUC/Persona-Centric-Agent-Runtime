"""Instance-private import workspace; production identities live in the business DB."""
from contextlib import contextmanager
import json
import secrets
import sqlite3

from .archive import fingerprint
from .corpus_files import digest
from .import_markdown import assign, parse, learning_rows
from .models import RuntimeErrorBase, timestamp

FIELDS = ("introduction", "core_traits", "expression_style", "values_and_boundaries", "initial_background")
CONTRACT = "pcar-dyadic-import-v1"


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
                dates=[r['date'] for r in parsed['rows'] if r['date']]
                db.execute('INSERT OR IGNORE INTO previews VALUES(?,?)',(ident,encode({'count':len(parsed['rows']),
                    'from':min(dates,default=None),'to':max(dates,default=None)})))
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
            preview=db.execute('SELECT metadata FROM previews WHERE id=?',(ident,)).fetchone()
        require(row is not None, "未找到导入记录。")
        return dict(row) | {'preview':json.loads(preview[0]) if preview else {}} | {key:json.loads(row[key]) for key in ("counts","mapping","overrides","draft")}

    def list(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,created,format,status,counts FROM documents ORDER BY created DESC")]

    def evidence(self, ident, start=0, prepared=False):
        require(type(start) is int and start>=0, "消息位置无效。")
        row = self.row(ident)
        if prepared:
            require(row["view"] is not None, "请先准备脱敏预览。")
            rows = json.loads(row["view"])
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

    def edition(self, ident, revision, role):
        row = self.row(ident)
        require(row["revision"]==revision and row["status"] not in ("running","revoked"), "草稿已变化。")
        target = json.loads(row["draft"])[role]
        record = {"document":ident, "role":role, "revision":revision, **target}
        version = fingerprint(record)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO editions VALUES(?,?,?)", (version,ident,encode(record)))
        return version,record

    def material(self, version):
        with self.db() as db:
            row = db.execute("""SELECT e.payload FROM editions e JOIN documents d ON d.id=e.document
                WHERE e.id=? AND d.status!='revoked'""",(version,)).fetchone()
        require(row, "来源已撤回或不可用。")
        result = json.loads(row[0])
        require(fingerprint(result)==version, "语料版本校验失败。")
        return result
