"""Project an approved selection into a v8 request using the reviewed repair contract."""
from difflib import SequenceMatcher
import re
import unicodedata

from .expression_corpus_sources import require


def content_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def projection_reason(asset):
    """Use long quote overlap only as an exclusion from this optional projection.

    General descriptions and short reactions may pass. An expression asset
    alone cannot establish that the character shared the source speaker's past.
    """
    style = asset['style']
    source = asset['source_quote']
    if '括号' in style and re.search(r'(?:（\s*）|\(\s*\))', source):
        return 'source_blank_parenthetical_instruction'
    for match in re.finditer(r'“([^”]*)”|「([^」]*)」|‘([^’]*)’|"([^"]*)"', style):
        example = next(group for group in match.groups() if group is not None)
        if '…' in example or '...' in example:
            return 'unbound_example_slot'
        quote, original = content_key(example), content_key(source)
        if SequenceMatcher(None, quote, original, autojunk=False).find_longest_match().size >= 4:
            return 'source_literal_four_or_more'
    return None


def repair_messages(baseline_messages, binding, thought, selected_assets, current_input):
    require(baseline_messages[-1] == {'role': 'user', 'content': current_input}, 'v8_target_changed')
    variables = binding['variables']
    chosen, omitted = [], {}
    for asset in selected_assets:
        reason = projection_reason(asset)
        if reason:
            omitted[asset['id']] = reason
        else:
            chosen.append(asset)
    blocks = []
    if chosen:
        wrap = variables['expression_habits_block']
        lines = [wrap['item_format'].format(situation=a['situation'], style=a['style']) for a in chosen]
        expressions = wrap['prefix'] + '\n'.join(lines)
        require(len(expressions + wrap['suffix_if_present']) <= 3000 and len(lines) <= 5, 'expression_capacity')
        blocks.append(expressions)
    if thought.strip() and thought.strip() != current_input.strip():
        blocks.append(variables['planner_reasoning']['format'].format(thought=thought))
    context = [{'role': 'user', 'content': '\n\n'.join(blocks)}] if blocks else []
    messages = [*baseline_messages[:-1], *context, baseline_messages[-1]]
    return messages, [a['id'] for a in chosen], omitted
