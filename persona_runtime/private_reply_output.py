"""Machine-readable limits and local format receipts for approved private-reply bundles."""
import json

from .archive import encoded
from .bubble_limits import BubbleLimitError, MAX_BUBBLE_CHARS, limit_ranges
from .models import RuntimeErrorBase
from .scene_style import short_frames


VERSION = 'private-reply-output-v1'
ADVISORY_VERSION = 'private-reply-output-v2'
SEMANTIC_REVIEW_VERSION = 'private-reply-output-v3'
FACT_REVIEW_VERSION = 'private-reply-output-v4'
SEARCH_SPEECH_V1 = 'phase-scoped-search-speech-v1'
SEARCH_SPEECH_VERSION = 'phase-scoped-search-speech-v2'
ADVISORY_VERSIONS = (ADVISORY_VERSION, SEMANTIC_REVIEW_VERSION, FACT_REVIEW_VERSION)


def limits(phase, mode, *, compact_notice=False):
    single_bubble = phase == 'search_notice' or mode in ('short_answer', 'long_detail')
    short_notice = compact_notice and phase == 'search_notice'
    return {
        'phase': phase,
        'mode': mode,
        'max_bubble_chars': 24 if short_notice else MAX_BUBBLE_CHARS,
        'max_bubbles': 1 if single_bubble else None,
        'sentences_per_bubble': 1 if short_notice or (phase == 'main' and single_bubble) else None,
        'output_fields': ['body', 'detail'] if mode == 'long_detail' else ['body'],
    }


def assess(body, detail, contract, *, advisory=False):
    from .private_lookup import sentence_count
    failed = []
    sentence_total = sentence_count(body)
    if contract['sentences_per_bubble'] == 1 and (sentence_total != 1 or '\n' in body.strip() or '\r' in body.strip()):
        failed.append('sentences_per_bubble')
    if 'detail' in contract['output_fields'] and not detail:
        failed.append('detail_required')
    frames = short_frames(body, contract['max_bubbles'] is None)
    if not advisory:
        try:
            frames = limit_ranges(body, frames)
        except BubbleLimitError:
            failed.append('protected_bubble_range')
    if (contract['max_bubbles'] == 1 and len(body) > contract['max_bubble_chars']
            or advisory and any(frame['end']-frame['start'] > contract['max_bubble_chars'] for frame in frames)):
        failed.append('max_bubble_chars')
    return {
        'failed_constraints': failed,
        'observed_counts': {'body_characters': len(body), 'sentences': sentence_total, 'bubbles': len(frames)},
    }


def record_format(db, attempt, constraints, assessment, *, blocks_review):
    key = 'private.reply.format.' + attempt['id']
    receipt = {
        'body_sha256': attempt['body_sha256'],
        'output_constraints': constraints,
        **assessment,
        'semantic_review': 'skipped_for_format' if blocks_review else 'not_used',
    }
    db.execute('INSERT OR IGNORE INTO knowledge_control VALUES (?,?)', (key, encoded(receipt)))
    existing = db.execute('SELECT value FROM knowledge_control WHERE key=?', (key,)).fetchone()
    prior=json.loads(existing[0])
    if not blocks_review and prior.get('semantic_review')=='separate':
        receipt['semantic_review']='separate'
    if prior != receipt:
        raise RuntimeErrorBase('private_reply_format_receipt_changed')
    return 'format/' + attempt['id']
