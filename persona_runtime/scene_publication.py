"""Use the existing utterance outlet for native scene replies and confirmed history."""
import json

from . import scene_policy, import_style
from .archive import fingerprint
from .expression_contract import text_hash
from .expression_utterance_publication import SpeechPublication
from .models import RuntimeErrorBase, timestamp
from .presentation import batch_view
from .progress import record
from .scene_style import short_frames, speech
from .scene_voice import require

SOURCE = 'scene_reply'


def native_policy(turn):
    policy = scene_policy.frozen(turn) or import_style.frozen(turn)
    if policy:
        return policy
    origin = json.loads(turn.get("origin_json") or "null") or {}
    return {"history_presentation":1} if origin.get("history_presentation")==1 else None


class ScenePublication:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.outlet = engine.expression.outlet
        self.publisher = SpeechPublication(self.outlet)

    @staticmethod
    def view(db, tid):
        row = db.execute("""SELECT u.id AS utterance_id,p.id AS publication_id,p.revision,p.state,p.reason
             FROM expression_speech_units u JOIN expression_speech_publications p ON p.utterance_id=u.id
             WHERE u.turn_id=? AND u.source_kind=?""",(tid,SOURCE)).fetchone()
        if not row:
            return None
        return dict(row) | {
            'items':[dict(r) for r in db.execute("""SELECT ordinal,state,delay_ms FROM expression_speech_items
                      WHERE publication_id=? AND revision=? ORDER BY ordinal""",(row['publication_id'],row['revision']))],
            'visible':[dict(r) for r in db.execute("""SELECT fragment_id AS id,ordinal,text,presented_at FROM
                       expression_speech_visible WHERE utterance_id=? ORDER BY ordinal""",(row['utterance_id'],))],
        }

    def issue(self, db, turn, source):
        context = json.loads(source['context_json'])
        from .history_context import validate as validate_history
        validate_history(db,turn,context['details'].get('import_history_sources'))
        imported = import_style.frozen(turn)
        history_only = native_policy(turn)=={"history_presentation":1}
        if history_only:
            body = source['body']
        elif imported:
            import_style.validate(self.store,db,turn,context['details'].get(import_style.KEY))
            body = source['body']
        else:
            selected = self.engine.scene.selected(db,turn)
            require(selected and context['details']['scene_voice']['card_sha256']==fingerprint(selected[1]),'source_changed')
            body = batch_view(speech(source['raw_text'],selected[0]))[0]
        require(body and body.strip(),'empty_reply')
        unit = self.outlet._register(db,turn,'main',SOURCE,source['id'],source['raw_hash'],body,
                                    native_policy(turn),{},context)
        self.publisher.publish(db,unit,SOURCE,body,short_frames(body,True),'history_native_reply' if history_only else 'import_native_reply' if imported else 'scene_native_reply')
        db.execute("UPDATE turns SET status='running',usage_json=?,error_code=NULL,updated_at=? WHERE id=?",
                   (source['usage_json'],timestamp(),turn['id']))
        record(db,turn['id'],'generated',source['logical_attempt_id'])

    def main(self, tid, attempt):
        with self.store.connection(write=True) as db:
            turn = self.engine.scene.root(db,tid)
            source = db.execute('SELECT * FROM expression_main_receipts WHERE logical_attempt_id=? AND valid=1',
                                (attempt,)).fetchone()
            require(source is not None,'receipt_missing')
            self.issue(db,turn,source)

    def validate(self, db, turn, unit):
        from .expression_sources import validate
        context = json.loads(unit['context_json'])
        require(native_policy(turn)==json.loads(unit['policy_json']),'source_changed')
        if import_style.frozen(turn):
            import_style.validate(self.store,db,turn,context['details'].get(import_style.KEY))
        elif native_policy(turn)!={'history_presentation':1}:
            selected = self.engine.scene.selected(db,turn)
            require(selected is not None and fingerprint(selected[1])==context['details']['scene_voice']['card_sha256'],'source_changed')
        source = db.execute('SELECT raw_hash,raw_text FROM expression_main_receipts WHERE id=? AND turn_id=? AND valid=1',
                            (unit['source_id'],turn['id'])).fetchone()
        require(source and source['raw_hash']==unit['source_hash']==text_hash(source['raw_text'])
                and unit['body_hash']==text_hash(unit['body']),'source_changed')
        validate(self.store,db,turn,context)
        return turn

    def stop_before(self, latest):
        """A new turn settles the previous presentation before its context is read."""
        with self.store.connection(write=True) as db:
            rows = db.execute("""SELECT u.* FROM expression_speech_units u JOIN turns t ON t.id=u.turn_id
                WHERE u.source_kind=? AND u.session_id=? AND u.closed_at IS NULL
                AND t.rowid<(SELECT rowid FROM turns WHERE id=?)""",(SOURCE,latest['session_id'],latest['id'])).fetchall()
            for unit in rows:
                pub = db.execute('SELECT * FROM expression_speech_publications WHERE utterance_id=?',(unit['id'],)).fetchone()
                if pub:
                    self.publisher._stop(db,dict(unit),pub,'scene_superseded')
                    current = db.execute('SELECT * FROM expression_speech_publications WHERE id=?',(pub['id'],)).fetchone()
                    if current['state']=='unknown':
                        self.publisher._close(db,dict(unit),current,'scene_presentation_unknown')

    def recover(self):
        from .migration import migration_pending
        if migration_pending(self.store.paths):
            return
        with self.store.connection(write=True) as db:
            db.execute("""UPDATE knowledge_control SET value=json_set(value,'$.reason',
                  CASE WHEN json_extract(value,'$.started') IS NULL THEN 'restart_before_dispatch' ELSE 'restart_unknown' END,
                  '$.scene','fallback') WHERE key LIKE 'scene.route.%' AND json_extract(value,'$.reason') IS NULL""")
            open_items = db.execute("""SELECT u.id FROM expression_speech_units u JOIN expression_speech_publications p
                  ON p.utterance_id=u.id WHERE u.source_kind=? AND p.state NOT IN ('closed','complete')""",(SOURCE,)).fetchall()
            for row in open_items:
                unit = self.outlet.unit(db,row['id'])
                pub = db.execute('SELECT * FROM expression_speech_publications WHERE utterance_id=?',(row['id'],)).fetchone()
                if pub['state']=='unknown':
                    self.publisher._close(db,unit,pub,'scene_presentation_unknown')
                elif unit['cancelled_at']:
                    self.publisher._stop(db,unit,pub,'cancelled_by_user')
                else:
                    db.execute("""UPDATE turns SET status='running',error_code=NULL WHERE id=? AND cancelled_at IS NULL
                               AND status='interrupted' AND error_code='service_restarted'""",(unit['turn_id'],))
            receipts = db.execute("""SELECT r.* FROM expression_main_receipts r JOIN turns t ON t.id=r.turn_id
                WHERE r.valid=1 AND t.status='interrupted' AND t.error_code='service_restarted' AND t.cancelled_at IS NULL
                AND (json_extract(t.context_policy_snapshot,'$.scene_voice') IS NOT NULL
                     OR json_extract(t.context_policy_snapshot,'$.import_style') IS NOT NULL
                     OR json_extract(t.origin_json,'$.history_presentation')=1)
                AND NOT EXISTS(SELECT 1 FROM expression_speech_units u WHERE u.turn_id=t.id AND u.phase='main')""").fetchall()
            for source in receipts:
                root = self.engine.scene.root(db,source['turn_id'])
                db.execute("UPDATE turns SET status='running' WHERE id=?",(root['id'],))
                try:
                    self.issue(db,root|{'status':'running'},source)
                except (RuntimeErrorBase, OSError, ValueError):
                    db.execute("UPDATE turns SET status='interrupted',error_code='scene_receipt_requires_review' WHERE id=?",
                               (root['id'],))
