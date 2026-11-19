"""Recall integration: frozen role policy, canonical source guards and local work."""
import json,time
from .archive import fingerprint,turn_scope
from .models import RuntimeErrorBase
from .recall_support import RecallSupport,encoded,tokens
from .recall_sources import scan,epoch

VERSION='recall-runtime-v1'
DEFAULT={'enabled':False,'version':VERSION}
QUERY_VERSION='recall-context-query-v2'
FUNCTION_CHARS=set('我你他她它的了是在和与有也就都很还要想把将被给让从向往于对为而但或及并所着得地过能会可来去到做说看先再正更最太挺点一两几某各每只又已这那哪们个些么吗呢啊吧呀之今明昨前后天早晚上下午时年月日现')
FUNCTION_TERMS={'今天','明天','昨天','前天','后天','晚上','上午','下午','现在','时候','之前','前面','刚才','这个','那个','什么','怎么','我们','你们','他们','还有','已经','可以','一下'}

def query_terms(db,selected,parts):
    ordered=[];seen=set();key=fingerprint(selected)
    for part in parts:
        candidates=[t for t in tokens(part) if t not in FUNCTION_TERMS and not (all('\u3400'<=c<='\u9fff' for c in t) and set(t)<=FUNCTION_CHARS)]
        candidates=sorted(set(candidates),key=lambda t:(-len(t),t))[:256]
        if not candidates:continue
        marks=','.join('?' for _ in candidates)
        known={r[0] for r in db.execute('SELECT DISTINCT t.term FROM recall_support_terms t JOIN recall_support_units u ON u.id=t.unit_id WHERE u.scope_key=? AND t.term IN ('+marks+')',(key,*candidates))}
        for term in candidates:
            if term in known and term not in seen:ordered.append(term);seen.add(term)
        if len(ordered)>=256:break
    return ordered[:256]

HISTORY_RANGE = "import_session:"


def enqueue_imported_session(db,pid,sid):
    """Coalesce new transcript events into a bounded, resumable index range."""
    if not policy(db,pid)["data"]["enabled"]:
        return
    db.execute("""DELETE FROM recall_runtime_dirty WHERE persona_id=? AND kind='dialogue'
        AND source_id IN (SELECT id FROM events WHERE persona_id=? AND session_id=? AND id LIKE 'imported.%')""",
        (pid,pid,sid))
    db.execute("""INSERT INTO recall_runtime_dirty(persona_id,kind,source_id) VALUES(?,'dialogue',?)
        ON CONFLICT(persona_id,kind,source_id) DO UPDATE SET cursor_json='[0,0]',revision=revision+1""",
        (pid,HISTORY_RANGE+sid))


def policy(db,pid):
    row=db.execute('SELECT * FROM recall_runtime_policies WHERE persona_id=?',(pid,)).fetchone()
    return {'revision':row['revision'],'data':json.loads(row['data_json'])} if row else {'revision':0,'data':dict(DEFAULT)}

def accepted(db,turn):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recall_runtime_turns'").fetchone():
        return {'revision':0,'data':dict(DEFAULT)},None
    row=db.execute('SELECT policy_json,selection_id FROM recall_runtime_turns WHERE turn_id=?',(turn['id'],)).fetchone()
    return (json.loads(row['policy_json']),row['selection_id']) if row else ({'revision':0,'data':dict(DEFAULT)},None)

def check_policy(db,turn):
    frozen,_=accepted(db,turn)
    if not frozen['data']['enabled']:return False
    if policy(db,turn['persona_id'])!=frozen:raise RuntimeErrorBase('recall_policy_changed')
    return True

def selection(db,turn):
    if not check_policy(db,turn):return None
    _,ident=accepted(db,turn)
    if not ident:return None
    row=db.execute('SELECT result_json,root_id,scope_key FROM recall_support_searches WHERE id=?',(ident,)).fetchone()
    selected=turn_scope(db,turn)
    if not row or row['root_id']!=turn['id'] or row['scope_key']!=fingerprint(selected):raise RuntimeErrorBase('recall_selection_identity_invalid')
    value=json.loads(row['result_json'])
    for item in value['selected']:
        unit=db.execute('SELECT * FROM recall_support_units WHERE id=?',(item['id'],)).fetchone()
        if not unit:raise RuntimeErrorBase('recall_sources_changed')
        source,error=RecallSupport._live(db,unit,selected,time.time())
        if error or unit['source_version']!=item['source_version'] or fingerprint(source['text'])!=item['text_hash']:
            raise RuntimeErrorBase('recall_sources_changed')
        item['text']=source['text']
    return value

