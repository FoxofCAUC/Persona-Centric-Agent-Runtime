"""Single-scene projection for the E.N5 isolated workbench and input review."""
import json
import math

from .archive import fingerprint
from .knowledge_translation import SEARCH_NOTICE
from .models import RuntimeErrorBase
from .prompt_assembly import PromptBlock, assemble

VERSION = 'en5-jev-scene-v1'
SCENES = ('daily', 'relationship', 'help', 'closing', 'fallback')
MAX_CONTEXT = 4000
WAIT_SECONDS = 3.0


def require(ok, code):
    if not ok:
        raise RuntimeErrorBase('scene_' + code)


def context(targets):
    """Targets come from verified, chronological events. Budget whole recent exchanges."""
    recent = sorted({t['pro_target']['message_index'] - int(t['role'] == 'assistant')
                     for t in targets if 'recent' in t['origins']})
    retained = recent[-2:]
    while True:
        state = []
        for item in targets:
            origins = item['origins']
            pair = item['pro_target']['message_index'] - int(item['role'] == 'assistant')
            if 'recent' in origins and pair not in retained:
                origins = [s for s in origins if s != 'recent']
            if origins:
                state.append({'role':item['role'], 'source':origins, 'text':item['text']})
        if len(json.dumps(state, ensure_ascii=False, separators=(',', ':'))) <= MAX_CONTEXT:
            return state
        if not retained:
            return None
        retained = retained[1:]


def jev_request(model, state, instruction, criteria, *, draft=False):
    require(set(criteria) == set(SCENES), 'choice_catalog')
    require(all(isinstance(t, str) for t in (instruction, *criteria.values())), 'invalid_copy')
    if not draft:
        require(all(t.strip() for t in (instruction, *criteria.values())), 'author_copy_missing')
    return {'model':model, 'state':state, 'questions':{'scene':{
        'type':'choice', 'instructions':instruction, 'criteria':dict(criteria)}}}


def decision(raw):
    def unique(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError('duplicate')
        return result
    try:
        response = json.loads(raw, object_pairs_hook=unique)
        answer = response['answers']['scene']
        chosen, probabilities = answer['choice'], answer['probabilities']
        if (answer.get('type') != 'choice' or chosen not in SCENES or set(probabilities) != set(SCENES)
                or any(type(p) not in (float, int) or not math.isfinite(p) or not 0 <= p <= 1
                       for p in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=0.001)):
            raise ValueError
        if probabilities[chosen] < .60 or any(p >= probabilities[chosen] for k,p in probabilities.items() if k != chosen):
            return 'fallback', 'uncertain'
        return chosen, 'selected'
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'fallback', 'invalid'


def explicit_facts(body):
    # The native character block stores each original canon selection on its own JSON line.
    if not body:
        return ''
    header, sep, records = body.partition('\n人物补充资料：\n')
    require(not header and bool(sep), 'character_block')
    lines = []
    for line in records.splitlines():
        item = json.loads(line)
        if item['kind'] == 'fact' and not item['id'].startswith('social.'):
            lines.append(line)
    return sep + '\n'.join(lines) if lines else ''


def project(messages, assembly, card, *, notice=False):
    """Replace the two v8 voice owners, retaining verified factual/context blocks only."""
    require(isinstance(card,str) and card.strip(), 'card_unavailable')
    require(assembly['input_snapshot_hash'] == fingerprint(messages) and messages[0]['role']=='system',
            'assembly_drift')
    system = messages[0]['content']
    blocks = []
    cursor = 0
    voice_count = 0
    for meta in assembly['blocks']:
        if meta['role'] != 'system':
            continue
        start = meta['start_character']
        require(meta['message_index'] == 0 and start == cursor, 'system_span')
        cursor = start + meta['characters']
        text = system[start:cursor]
        require(fingerprint(text) == meta['content_hash'], 'system_hash')
        name = meta['name']
        if name == 'persona_expression':
            require(meta['owner'] == 'companion_im', 'unexpected_baseline')
            blocks.append(PromptBlock('scene_voice', card, 'scene_voice', VERSION))
            voice_count += 1
            continue
        if name == 'character':
            text = explicit_facts(text)
        elif name == 'reply_protocol':
            require(text == (SEARCH_NOTICE if notice else ''), 'reply_protocol')
        elif name not in ('memory_summary','recall_support','explicit_references','search_notice'):
            # Relationship, social style, temporary voices and optional guide blocks are not speech inputs here.
            continue
        if text:
            blocks.append(PromptBlock(name,text,meta['owner'],meta['version']))
    require(cursor == len(system) and voice_count == 1, 'incomplete_system')
    return assemble(blocks, messages[1:-1], messages[-1])
