"""Durable desktop route and the sole draft dispatch boundary for an isolated candidate."""
import json
import math
import time

from . import desktop_recovery, session_privacy
from .archive import encoded, fingerprint
from .call_limits import gate, resume_on_limit, turn_scope
from .character import head as character_head
from .config import same_credential
from .expression_contract import text_hash
from .models import RuntimeErrorBase, identifier, timestamp
from .profile import snapshot
from .turn_input import load_turn_input

ROUTE = 'maibot-private-v1'
ORDINARY = 'maibot-private-v2'
RESEARCH = 'maibot-private-v3'
PRIVATE_ROUTES = (ORDINARY, RESEARCH)
PHASES = {'main', 'search_notice'}
ACTIONS = {'share', 'respond', 'support', 'clarify', 'close', 'change_topic'}
MODES = {'chat', 'short_answer', 'long_detail'}


def selected(db, instance, persona_id=None):
    record = db.execute("SELECT route FROM private_reply_instances WHERE instance_id=?", (instance,)).fetchone()
    if record and persona_id is not None:
        scope=db.execute("SELECT value FROM knowledge_control WHERE key=?",('private.reply.personas.'+instance,)).fetchone()
        if scope and persona_id not in json.loads(scope[0]):
            return None
    return record[0] if record else None


def retired(db, instance):
    return bool(db.execute("SELECT 1 FROM knowledge_control WHERE key=?",
                           ('private.reply.retired.' + instance,)).fetchone())


def bind(db, turn_id, instance, route=ROUTE):
    db.execute("INSERT INTO private_reply_turns VALUES (?,?,?)", (turn_id, instance, route))


def candidate(db, turn_id):
    return db.execute("SELECT * FROM private_reply_turns WHERE turn_id=?", (turn_id,)).fetchone()


def stop(db, turn_id, reason):
    db.execute("""UPDATE turns SET status='interrupted',error_code=?,updated_at=?
        WHERE id=? AND cancelled_at IS NULL AND status!='completed'""", (reason,timestamp(),turn_id))


def recover(db, turn):
    if candidate(db,turn['id'])['route'] in PRIVATE_ROUTES:
        from .ordinary_reply import recover_turn
        return recover_turn(db,turn)
    attempt = db.execute("SELECT state FROM private_reply_attempts WHERE turn_id=? LIMIT 1", (turn['id'],)).fetchone()
    if attempt is None:
        return False
    if attempt['state'] == 'sending':
        db.execute("UPDATE private_reply_attempts SET state='unknown' WHERE turn_id=? AND state='sending'", (turn['id'],))
    stop(db, turn['id'], 'reply_review_pending' if attempt['state']=='received' else 'reply_dispatch_unknown')
    return True


