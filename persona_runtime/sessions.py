"""Scoped session authority and immutable source manifests. No model calls."""
import asyncio
import json
import time
import threading

from .archive import scope, turn_scope, read_exact, binding, fingerprint, encoded
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .turn_input import load_turn_input

IDLE_SECONDS = 1800
PIPELINE = 'session-package-v1'


def infer_scope(conn, session):
    scopes = {encoded(turn_scope(conn, dict(t))) for t in conn.execute('SELECT * FROM turns WHERE session_id=?', (session['id'],))}
    scopes.update(encoded({k: r[k] for k in ('persona_id','user_id','channel','bot','peer')})
                  for r in conn.execute('SELECT * FROM raw_events WHERE event_id IN (SELECT id FROM events WHERE session_id=?)', (session['id'],)))
    if len(scopes) == 1:
        value = json.loads(next(iter(scopes)))
        try:
            return scope(value['persona_id'], value['channel'], value['bot'], value['peer'])
        except RuntimeErrorBase:
            return None
    if scopes:
        return None
    channels = {r[0] for r in conn.execute('SELECT channel FROM channel_sessions WHERE session_id=?', (session['id'],))}
    return scope(session['persona_id'], 'desktop') if channels == {'desktop'} else None


def event(conn, row, kind, data=None):
    conn.execute('INSERT INTO session_events VALUES(?,?,?,?,?,?,?,?)',
                 (identifier(), row['persona_id'], LOCAL_USER_ID, row['session_id'], kind, row['revision'], encoded(data or {}), timestamp()))


