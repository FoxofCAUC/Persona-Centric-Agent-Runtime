"""Render and acknowledge scene bubbles one at a time in the ordinary Workbench."""
from datetime import datetime, timezone

from .models import identifier


class SceneBubbles:
    resources = {'scene-bubble'}
    blockers = {'send','chat','persona','chat-cancel','notice-display','scene-bubble'}

    def __init__(self, window):
        self.window = window
        self.presenter = identifier()
        self.pending = None

    def scope(self):
        w = self.window
        return (w.view_epoch,w.persona_id,w.session_id)

    def ready(self, scope):
        w = self.window
        return (scope==self.scope() and not w.closed and w.current_page=='chat' and not w.tray_hidden
                and w.transcript.winfo_viewable() and w.state() not in ('withdrawn','iconic')
                and not any(op.resources & self.blockers for op in w.hub.active.values()))

    def insert(self, turn):
        speech = turn.get('scene_speech')
        if not speech:
            return
        widget = self.window.transcript
        visible = speech['visible']
        for part in visible:
            widget.insert('end','人格：'+part['text']+'\n\n')
        pending = self.pending
        if (pending and pending['scope']==self.scope() and pending['turn_id']==turn['id']
                and pending['state']!='not_presented'
                and all(part['id']!=pending['fragment_id'] for part in visible)):
            widget.insert('end','人格：'+pending['text']+'\n\n','scene-current-bubble')

    def receipt(self):
        if not self.pending:
            return None
        fields = ('persona_id','session_id','utterance_id','fragment_id','lease_id','presenter_id','receipt_id',
                  'body_hash','state')
        return {key:self.pending[key] for key in fields}

    def confirm(self):
        w = self.window
        pending = self.pending
        args = self.receipt()
        if not args:
            return
        def done(_):
            if self.pending is pending:
                self.pending = None
                # The next snapshot contains this receipt. Until it arrives, the
                # widget keeps the line that was already drawn.
        def failed(error):
            if str(error) in ('speech_publication_closed','speech_receipt_conflict'):
                done(None)
                w.render_chat()
        w.run_task('确认人物发言已呈现',self.resources,
                  lambda _:w.client.manage('expression.v2.ack',**args),done,failure=failed,quiet=True)

    def pump(self):
        w = self.window
        scope = self.scope()
        if not self.ready(scope):
            return
        if self.pending:
            self.confirm()
            return
        candidate = next((t for t in w.turns if t.get('scene_speech') and
                          t['scene_speech']['state'] in ('pending','partial')),None)
        if not candidate:
            return
        speech = candidate['scene_speech']
        item = next((v for v in speech['items'] if v['state']!='presented'),None)
        if not item or item['state']!='pending':
            return
        visible = speech['visible']
        if visible:
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(visible[-1]['presented_at'])).total_seconds()
            if age*1000<item['delay_ms']:
                return
        owner = dict(persona_id=scope[1],session_id=scope[2],utterance_id=speech['utterance_id'],
                     presenter_id=self.presenter)
        def offer(_):
            value = w.client.manage('expression.v2.offer',**owner,
                publication_id=speech['publication_id'],revision=speech['revision'],ordinal=item['ordinal'])
            if value.get('state')=='offered' and not value.get('lease_id'):
                # A lost local reply to the offer never furnished a body to paint.
                for lease in w.client.manage('expression.v2.pending_receipts',**owner):
                    if lease['id']==value['id']:
                        return lease | {'body':None}
            return value

        def paint(value):
            if value.get('state')!='offered' or not value.get('lease_id'):
                return
            painted = value['body'] is not None and self.ready(scope)
            self.pending = owner | dict(scope=scope,turn_id=candidate['id'],fragment_id=value['id'],
                lease_id=value['lease_id'],receipt_id=identifier(),body_hash=value['body_hash'],text=value['body'],
                state='presented' if painted else 'not_presented')
            if painted:
                w.render_chat()
                marks = w.transcript.tag_ranges('scene-current-bubble')
                if marks:
                    w.transcript.see(marks[0])
                    w.transcript.update_idletasks()
                if not marks or not w.transcript.bbox(marks[0]) or not self.ready(scope):
                    self.pending['state']='not_presented'
                    w.render_chat()
            self.confirm()
        w.run_task('读取下一条人物发言',self.resources,offer,paint,quiet=True)