def validate(db,turn,meta):
    if not meta:return
    if not check_policy(db,turn):raise RuntimeErrorBase('recall_policy_changed')
    if meta.get('fallback')=='index_warming' and not meta.get('selected_ids'):return
    selected=turn_scope(db,turn)
    row=db.execute('SELECT root_id,scope_key,result_json FROM recall_support_searches WHERE id=?',(meta.get('selection_id'),)).fetchone()
    if not row or row['root_id']!=turn['id'] or row['scope_key']!=fingerprint(selected):raise RuntimeErrorBase('recall_selection_identity_invalid')
    expected={i['id']:i for i in json.loads(row['result_json'])['selected']}
    for ident in meta['selected_ids']:
        unit=db.execute('SELECT * FROM recall_support_units WHERE id=?',(ident,)).fetchone()
        if not unit or ident not in expected:raise RuntimeErrorBase('recall_sources_changed')
        source,error=RecallSupport._live(db,unit,selected,time.time())
        if error or unit['source_version']!=expected[ident]['source_version'] or fingerprint(source['text'])!=expected[ident]['text_hash']:
            raise RuntimeErrorBase('recall_sources_changed')

def render(items,db=None):
    if not items:return ''
    lines=['\n本轮相关的既有资料（按其来源理解，不把人物自己的发言当作用户事实）：']
    for item in items:
        label={'memory':'已有记忆','dialogue':'原始交流片段','native':'先前取得的公开资料'}[item['kind']]
        source_label={'user_statement':'用户当时说的话','assistant_statement':'人物当时说的话',
            'user_memory':'有来源的记忆','user_correction':'用户更正'}.get(item['source_type'],'参考材料')
        lines.append(label+'（'+source_label+'）：\n'+item['text'])
        if db is not None and item['kind']=='memory':
            memory=db.execute('SELECT previous_id,created_at,source_event_id FROM memories WHERE id=?',(item['source_id'],)).fetchone()
            if memory:
                from .import_history import source_time
                when = source_time(db,memory['source_event_id']) if memory['source_event_id'].startswith('imported.') else memory['created_at']
                if when:
                    lines.append('记忆记录时间：'+when)
                if memory['previous_id']:lines.append('这是当前更正版本；原始交流中的旧值只表示当时记录。')
    return '\n'.join(lines)

def project(items,db):
    # A canonical memory and its exact original user event are one supplied fact,
    # not two independent pieces of evidence or two reasons to strengthen it.
    duplicates=set()
    for item in items:
        if item['kind']!='memory':continue
        row=db.execute('SELECT source_event_id FROM memories WHERE id=?',(item['source_id'],)).fetchone()
        if row:
            event=db.execute('SELECT text FROM events WHERE id=?',(row[0],)).fetchone()
            if event and event[0].strip()==item['text'].strip():duplicates.add(row[0])
    return [i for i in items if not (i['kind']=='dialogue' and i['source_id'] in duplicates)]

