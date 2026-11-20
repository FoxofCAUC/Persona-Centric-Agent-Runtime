"""Connect a frozen direct reply to the existing receipt-based character outlet."""
from contextlib import nullcontext
import json

from .archive import fingerprint
from .dialogue_generation import DialogueGeneration
from .expression_contract import text_hash
from .expression_utterance_publication import SpeechPublication
from .models import RuntimeErrorBase, timestamp
from .progress import record


class DirectReplies:
    def __init__(self, expression, generation=None):
        self.expression = expression
        self.store = expression.store
        self.generation = generation or DialogueGeneration(self.store)
        expression.outlet.direct = self
        self.publication = SpeechPublication(expression.outlet)

    def register(self, db, turn, phase, model, messages, details, attempt_id, task_id=None):
        policy = self.generation.accepted(db, turn['id'])['data']
        assembly = self.generation.load_assembly(db, turn['persona_id'], policy['bundle_id'])
        material = assembly.freeze(db, turn, policy['bundle_id'], model, messages, phase=phase, details=details)
        if details.get('history_rows') and not details.get('history_groups'):
            groups=[]
            for ordinal in details['history_rows']:
                row=db.execute('SELECT id FROM turns WHERE rowid=? AND persona_id=? AND session_id=?',
                    (ordinal,turn['persona_id'],turn['session_id'])).fetchone()
                if not row:raise RuntimeErrorBase('dialogue_history_scope_changed')
                groups.append({'turn_id':row[0]})
            details=dict(details,history_groups=groups)
        context = self.expression.capture_context(db, turn, details)
        context.update(logical_attempt_id=attempt_id, organization_task_id=task_id)
        return self.generation.register(db, turn, phase, material, context)

    def _assembly(self, db, unit_id):
        row = db.execute("""SELECT t.persona_id, u.material_json FROM dialogue_generation_units u
            JOIN turns t ON t.id=u.turn_id WHERE u.id=?""", (unit_id,)).fetchone()
        if not row:
            raise RuntimeErrorBase('dialogue_unit_missing')
        return self.generation.load_assembly(db, row['persona_id'], json.loads(row['material_json'])['bundle_id'])

    @staticmethod
    def main_info(db, turn_id):
        pub=db.execute("""SELECT p.id,p.revision,p.state,p.producer_kind,p.reason,u.id AS utterance_id
            FROM expression_speech_units u JOIN expression_speech_publications p ON p.utterance_id=u.id
            WHERE u.turn_id=? AND u.phase='main' AND u.source_kind='dialogue_reply'""",(turn_id,)).fetchone()
        return {'draft':None,'run':None,'publication':dict(pub) if pub else None}

    @staticmethod
    def find(db, turn_id, phase):
        from .dialogue_generation import enabled
        if not enabled(db, turn_id):
            return None
        row = db.execute('SELECT id FROM dialogue_generation_units WHERE turn_id=? AND phase=?',
                         (turn_id, phase)).fetchone()
        return row[0] if row else None

    async def generate(self, unit_id, credential_model):
        with self.store.connection() as db:
            assembly = self._assembly(db, unit_id)
        result = await self.generation.execute(unit_id, credential_model, assembly)
        if result['state'] != 'completed':
            raise RuntimeErrorBase(result.get('error_code') or 'dialogue_stage_' + result['state'])
        return result

    @staticmethod
    def _receipt(db, unit_id):
        stage = db.execute("""SELECT * FROM dialogue_generation_stages
            WHERE unit_id=? AND stage='reply' AND state='completed' AND artifact_json IS NOT NULL AND validation_error IS NULL""", (unit_id,)).fetchone()
        if not stage:
            raise RuntimeErrorBase('dialogue_reply_receipt_missing')
        return stage, json.loads(stage['result_json'])

    def validate_publication(self, db, unit):
        context = json.loads(unit['context_json'])
        uid = context['generation_unit_id']
        assembly = self._assembly(db, uid)
        turn, gen, material = self.generation.validate(db, uid, assembly)
        stage, rendered = self._receipt(db, uid)
        body = json.loads(stage['artifact_json'])['display_body']
        if (turn['id'], gen['phase'], stage['id'], stage['raw_hash'], body, text_hash(body)) != (
                unit['turn_id'], unit['phase'], unit['source_id'], unit['source_hash'], unit['body'], unit['body_hash']):
            raise RuntimeErrorBase('dialogue_publication_source_changed')
        if unit['task_id']:
            task = db.execute('SELECT status,error_code FROM organization_tasks WHERE id=?',(unit['task_id'],)).fetchone()
            if not task or task['status'] in ('cancelled','stale','blocked_authorization') or task['error_code']=='migration_requires_review':
                raise RuntimeErrorBase('speech_source_task_inactive')
        return turn

    def authority_window(self, unit_id):
        with self.store.connection() as db:
            assembly = self._assembly(db, unit_id)
        return getattr(assembly, 'authority_window', nullcontext)()

    def publish(self, unit_id):
        with self.authority_window(unit_id), self.store.connection(write=True) as db:
            assembly = self._assembly(db, unit_id)
            turn, gen, material = self.generation.validate(db, unit_id, assembly)
            stage, rendered = self._receipt(db, unit_id)
            body = json.loads(stage['artifact_json'])['display_body']
            if not body.strip():
                raise RuntimeErrorBase('dialogue_empty_projection')
            context = json.loads(gen['context_json'])
            context.update(generation_unit_id=unit_id, notice_response_hash=fingerprint(rendered['body']))
            unit = self.expression.outlet._register(db, turn, gen['phase'], 'dialogue_reply', stage['id'],
                stage['raw_hash'], body, self.generation.accepted(db,turn['id']), {}, context, context['organization_task_id'])
            self.publication.publish(db, unit, 'dialogue_reply', body, assembly.segments(body, material), 'direct_reply')
            if gen['phase']=='main':
                db.execute("UPDATE turns SET status='running',error_code=NULL,updated_at=? WHERE id=?",(timestamp(),turn['id']))
                db.execute("UPDATE attempts SET status='completed',error_code=NULL WHERE id=?", (context['logical_attempt_id'],))
                record(db,turn['id'],'generated',context['logical_attempt_id'])
            return unit['id']

    def recover(self):
        from .dialogue_generation import available
        with self.store.connection() as db:
            if not available(db):return
        self.generation.recover()
        from .migration import migration_pending
        if migration_pending(self.store.paths):return
        with self.store.connection(write=True) as db:
            db.execute("""UPDATE turns SET status='running',error_code=NULL WHERE status='interrupted'
                AND cancelled_at IS NULL AND error_code='service_restarted' AND id IN
                (SELECT u.turn_id FROM expression_speech_units u JOIN expression_speech_publications p
                 ON p.utterance_id=u.id JOIN dialogue_generation_turns g ON g.turn_id=u.turn_id
                 WHERE u.phase='main' AND u.source_kind='dialogue_reply' AND u.closed_at IS NULL
                 AND u.cancelled_at IS NULL AND g.closed_at IS NULL AND p.state NOT IN ('closed','complete'))""")

    async def main(self, unit_id, model):
        await self.generate(unit_id, model)
        return self.publish(unit_id)

    async def notice_result(self, unit_id, model):
        from .chat import ModelCompletion
        await self.generate(unit_id, model)
        with self.store.connection() as db:
            stage, rendered = self._receipt(db, unit_id)
            return ModelCompletion(rendered['body'], None, {'finish_reason':rendered['finish_reason'],
                                   'model':rendered['model'], 'generation_stage_id':stage['id']})

    async def resume_notice_organization(self, unit_id, model, managed):
        """Recover the original organization receipt across the two local commit boundaries."""
        with self.store.connection() as db:
            row=db.execute('SELECT context_json FROM dialogue_generation_units WHERE id=?',(unit_id,)).fetchone()
            context=json.loads(row[0])
            task_id, aid=context['organization_task_id'], context['logical_attempt_id']
            task=db.execute('SELECT status FROM organization_tasks WHERE id=?',(task_id,)).fetchone()
            contract=managed.contract(db,task_id)
            if not task or contract['data'].get('cancel_requested') or task[0] in ('cancelled','stale'):
                raise RuntimeErrorBase('dialogue_notice_inactive')
            if task[0]=='completed':return
            existing=db.execute('SELECT 1 FROM organization_receipts WHERE attempt_id=?',(aid,)).fetchone()
            if existing:return
        # execute consults the immutable stage before allowing an unsent request.
        value=await self.notice_result(unit_id,model)
        from .organization_output import receipt
        from .generation_retry import safe_diagnostics
        audit=receipt(value[0])
        audit.update(status='received',provider=safe_diagnostics(value.diagnostics))
        with self.store.connection(write=True) as db:
            contract=managed.contract(db,task_id)
            old=db.execute('SELECT response_text FROM organization_receipts WHERE attempt_id=?',(aid,)).fetchone()
            if old:
                if old[0]!=value[0]:raise RuntimeErrorBase('search_notice_receipt_conflict')
                return
            owner=db.execute('SELECT task_id FROM organization_attempts WHERE id=?',(aid,)).fetchone()
            if not owner or owner[0]!=task_id or contract['data'].get('cancel_requested'):
                raise RuntimeErrorBase('dialogue_notice_inactive')
            db.execute('INSERT INTO organization_receipts VALUES(?,?,?)',(aid,value[0],json.dumps(audit)))
            contract['data']['phase']='received'
            managed.save(db,contract)
            db.execute("UPDATE organization_tasks SET status='queued',error_code=NULL WHERE id=?",(task_id,))

    async def present_notice(self, turn_id):
        with self.store.connection() as db:
            uid = self.find(db, turn_id, 'search_notice')
            prior = db.execute("SELECT id FROM expression_speech_units WHERE turn_id=? AND phase='search_notice' AND source_kind='dialogue_reply'",(turn_id,)).fetchone()
        if not uid:
            raise RuntimeErrorBase('dialogue_notice_unit_missing')
        speech_id = prior[0] if prior else self.publish(uid)
        with self.store.connection() as db:
            unit = self.expression.outlet.unit(db,speech_id)
            pub = db.execute("SELECT state FROM expression_speech_publications WHERE utterance_id=?", (speech_id,)).fetchone()
            if pub and pub[0]=='complete':
                return
            if not pub or unit['cancelled_at'] or unit['closed_at'] or pub[0]=='closed':
                raise RuntimeErrorBase('speech_notice_not_fully_presented')
        return speech_id
