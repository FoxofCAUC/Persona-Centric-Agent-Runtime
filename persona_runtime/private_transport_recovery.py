"""Park a candidate request only on positive before-send evidence."""
import time

from .generation_retry import pause_before_send
from .models import RuntimeErrorBase
from .scene_policy import read, save


class PrivateTransportParked(RuntimeErrorBase):
    """The ingress worker owns the next attempt after a network or storage wake."""


def park(db, turn, attempt, stage, binding, meter, error):
    diagnostics = getattr(error, 'diagnostics', None) or {}
    delay = pause_before_send(str(error), diagnostics, getattr(error, 'usage', None))
    if not delay:
        return False
    table, column, prefix = {
        'judge': ('private_reply_nodes', 'attempt_id', 'private.judge.unsent.'),
        'writer': ('private_reply_attempts', 'id', 'private.writer.unsent.'),
        'lookup': ('private_lookup_calls', 'attempt_id', 'private.lookup.unsent.'),
    }[stage]
    tid = turn['id']
    save(db, prefix + attempt, {'turn': tid, 'binding': binding, 'stage': stage,
                                'code': str(error), 'headers_sent': False, 'revision': turn['execution_revision']})
    if db.execute(f"DELETE FROM {table} WHERE {column}=? AND turn_id=? AND state='sending'",
                  (attempt, tid)).rowcount != 1:
        raise RuntimeErrorBase('private_dispatch_receipt_conflict')
    db.execute('DELETE FROM model_call_meter WHERE id=? AND started=?', meter)
    park_turn(db,turn,delay)
    return True


def park_turn(db,turn,delay):
    tid=turn['id']
    mark = read(db, 'desktop.network.' + tid) or {}
    count = mark.get('probes', 0) + 1
    save(db, 'desktop.network.' + tid, {'revision': turn['execution_revision'], 'probes': count,
                                       'due': time.time() + min(60, 2**min(count, 6))})
    db.execute("""UPDATE turns SET status='queued',error_code=? WHERE id=? AND execution_revision=?
                  AND status!='completed' AND cancelled_at IS NULL""", ('desktop_'+delay+'_wait', tid, turn['execution_revision']))