def register(conn, session, selected=None, reason='created', continued_from=None, managed=True):
    old = conn.execute('SELECT * FROM session_lifecycle WHERE session_id=?', (session['id'],)).fetchone()
    if old:
        return dict(old)
    now = timestamp()
    conn.execute('''INSERT INTO session_lifecycle(session_id,persona_id,user_id,channel,bot,peer,state,revision,
        last_activity_at,observation_started_at,continued_from_session_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (session['id'], session['persona_id'], LOCAL_USER_ID, selected['channel'] if selected else None,
         selected['bot'] if selected else '', selected['peer'] if selected else '', 'active' if selected and managed else 'legacy_unmanaged',
         1, None, now if selected and managed else None, continued_from, now))
    row = dict(conn.execute('SELECT * FROM session_lifecycle WHERE session_id=?', (session['id'],)).fetchone())
    if selected and managed:
        conn.execute('INSERT INTO session_heads VALUES(?,?,?,?,?,?) ON CONFLICT(persona_id,user_id,channel,bot,peer) DO UPDATE SET session_id=excluded.session_id',
                     (*selected.values(), session['id']))
    event(conn, row, reason, {'scope_verified': bool(selected)})
    return row


def owned(conn, session_id, selected):
    row = conn.execute('SELECT * FROM session_lifecycle WHERE session_id=? AND persona_id=? AND user_id=?',
                       (session_id, selected['persona_id'], LOCAL_USER_ID)).fetchone()
    if not row or {k: row[k] for k in selected} != selected:
        raise RuntimeErrorBase('session_unavailable')
    return dict(row)


def assert_writable(conn, session_id):
    row = conn.execute('SELECT state FROM session_lifecycle WHERE session_id=?', (session_id,)).fetchone()
    if row and row[0] == 'sealed':
        raise RuntimeErrorBase('session_sealed')


def select(conn, persona_id, channel, session_id=None, origin=None, continued_from=None):
    selected = scope(persona_id, channel, (origin or {}).get('bot',''), (origin or {}).get('sender',''))
    head = conn.execute('SELECT session_id FROM session_heads WHERE persona_id=? AND user_id=? AND channel=? AND bot=? AND peer=?', tuple(selected.values())).fetchone()
    if session_id:
        assert_writable(conn, session_id)
        if head and head[0] != session_id:
            raise RuntimeErrorBase('session_not_current')
        existing = conn.execute('SELECT * FROM sessions WHERE id=? AND persona_id=? AND user_id=?', (session_id, persona_id, LOCAL_USER_ID)).fetchone()
        if not existing:
            raise RuntimeErrorBase('session_unavailable')
        row = conn.execute('SELECT * FROM session_lifecycle WHERE session_id=?', (session_id,)).fetchone()
        if not row or row['state'] == 'legacy_unmanaged':
            inferred = infer_scope(conn, dict(existing))
            empty = not conn.execute('SELECT 1 FROM events WHERE session_id=?', (session_id,)).fetchone()
            if inferred != selected and not (empty and channel == 'desktop'):
                raise RuntimeErrorBase('session_unavailable')
            if head and head[0] != session_id:
                raise RuntimeErrorBase('session_not_current')
            if row:
                conn.execute("UPDATE session_lifecycle SET channel=?,bot=?,peer=?,state='active',revision=revision+1,observation_started_at=? WHERE session_id=?",
                             (channel, selected['bot'], selected['peer'], timestamp(), session_id))
            else:
                register(conn, dict(existing), selected, reason='adopted')
        owned(conn, session_id, selected)
    else:
        session_id = head[0] if head else None
        if not session_id:
            session_id = identifier()
            conn.execute("INSERT INTO sessions VALUES(?,?,?,'private',?)", (session_id, persona_id, LOCAL_USER_ID, timestamp()))
            register(conn, dict(conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()), selected,
                     continued_from=continued_from)
    conn.execute('INSERT INTO session_heads VALUES(?,?,?,?,?,?) ON CONFLICT(persona_id,user_id,channel,bot,peer) DO UPDATE SET session_id=excluded.session_id',
                 (*selected.values(), session_id))
    conn.execute('INSERT INTO channel_sessions VALUES(?,?,?,?) ON CONFLICT(persona_id,channel) DO UPDATE SET session_id=excluded.session_id',
                 (persona_id, channel, LOCAL_USER_ID, session_id))
    return session_id


def blockers(conn, session_id):
    result = [{'kind':'input_batch','id':r['id'],'state':r['state']} for r in conn.execute(
        "SELECT id,state FROM input_batches WHERE session_id=? AND state IN ('collecting','sealed_waiting_capacity','interrupted_buffer')", (session_id,))]
    result += [{'kind':'turn','id':r['id'],'state':r['status']} for r in conn.execute("SELECT id,status FROM turns WHERE session_id=? AND status IN ('queued','running')", (session_id,))]
    result += [{'kind':'part','id':r['id'],'state':r['state']} for r in conn.execute('''SELECT p.id,p.state FROM reply_delivery_parts p JOIN turns t ON t.id=p.turn_id
        WHERE t.session_id=? AND p.state NOT IN ('sent','skipped','resolved')''', (session_id,))]
    result += [{'kind':'delivery_hold','id':r[0],'state':'admin_hold'} for r in conn.execute('''SELECT DISTINCT s.id FROM reply_delivery_streams s
        JOIN reply_delivery_parts p ON p.id=s.blocked_part_id JOIN turns t ON t.id=p.turn_id WHERE t.session_id=? AND s.blocked_reason='admin_hold' ''', (session_id,))]
    result += [{'kind':'legacy_delivery','id':r['turn_id'],'state':r['state']} for r in conn.execute('''SELECT d.* FROM deliveries d JOIN turns t ON t.id=d.turn_id
        WHERE t.session_id=? AND t.status='completed' AND d.state!='sent'
        AND NOT EXISTS(SELECT 1 FROM reply_delivery_parts WHERE turn_id=t.id)
        AND NOT EXISTS(SELECT 1 FROM legacy_delivery_dispositions WHERE turn_id=t.id)''', (session_id,))]
    return result


def operation(conn, operation_id, action, selected, value):
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id)>200:
        raise RuntimeErrorBase('invalid_operation_id')
    digest = fingerprint({'action':action,'scope':selected,'value':value})
    old = conn.execute('SELECT * FROM session_operations WHERE operation_id=?', (operation_id,)).fetchone()
    if old:
        if old['request_hash'] != digest:
            raise RuntimeErrorBase('operation_conflict')
        return digest, json.loads(old['result_json'])
    current = conn.execute("SELECT value FROM settings WHERE key='current_persona'").fetchone()
    if not current or current[0] != selected['persona_id']:
        raise RuntimeErrorBase('persona_selection_conflict')
    return digest, None


def record_operation(conn, operation_id, action, selected, digest, result):
    conn.execute('INSERT INTO session_operations VALUES(?,?,?,?,?,?,?)',
                 (operation_id,selected['persona_id'],LOCAL_USER_ID,action,digest,encoded(result),timestamp()))


def check_revision(row, expected):
    if type(expected) is not int or expected != row['revision']:
        raise RuntimeErrorBase('session_version_conflict')


def build_manifest(conn, row):
    selected = {k: row[k] for k in ('persona_id','user_id','channel','bot','peer')}
    turns, linked = [], set()
    for t in conn.execute('SELECT rowid AS ordinal,* FROM turns WHERE session_id=? ORDER BY rowid', (row['session_id'],)):
        t = dict(t); members = load_turn_input(conn,t); linked.update(m['id'] for m in members)
        if t['reply_event_id']: linked.add(t['reply_event_id'])
        ordinary = read_exact(conn,selected,{'kind':'turn','id':t['id']})
        refs = [{'kind':'event','id':m['id'],'hash':fingerprint(m['text'])} for m in members]
        reply = conn.execute('SELECT id,text FROM events WHERE id=?',(t['reply_event_id'],)).fetchone()
        if reply: refs.append({'kind':'event','id':reply['id'],'hash':fingerprint(reply['text']),'role':'generated_reply'})
        parts = [dict(p) for p in conn.execute('SELECT id,state,text_hash,platform_message_id,semantic_index,part_index,sent_at FROM reply_delivery_parts WHERE turn_id=? ORDER BY part_index',(t['id'],))]
        legacy = conn.execute('SELECT state,updated_at FROM deliveries WHERE turn_id=?',(t['id'],)).fetchone()
        disposition = conn.execute('SELECT reason,actor,created_at FROM legacy_delivery_dispositions WHERE turn_id=?',(t['id'],)).fetchone()
        versions = {}
        for field in ('profile_snapshot','character_snapshot','context_policy_snapshot'):
            v=json.loads(t.get(field) or 'null')
            if isinstance(v,dict):
                versions[field]={k:({x:value[x] for x in ('id','revision') if x in value} if isinstance(value,dict) else value)
                                 for k,value in v.items() if k in ('id','revision','policy_id','core','expression','relationship','schema_version')}
        turns.append({'turn_id':t['id'],'order':t['ordinal'],'status':t['status'],'sources':refs,'member_ids':[m['id'] for m in members],
                      'reply_event_id':t['reply_event_id'],'parts':parts,'binding':binding(ordinary),
                      'legacy_delivery':dict(legacy) if legacy else None,'legacy_disposition':dict(disposition) if disposition else None,
                      'coverage':ordinary.get('coverage','unavailable'),'excluded_reason':None if ordinary['status']=='found' else ordinary['status'],
                      'versions':versions,'snapshot_hashes':{k:fingerprint(t.get(k)) for k in ('profile_snapshot','character_snapshot','context_policy_snapshot','reply_mode_snapshot')},
                      'context_ids':[r[0] for r in conn.execute('SELECT id FROM context_manifests WHERE turn_id=? ORDER BY rowid',(t['id'],))]})
    loose = [{'id':e['id'],'role':e['role'],'hash':fingerprint(e['text']),'reason':'no_complete_turn'} for e in conn.execute('SELECT * FROM events WHERE session_id=? ORDER BY seq',(row['session_id'],)) if e['id'] not in linked]
    return {'format_version':1,'pipeline_version':PIPELINE,'scope':selected,'session_id':row['session_id'],
            'activity_revision':row['activity_revision'],'turns':turns,'standalone_events':loose,'user_read':'unknown'}


class OrganizationDecisionProvider:
    def evaluate(self, package_view):
        return {'decision':'DEFER','reason':'provider_not_enabled','provider':'disabled_m8'}


class SessionService:
    def __init__(self, store, monotonic=None):
        self.store=store; self.monotonic=monotonic or time.monotonic; self.clocks={}; self.started=self.monotonic(); self.clock_lock=threading.RLock(); self.last_error=None

    def restart_observation(self):
        self.clocks.clear(); self.started=self.monotonic()
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE session_lifecycle SET observation_started_at=? WHERE state='active'",(timestamp(),))

    def elapsed(self, row):
        with self.clock_lock:
            old=self.clocks.get(row['session_id'])
            if not old: self.clocks[row['session_id']]=(row['activity_revision'],self.monotonic())
            elif old[0]!=row['activity_revision']: self.clocks[row['session_id']]=(row['activity_revision'],self.monotonic())
            return max(0,self.monotonic()-self.clocks[row['session_id']][1])

    def current(self, persona_id, channel='desktop', bot='', peer=''):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            r=conn.execute('SELECT session_id FROM session_heads WHERE persona_id=? AND user_id=? AND channel=? AND bot=? AND peer=?',tuple(selected.values())).fetchone()
        return self.show(r[0],persona_id,channel,bot,peer) if r else {'session_id':None,'scope':selected}

    def list(self, persona_id, channel='desktop', bot='', peer=''):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM session_lifecycle WHERE persona_id=? AND user_id=? AND channel=? AND bot=? AND peer=? ORDER BY created_at DESC,rowid DESC',tuple(selected.values()))]

    def show(self, session_id, persona_id, channel='desktop', bot='', peer=''):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            row=owned(conn,session_id,selected); row['blockers']=blockers(conn,session_id)
            p=conn.execute('SELECT id FROM session_packages WHERE session_id=?',(session_id,)).fetchone()
            row['package_id']=p[0] if p else None
        row['idle_seconds']=self.elapsed(row) if row['state']=='active' else None
        row['idle_threshold']=IDLE_SECONDS
        return row

    def seal(self, session_id, persona_id, expected_revision, operation_id, channel='desktop', bot='', peer='', reason='manual', _automatic=False, _clock_evidence=None):
        if reason not in ('manual','idle','new_session'):raise RuntimeErrorBase('invalid_seal_reason')
        selected=scope(persona_id,channel,bot,peer); value={'session_id':session_id,'revision':expected_revision,'reason':reason}
        with self.store.connection() as conn:
            old=conn.execute('SELECT request_hash,result_json FROM session_operations WHERE operation_id=?',(operation_id,)).fetchone()
            digest=fingerprint({'action':'seal','scope':selected,'value':value})
            if old:
                if old[0]!=digest:raise RuntimeErrorBase('operation_conflict')
                return json.loads(old[1])
            row=owned(conn,session_id,selected);check_revision(row,expected_revision)
            if row['state']=='legacy_unmanaged':raise RuntimeErrorBase('legacy_session_requires_continuation')
            if row['state']=='sealed':return {'state':'sealed','session_id':session_id,'package_id':conn.execute('SELECT id FROM session_packages WHERE session_id=?',(session_id,)).fetchone()[0]}
            blocked=blockers(conn,session_id)
            if blocked:return {'state':'blocked','session_id':session_id,'blockers':blocked,'revision':row['revision']}
            manifest=build_manifest(conn,row)
        with self.store.connection(write=True) as conn:
            if not _automatic:
                digest,old=operation(conn,operation_id,'seal',selected,value)
                if old is not None:return old
            row=owned(conn,session_id,selected);check_revision(row,expected_revision)
            blocked=blockers(conn,session_id)
            if blocked:return {'state':'blocked','session_id':session_id,'blockers':blocked,'revision':row['revision']}
            now=timestamp(); package_id=identifier(); manifest.update({'sealed_at':now,'reason':reason})
            if _clock_evidence is not None:manifest['clock_evidence']=_clock_evidence
            conn.execute('INSERT INTO session_packages VALUES(?,?,?,?,?,?,?)',(package_id,persona_id,LOCAL_USER_ID,session_id,encoded(manifest),fingerprint(manifest),now))
            conn.execute("UPDATE session_lifecycle SET state='sealed',revision=revision+1,sealed_at=?,seal_reason=? WHERE session_id=?",(now,reason,session_id))
            conn.execute('DELETE FROM session_heads WHERE session_id=?',(session_id,))
            row=owned(conn,session_id,selected);event(conn,row,'sealed',{'package_id':package_id,'reason':reason})
            eligible=any(t['binding']['status']=='found' for t in manifest['turns'])
            for kind,state in (('organize','awaiting_manual'),('reflect','deferred_m8')):
                conn.execute('INSERT INTO session_triggers VALUES(?,?,?,?,?,?,?,?)',(identifier(),persona_id,LOCAL_USER_ID,package_id,kind,PIPELINE,state if eligible else 'no_action',now))
            result={'state':'sealed','session_id':session_id,'package_id':package_id,'revision':row['revision']}
            record_operation(conn,operation_id,'seal',selected,digest,result)
            return result

    def reopen(self, session_id, persona_id, expected_revision, operation_id, channel='desktop', bot='', peer=''):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection(write=True) as conn:
            digest,old=operation(conn,operation_id,'reopen',selected,{'session_id':session_id,'revision':expected_revision})
            if old is not None:return old
            row=owned(conn,session_id,selected);check_revision(row,expected_revision)
            if row['state'] not in ('sealed','legacy_unmanaged'):raise RuntimeErrorBase('session_not_sealed')
            if blockers(conn,session_id):raise RuntimeErrorBase('session_has_pending_work')
            head=conn.execute('SELECT session_id FROM session_heads WHERE persona_id=? AND user_id=? AND channel=? AND bot=? AND peer=?',tuple(selected.values())).fetchone()
            if head:raise RuntimeErrorBase('end_current_session_first')
            sid=select(conn,persona_id,channel,origin={'bot':bot,'sender':peer},continued_from=session_id)
            result={'session_id':sid,'continued_from_session_id':session_id,'state':'active','revision':1}
            record_operation(conn,operation_id,'reopen',selected,digest,result);return result

    def package(self, session_id, persona_id, channel='desktop', bot='', peer=''):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            owned(conn,session_id,selected)
            r=conn.execute('SELECT * FROM session_packages WHERE session_id=?',(session_id,)).fetchone()
            if not r:raise RuntimeErrorBase('session_not_sealed')
            return {'id':r['id'],'manifest':json.loads(r['manifest_json']),'content_hash':r['content_hash']}

    def replay(self, session_id, persona_id, channel='desktop', bot='', peer=''):
        p=self.package(session_id,persona_id,channel,bot,peer); selected=scope(persona_id,channel,bot,peer); groups=[]
        with self.store.connection() as conn:
            for t in p['manifest']['turns']:
                current=read_exact(conn,selected,{'kind':'turn','id':t['turn_id']})
                valid=current['status']=='found' and binding(current)==t['binding']
                groups.append({'turn_id':t['turn_id'],'usable':valid,'reason':None if valid else 'source_unavailable_or_changed',
                               'current':current if valid else {'status':'unavailable'},'sealed':t})
        return p|{'groups':groups,'replay_kind':'read_only','user_read':'unknown'}

    def triggers(self, session_id, persona_id, channel='desktop', bot='', peer=''):
        p=self.package(session_id,persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            return {'triggers':[dict(r) for r in conn.execute('SELECT * FROM session_triggers WHERE package_id=?',(p['id'],))],
                    'tasks':[dict(r) for r in conn.execute("SELECT id,status,error_code,automatic FROM organization_tasks WHERE session_id=? AND json_extract(payload_json,'$.package_id')=? ORDER BY rowid",(session_id,p['id']))],
                    'decision':OrganizationDecisionProvider().evaluate(p)}

    def abandon_delivery(self, session_id, turn_id, persona_id, expected_revision, operation_id, reason, channel='qq', bot='', peer=''):
        selected=scope(persona_id,channel,bot,peer)
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise RuntimeErrorBase('invalid_disposition_reason')
        with self.store.connection(write=True) as conn:
            digest,old=operation(conn,operation_id,'abandon_delivery',selected,{'session_id':session_id,'turn_id':turn_id,'revision':expected_revision,'reason':reason})
            if old is not None:return old
            row=owned(conn,session_id,selected);check_revision(row,expected_revision);assert_writable(conn,session_id)
            t=conn.execute('SELECT * FROM turns WHERE id=? AND session_id=?',(turn_id,session_id)).fetchone()
            d=conn.execute('SELECT state FROM deliveries WHERE turn_id=?',(turn_id,)).fetchone()
            if not t or not d or d[0] not in ('pending','failed','uncertain') or conn.execute('SELECT 1 FROM reply_delivery_parts WHERE turn_id=?',(turn_id,)).fetchone():raise RuntimeErrorBase('invalid_legacy_disposition')
            conn.execute('INSERT OR IGNORE INTO legacy_delivery_dispositions VALUES(?,?,?,?,?,?)',(turn_id,persona_id,LOCAL_USER_ID,reason,'admin',timestamp()))
            conn.execute('UPDATE session_lifecycle SET revision=revision+1,activity_revision=activity_revision+1,last_activity_at=? WHERE session_id=?',(timestamp(),session_id))
            result={'turn_id':turn_id,'disposition':'abandoned','original_state':d[0]}
            record_operation(conn,operation_id,'abandon_delivery',selected,digest,result);return result

    def tick(self, *, _session_ids=None, _clock_evidence=None):
        with self.store.connection() as conn:
            rows=[dict(r) for r in conn.execute("SELECT * FROM session_lifecycle WHERE state='active'")]
        result=[]
        for row in rows:
            if _session_ids is not None and row['session_id'] not in _session_ids:continue
            if self.elapsed(row)<IDLE_SECONDS:continue
            try:
                result.append(self.seal(row['session_id'],row['persona_id'],row['revision'],'idle-'+row['session_id']+'-'+str(row['revision']),
                                        row['channel'],row['bot'],row['peer'],reason='idle',_automatic=True,_clock_evidence=_clock_evidence))
            except RuntimeErrorBase as error:
                if str(error)!='session_version_conflict':raise
        return result

    async def worker(self):
        while True:
            scan=asyncio.create_task(asyncio.to_thread(self.tick))
            try:
                await asyncio.shield(scan)
                self.last_error=None
            except asyncio.CancelledError:
                # Shutdown must not leave an in-flight database mutation behind.
                await asyncio.gather(scan,return_exceptions=True)
                raise
            except Exception:
                import logging
                self.last_error='session_scan_failed'
                logging.getLogger('session').warning('session_scan_failed')
            await asyncio.sleep(5)
