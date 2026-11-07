"""Native companion text without injected dialogue examples or output templates."""
from .prompt_assembly import PromptBlock

VERSION = 'companion-no-examples-v1'
FORMAT_VERSION = 'native-companion-text-v1'


def consume(parts):
    kept = [p for p in parts if p['kind'] != 'example']
    excluded = [{'kind': p['kind'], 'id': p['id'], 'reason': 'native_dialogue_examples_off'}
                for p in parts if p['kind'] == 'example']
    return kept, {'version': VERSION, 'field_id': 'examples', 'status': 'applied',
                  'excluded_parts': excluded}


def render_blocks(blocks, parts):
    character = '\n人物补充资料：\n' + '\n'.join(p['text'] for p in parts) if parts else ''
    return [PromptBlock(b.name, character, 'companion_native', VERSION) if b.name == 'character'
            else PromptBlock(b.name, '', 'companion_native', FORMAT_VERSION) if b.name == 'reply_protocol'
            else b for b in blocks], character