class RecallRuntime:
    def __init__(self,store):self.store=store;self.service=RecallSupport(store)

    def policy(self,pid):
        with self.store.connection() as db:return policy(db,pid)

    def status(self,persona_id):
        with self.store.connection() as db:
            return {'policy':policy(db,persona_id),'dirty_jobs':db.execute('SELECT count(*) FROM recall_runtime_dirty WHERE persona_id=?',(persona_id,)).fetchone()[0],
                'feedback':{r[0]:r[1] for r in db.execute('SELECT f.state,count(*) FROM recall_runtime_feedback f JOIN turns t ON t.id=f.turn_id WHERE t.persona_id=? GROUP BY f.state',(persona_id,))}}

    def update(self,persona_id,enabled,expected_revision,operation_id):
        if type(enabled) is not bool or type(expected_revision) is not int or not isinstance(operation_id,str) or not operation_id:
            raise RuntimeErrorBase('recall_policy_invalid')
        key='knowledge.recall.operation.'+operation_id;token=fingerprint([persona_id,enabled,expected_revision])
        with self.store.connection(write=True) as db:
            old=db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
            if old:
                value=json.loads(old[0])
                if value['hash']!=token:raise RuntimeErrorBase('operation_id_conflict')
                return value['result']
            from .persona_management import active
            active(db,persona_id)
            current=policy(db,persona_id)
            if current['revision']!=expected_revision:raise RuntimeErrorBase('version_conflict')
            result={'revision':expected_revision+1,'data':{'enabled':enabled,'version':VERSION}}
            db.execute('INSERT INTO recall_runtime_policies VALUES(?,?,?) ON CONFLICT(persona_id) DO UPDATE SET revision=excluded.revision,data_json=excluded.data_json',
                (persona_id,result['revision'],encoded(result['data'])))
            if enabled:
                for kind in ('memory','dialogue','native'):
                    db.execute("INSERT INTO recall_runtime_dirty(persona_id,kind,source_id) VALUES(?,?,'*') ON CONFLICT(persona_id,kind,source_id) DO UPDATE SET cursor_json='[0,0]',revision=revision+1",(persona_id,kind))
            else:db.execute('DELETE FROM recall_runtime_dirty WHERE persona_id=?',(persona_id,))
            db.execute('INSERT INTO knowledge_control VALUES(?,?)',(key,encoded({'hash':token,'result':result})))
            return result

    def maintain(self,pid=None,*,pages=3,limit=64):
        worked=0
        if pid is None:
            with self.store.connection() as db:
                if db.execute("SELECT 1 FROM turns WHERE status IN ('queued','running') LIMIT 1").fetchone():return {'pages':0,'frontend_priority':True}
        for _ in range(pages):
            with self.store.connection(write=True) as db:
                args=[];extra=''
                if pid:extra=' AND d.persona_id=?';args=[pid]
                row=db.execute('''SELECT d.* FROM recall_runtime_dirty d JOIN recall_runtime_policies p ON p.persona_id=d.persona_id
                    WHERE json_extract(p.data_json,'$.enabled')=1'''+extra+" ORDER BY CASE WHEN d.source_id='*' THEN 2 WHEN d.source_id LIKE 'import_session:%' THEN 1 ELSE 0 END,d.rowid LIMIT 1",args).fetchone()
                if not row:break
                now,_=self.service._time(db);cursor=json.loads(row['cursor_json']);only=None
                session_range = None
                if row['kind']=='dialogue' and row['source_id'].startswith(HISTORY_RANGE):
                    session_range = row['source_id'][len(HISTORY_RANGE):]
                elif row['source_id']!='*':
                    table,column=('knowledge_control','key') if row['kind']=='native' else ('memories','id') if row['kind']=='memory' else ('events','id')
                    source=db.execute('SELECT rowid AS source_row FROM '+table+' WHERE '+column+'=?',(row['source_id'],)).fetchone()
                    if not source:
                        db.execute('DELETE FROM recall_runtime_dirty WHERE persona_id=? AND kind=? AND source_id=?',(row['persona_id'],row['kind'],row['source_id']));continue
                    only=source['source_row'];cursor=[only,cursor[1]]
                entries,next_cursor,done=scan(db,row['persona_id'],row['kind'],cursor,limit,now,only_row=only,imported_session=session_range)
                for ref,data in entries:
                    if not data.get('skip_fragment'):self.service._register(db,row['kind'],ref,data,now)
                if done:db.execute('DELETE FROM recall_runtime_dirty WHERE persona_id=? AND kind=? AND source_id=?',(row['persona_id'],row['kind'],row['source_id']))
                else:db.execute('UPDATE recall_runtime_dirty SET cursor_json=? WHERE persona_id=? AND kind=? AND source_id=?',(encoded(next_cursor),row['persona_id'],row['kind'],row['source_id']))
                worked+=1
        self.feedback(limit=8)
        return {'pages':worked}

    def prepare(self,turn):
        with self.store.connection() as db:
            if not check_policy(db,turn):return None
        self.maintain(turn['persona_id'],pages=6)
        with self.store.connection() as db:
            if db.execute("SELECT 1 FROM recall_runtime_dirty WHERE persona_id=? AND source_id='*'",(turn['persona_id'],)).fetchone():return None
            from .turn_input import input_text
            question=input_text(db,turn);original_chars=len(question)
            query_head=question if len(question)<=8192 else question[:4096]+'\n'+question[-4096:]
            parts=[query_head]
            from .context_builders import policy_for
            config=policy_for(turn);cross=config['cross_session'] and config['context_mode']=='working_set'
            selected=turn_scope(db,turn)
            recent=[dict(r) for r in db.execute("SELECT * FROM turns WHERE persona_id=? AND channel=? AND status='completed' AND rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY (session_id=?) DESC,rowid DESC LIMIT 10",(turn['persona_id'],turn['channel'],turn['id'],turn['session_id'])) if turn_scope(db,dict(r))==selected]
            if not cross:recent=[r for r in recent if r['session_id']==turn['session_id']]
            if any(cue in question for cue in ('刚才','那页','那份','这份','就用','接着','这件事','那件事','回到','前面说','查到的','之前那','前面那')):
                for prior in recent[:2]:
                    if prior["source_key"].startswith("imported."):
                        from .archive import read_exact
                        from .import_history import chronological
                        group = read_exact(db,selected,{"kind":"turn","id":prior["id"]})
                        if group.get("usable"):
                            excerpt = "\n".join(e["text"] for e in chronological(group))
                            parts.append(excerpt[-2048:])
                        continue
                    parts.append(input_text(db,prior)[:1024])
                    reply=db.execute('SELECT text FROM events WHERE id=?',(prior['reply_event_id'],)).fetchone()
                    if reply:parts.append(reply[0][:1024])
            query_lexical=query_terms(db,selected,parts)
            question='\n'.join(parts)
            event_ids=[r['user_event_id'] for r in recent]
            required=[]
            if event_ids:
                marks=','.join('?' for _ in event_ids)
                required=[r[0] for r in db.execute('''SELECT m.id FROM memories m JOIN memories old ON old.id=m.previous_id
                    JOIN events original ON original.id=old.source_event_id
                    WHERE m.persona_id=? AND m.status='active' AND (original.session_id=? OR old.source_event_id IN ('''+marks+')) LIMIT 33',
                    (turn['persona_id'],turn['session_id'],*event_ids))]
        if len(required)>32:raise RuntimeErrorBase('required_recall_budget_exceeded')
        units=[]
        for ident in required:
            try:units.append(self.service.register('memory',{'id':ident},selected))
            except RuntimeErrorBase as error:
                if str(error) not in ('recall_source_unavailable','recall_scope_denied'):raise
        value=self.service.select(selected,question,root_id=turn['id'],session_id=None if cross else turn['session_id'],required_ids=units,lexical_terms=query_lexical)
        value['query_version']=QUERY_VERSION
        value['query_original_chars']=original_chars
        value['query_excerpt_used']=original_chars>8192
        with self.store.connection(write=True) as db:
            check_policy(db,turn)
            row=db.execute('SELECT result_json FROM recall_support_searches WHERE id=?',(value['id'],)).fetchone()
            audit=json.loads(row[0]);audit.update({k:value[k] for k in ('query_version','query_original_chars','query_excerpt_used')})
            db.execute('UPDATE recall_support_searches SET result_json=? WHERE id=?',(encoded(audit),value['id']))
            db.execute('UPDATE recall_runtime_turns SET selection_id=? WHERE turn_id=?',(value['id'],turn['id']))
        return value

    def feedback(self,*,limit=8):
        with self.store.connection() as db:rows=[dict(r) for r in db.execute("SELECT * FROM recall_runtime_feedback WHERE state='pending' ORDER BY rowid LIMIT ?",(limit,))]
        for row in rows:
            try:
                if row['stage']=='main':
                    from .recall_context import record_completed
                    record_completed(self.store,row['turn_id'])
                else:
                    with self.store.connection() as db:
                        attempt=db.execute('SELECT status FROM organization_attempts WHERE id=?',(row['consumer_id'],)).fetchone()
                        if not attempt or attempt[0]!='completed':continue
                        record=db.execute('SELECT manifest_json FROM context_manifests WHERE organization_attempt_id=? ORDER BY rowid DESC LIMIT 1',(row['consumer_id'],)).fetchone()
                        meta=json.loads(record[0]).get('recall_support') if record else None
                        root=dict(db.execute('SELECT * FROM turns WHERE id=?',(row['turn_id'],)).fetchone());selected=turn_scope(db,root)
                    if meta:
                        for ident in meta['selected_ids']:
                            self.service.record(ident,root['id'],selected,kind='context_adopted',status='completed',occurred_at=epoch(root['created_at']),
                                evidence={'organization_attempt_id':row['consumer_id'],'selection_id':meta['selection_id']})
                state,error='completed',None
            except RuntimeErrorBase as exc:state,error='declined',str(exc)
            except Exception:continue
            with self.store.connection(write=True) as db:db.execute('UPDATE recall_runtime_feedback SET state=?,error_code=? WHERE consumer_id=?',(state,error,row['consumer_id']))

def queue_feedback(db,turn,consumer,stage,meta):
    if meta:db.execute("INSERT OR IGNORE INTO recall_runtime_feedback VALUES(?,?,?,'pending',NULL)",(consumer,turn['id'],stage))