class PrivateReplyEntry:
    def __init__(self, engine, transport=None, ordinary=None):
        self.engine = engine
        self.store = engine.store
        self.config = engine.config
        self.transport = transport
        from .ordinary_reply import OrdinaryReply
        self.ordinary = OrdinaryReply(self,ordinary)
        target_route = RESEARCH if getattr(ordinary,'research',False) else ORDINARY
        # Installing the isolated transport selects the route durably for subsequent desktop inputs.
        if transport is not None or ordinary is not None:
            with self.store.connection(write=True) as db:
                db.execute("INSERT OR IGNORE INTO private_reply_instances VALUES(?,?)",
                           (self.config.load()['instance_id'], target_route if ordinary is not None else ROUTE))
                if ordinary is not None:
                    db.execute("UPDATE private_reply_instances SET route=? WHERE instance_id=?",
                               (target_route,self.config.load()['instance_id']))
                    instance=self.config.load()['instance_id']
                    scope=getattr(ordinary,'bundle',{}).get('scope',{}).get('personas')
                    if scope:
                        db.execute("INSERT OR REPLACE INTO knowledge_control VALUES(?,?)",
                                   ('private.reply.personas.'+instance,encoded(scope)))

    def _binding(self, db, turn_id, phase):
        from .migration import ensure_cloud_ready
        ensure_cloud_ready(self.store.paths)
        cfg = self.config.load()
        route = candidate(db, turn_id)
        if not route or route['instance_id'] != cfg['instance_id'] or route['route'] not in (ROUTE,*PRIVATE_ROUTES) or phase not in PHASES:
            raise RuntimeErrorBase('private_reply_route_changed')
        if retired(db,cfg['instance_id']):
            raise RuntimeErrorBase('private_reply_candidate_retired')
        turn = dict(db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone())
        if turn['channel']!='desktop' or turn['cancelled_at'] or turn['status']=='completed':
            raise RuntimeErrorBase('private_reply_turn_inactive')
        desktop_recovery.validate_turn(db, turn)
        pid, sid = turn['persona_id'], turn['session_id']
        if (self.store.current(db,required=False)!=pid or json.loads(turn['character_snapshot'])['id']!=character_head(db,pid)['id']
                or json.loads(turn['profile_snapshot'])!=snapshot(db,pid)):
            raise RuntimeErrorBase('private_reply_persona_changed')
        model = json.loads(turn['model_snapshot'])
        live = cfg['model']
        if (model['name'],model['base_url']) != (live['name'],live['base_url']) or not same_credential(model.get('encrypted_key'),live.get('encrypted_key')):
            raise RuntimeErrorBase('private_reply_model_changed')
        grant = session_privacy.current(db,self.config,cfg,pid,sid,session_privacy.recipients(self.config,cfg))
        if not grant['enabled']:
            from .private_personas import guard_cloud_dispatch
            guard_cloud_dispatch(self.store,pid,'main',model,db=db)
            notice=getattr(self.ordinary.providers,'notice_model',None)
            if phase=='search_notice' and notice is not None and (target:=notice()) is not None:
                guard_cloud_dispatch(self.store,pid,'knowledge_notice',target,db=db)
        members = load_turn_input(db,turn)
        contract = {'instance':cfg['instance_id'],'route':route['route'],'persona':pid,'session':sid,'turn':turn_id,
                    'revision':turn['execution_revision'],'phase':phase,
                    'inputs':[(m['id'],text_hash(m['text'])) for m in members],
                    'profile':fingerprint(turn['profile_snapshot']), 'character':fingerprint(turn['character_snapshot']),
                    'model':fingerprint(turn['model_snapshot']), 'consent':grant}
        return turn, contract

    def contract(self, turn_id, phase='main'):
        with self.store.connection() as db:
            _, contract = self._binding(db,turn_id,phase)
        return {'binding': fingerprint(contract), 'targets':[row[0] for row in contract['inputs']]}

    def accept_decision(self, turn_id, decision, expected_binding, *, expires_at, phase='main', ordinary_proof=None):
        """Trusted planner interface. It has no HTTP setter and cannot publish a reply."""
        with self.store.connection(write=True) as db:
            if type(expires_at) not in (float,int) or not math.isfinite(expires_at) or expires_at <= time.time():
                raise RuntimeErrorBase('private_reply_decision_stale')
            turn, contract = self._binding(db,turn_id,phase)
            if contract['route'] in PRIVATE_ROUTES:
                contract = self.ordinary.extend_contract(db,turn,contract,ordinary_proof)
                if ordinary_proof['decision_hash']!=fingerprint(decision):
                    raise RuntimeErrorBase('reply_decision_required')
            binding = fingerprint(contract)
            if binding != expected_binding:
                raise RuntimeErrorBase('private_reply_decision_stale')
            keys = {'targets','action','stance','style','facts','mode','ending'}
            targets = [row[0] for row in contract['inputs']]
            allowed_facts = targets
            if ordinary_proof:
                targets = targets + ordinary_proof['references']
                allowed_facts = targets + list(ordinary_proof['selected'])
            if (not isinstance(decision,dict) or set(decision)!=keys
                    or not isinstance(decision['targets'],list) or not decision['targets']
                    or not all(isinstance(item,str) and item in targets for item in decision['targets'])
                    or len(set(decision['targets']))!=len(decision['targets'])
                    or not isinstance(decision['facts'],list)
                    or not all(isinstance(item,str) and item in allowed_facts for item in decision['facts'])
                    or not all(isinstance(decision[k],str) and decision[k].strip() for k in ('action','stance','style','mode','ending'))
                    or decision['action'] not in ACTIONS or decision['mode'] not in MODES
                    or decision['ending'] not in ('stop','clarify')):
                raise RuntimeErrorBase('reply_decision_required')
            draft = ordinary_proof['draft'] if ordinary_proof else 0
            if db.execute("SELECT 1 FROM private_reply_attempts WHERE turn_id=? AND phase=? AND draft=?", (turn_id,phase,draft)).fetchone():
                raise RuntimeErrorBase('private_reply_already_dispatched')
            raw = encoded(decision)
            old = db.execute("""SELECT id,decision_json FROM private_reply_decisions
                WHERE turn_id=? AND execution_revision=? AND phase=? AND binding=? AND expires_at>?
                ORDER BY rowid DESC LIMIT 1""",
                (turn_id,turn['execution_revision'],phase,binding,time.time())).fetchone()
            if old:
                if old['decision_json'] != raw:
                    raise RuntimeErrorBase('private_reply_decision_conflict')
                return old['id']
            receipt = identifier()
            db.execute("""INSERT INTO private_reply_decisions
                (id,turn_id,execution_revision,phase,binding,contract_json,decision_json,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (receipt,turn_id,turn['execution_revision'],phase,binding,encoded(contract),raw,timestamp(),expires_at))
            if phase == 'main':
                db.execute("""UPDATE turns SET status='queued',error_code=NULL,updated_at=?
                    WHERE id=? AND status='interrupted' AND error_code IN
                    ('reply_decision_required','private_reply_transport_unavailable') AND cancelled_at IS NULL""",
                    (timestamp(),turn_id))
            return receipt

    @resume_on_limit
    async def run(self, turn_id, phase='main'):
        with self.store.connection() as db:
            route = candidate(db,turn_id)
        if route and route['route'] in PRIVATE_ROUTES:
            return await self.ordinary.run(turn_id,phase)
        from .private_transport_recovery import PrivateTransportParked
        try:
            return await self.dispatch(turn_id,phase)
        except PrivateTransportParked:
            return

    async def dispatch(self, turn_id, phase, *, proof=None):
        draft = proof['draft'] if proof else 0
        with self.store.connection(write=True) as db:
            existing = db.execute("SELECT state FROM private_reply_attempts WHERE turn_id=? AND phase=? AND draft=?", (turn_id,phase,draft)).fetchone()
            if existing:
                return
            try:
                turn, contract = self._binding(db,turn_id,phase)
                if contract['route'] in PRIVATE_ROUTES:
                    contract = self.ordinary.extend_contract(db,turn,contract,proof)
            except RuntimeErrorBase as exc:
                stop(db,turn_id,str(exc))
                return
            choice = db.execute("""SELECT id,decision_json FROM private_reply_decisions
                WHERE turn_id=? AND execution_revision=? AND phase=? AND binding=? AND expires_at>?
                ORDER BY rowid DESC LIMIT 1""",
                (turn_id,turn['execution_revision'],phase,fingerprint(contract),time.time())).fetchone()
            transport = self.ordinary.write if proof else self.transport
            if not choice or transport is None:
                stop(db,turn_id,'reply_decision_required' if not choice else 'private_reply_transport_unavailable')
                return
            if not proof and not desktop_recovery.claim(db,turn,self.config.load(),self.engine.desktop_owner):
                return
            if proof:
                state=desktop_recovery.contract(db,turn)
                if not state or state['owner']!=self.engine.desktop_owner:
                    raise RuntimeErrorBase('desktop_owner_changed')
            meter=gate(db,self.store,'private_reply',turn_scope(turn))
            attempt = identifier()
            db.execute("""INSERT INTO private_reply_attempts
                (id,decision_id,turn_id,phase,draft,state,started_at) VALUES(?,?,?,?,?,'sending',?)""",
                (attempt,choice['id'],turn_id,phase,draft,timestamp()))
            db.execute("UPDATE turns SET status='running',error_code=NULL WHERE id=?", (turn_id,))
        parked=False
        try:
            body, usage = await transport(attempt, json.loads(choice['decision_json']), contract)
            detail = None
            if proof and json.loads(choice['decision_json'])['mode']=='long_detail':
                if (isinstance(body,dict) and set(body)=={'body','detail'} and
                        all(isinstance(s,str) and s.strip() for s in body.values())):
                    body,detail=body['body'],body['detail']
                elif not isinstance(body,str):
                    body=encoded(body)
            if not isinstance(body,str) or not body.strip() or not isinstance(usage,dict):
                raise RuntimeErrorBase('private_reply_invalid_receipt')
            with self.store.connection(write=True) as db:
                db.execute("""UPDATE private_reply_attempts SET state='received',body=?,detail=?,body_sha256=?,
                    usage_json=?,received_at=? WHERE id=? AND state IN ('sending','unknown')""",
                    (body,detail,text_hash(body),encoded(usage),timestamp(),attempt))
                if proof:
                    db.execute("UPDATE turns SET status='queued',error_code=NULL WHERE id=? AND cancelled_at IS NULL",(turn_id,))
                else:
                    stop(db,turn_id,'reply_review_pending')
        except (OSError,RuntimeErrorBase) as error:
            from .chat import ModelError
            from .private_transport_recovery import PrivateTransportParked,park
            with self.store.connection(write=True) as db:
                parked=park(db,turn,attempt,'writer',fingerprint(contract),meter,error)
                if not parked and isinstance(error,ModelError):
                    diagnostics=error.diagnostics or {}
                    fields=('phase','elapsed_ms','finish_reason','http_status','request_headers_sent',
                            'response_headers_received','transport_profile','requested_model_echoed','output_characters')
                    usage=dict(error.usage or {}, transport_error={'code':str(error),
                               **{key:diagnostics[key] for key in fields if key in diagnostics}})
                    db.execute("UPDATE private_reply_attempts SET usage_json=? WHERE id=? AND state='sending'",
                               (encoded(usage),attempt))
            if parked:
                raise PrivateTransportParked('private_transport_parked') from error
            raise
        finally:
            with self.store.connection(write=True) as db:
                if db.execute("UPDATE private_reply_attempts SET state='unknown' WHERE id=? AND state='sending'",
                              (attempt,)).rowcount:
                    stop(db,turn_id,'reply_dispatch_unknown')
                if not proof:
                    if parked:
                        desktop_recovery.release_for_continuation(db,turn,self.engine.desktop_owner)
                    else:
                        desktop_recovery.finish(db,turn_id)
