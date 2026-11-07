"""Purpose-labelled rendering of already selected companion material. No IO."""
import json

from .archive import fingerprint
from .models import RuntimeErrorBase

VERSION = 'companion-persona-v2'
INSTRUCTION_VERSION = 'companion-minimal-v2'
INSTRUCTION = (
    '你就是下面这个人物，正在和对方私聊。用自己的口吻参与眼前的交流。'
    '可以自然联想、想象和补白，尊重对方明确的更正与拒绝；认真任务把内容做好。'
    '下面的人物资料分别描述你的身份、过往与待人分寸。记忆和引文按其来源理解，'
    '相关描述不代表已经执行了现实操作。'
)
SECTIONS = (
    ('identity', '你是谁', '照这个人的身份、特点和口吻参与交流。'),
    ('life', '你的过往和生活', '这些是你自己的生活底色，话题相关时自然带出；'
     '与对方共同发生的经历以实际对话与记忆为准。'),
    ('conduct', '你待人做事的分寸', '让这些分寸体现在你的判断和回应中，'
     '交流确实涉及相关边界时再自然说明。'),
    ('examples', '说话示范', '从这些示范体会人物的说话方式。'),
)
FIELDS = {
    'identity.aliases': ('identity', '别名'), 'identity.species': ('identity', '物种'),
    'identity.gender': ('identity', '性别'), 'identity.pronouns': ('identity', '称谓'),
    'age.years': ('identity', '年龄'), 'age.birthday': ('identity', '生日'),
    'appearance.height_cm': ('identity', '身高'), 'appearance.weight_kg': ('identity', '体重'),
    'appearance.blood_type': ('identity', '血型'), 'appearance.hair_color': ('identity', '发色'),
    'appearance.eye_color': ('identity', '瞳色'), 'appearance.description': ('identity', '外貌'),
    'biography.hometown': ('life', '家乡'), 'biography.occupation': ('life', '职业'),
    'biography.education': ('life', '教育经历'), 'biography.family': ('life', '家庭'),
    'biography.relationships': ('life', '背景关系'),
    'interests.likes': ('life', '喜欢'), 'interests.dislikes': ('life', '讨厌'),
    'interests.hobbies': ('life', '爱好'), 'interests.notes': ('life', '兴趣说明'),
    'social.public_image': ('identity', '社交形象'), 'social.humor': ('identity', '幽默方式'),
    'social.context_rules': ('conduct', '情境分寸'),
    'world.name': ('life', '世界'), 'world.description': ('life', '世界背景'),
}
HARD_FIELDS = frozenset(('age.years', 'age.birthday', 'appearance.height_cm',
                         'appearance.weight_kg', 'appearance.blood_type',
                         'identity.species', 'identity.gender'))
UNITS = {'cm': '厘米', 'kg': '千克', 'years': '岁'}
UNCERTAINTY = '对资料缺乏明确支持的年龄、生日等具体身份信息，回答保留不确定性。'


def render(spec, parts):
    """Keep selection and trimming upstream; return metadata without source text."""
    sections = {key: [] for key, _, _ in SECTIONS}
    rows = []
    seen = {}
    included = []
    missing_hard = []
    budgets = {'anchor': 0, 'dynamic': 0, 'examples': 0}

    def add(section, field, label, value, kind, *, unit=None):
        status = 'unset' if value is None or value == [] or value == '' else 'value'
        row = {'section': section, 'field_id': field, 'source_kind': kind,
               'status': status, 'included': status == 'value', 'characters': 0}
        rows.append(row)
        if status == 'unset':
            row['reason'] = 'unset_omitted'
            return False
        value_hash = fingerprint([value, unit])
        key = (kind if kind in ('lore','example') else 'field', field)
        if key in seen:
            if seen[key] != value_hash:
                raise RuntimeErrorBase('companion_persona_duplicate_conflict')
            row['reason'] = 'same_field_merged'
            return True
        seen[key] = value_hash
        text = '\n'.join(str(item) for item in value) if isinstance(value, list) else str(value)
        if unit:
            if unit not in UNITS:
                raise RuntimeErrorBase('companion_persona_unit_unknown')
            text += UNITS[unit]
        text = label + '\n' + text + '\n'
        sections[section].append(text)
        row['characters'] = len(text)
        row['content_hash'] = fingerprint(text)
        budget = {'anchor': 'anchor', 'fact': 'dynamic', 'lore': 'dynamic',
                  'example': 'examples'}.get(kind)
        if budget:
            budgets[budget] += len(text)
        return True

    for key, section, label in (
        ('name', 'identity', '名称'), ('introduction', 'identity', '自我介绍'),
        ('core_traits', 'identity', '核心特点'), ('expression_style', 'identity', '表达方式'),
        ('initial_background', 'life', '初始背景'),
        ('values_and_boundaries', 'conduct', '价值与分寸'),
    ):
        field = 'expression.style' if key == 'expression_style' else 'core.' + key
        add(section, field, label, spec[key], 'profile')

    for part in parts:
        payload = json.loads(part['text'])
        kind, field = part['kind'], part['id']
        if payload.get('kind') != kind or payload.get('id') != field:
            raise RuntimeErrorBase('companion_persona_part_mismatch')
        value = payload['data']
        if kind in ('anchor', 'fact'):
            if field not in FIELDS:
                raise RuntimeErrorBase('companion_persona_field_unknown')
            section, label = FIELDS[field]
            unit = None
            if kind == 'fact':
                unit = value['unit']
                status = value['status']
                value = value['value']
                if status not in ('value', 'unset') or (status == 'unset') != (value is None or value == []):
                    raise RuntimeErrorBase('companion_persona_fact_mismatch')
                if status == 'unset' and field in HARD_FIELDS:
                    missing_hard.append(field)
            if add(section, field, label, value, kind, unit=unit):
                included.append(part)
        elif kind == 'lore_group':
            for entry in value:
                add('life', entry['id'], entry['title'], entry['text'], 'lore')
            included.append(part)
        elif kind == 'example':
            add('examples', field, '情境 ' + value['scenario'], value['text'], 'example')
            included.append(part)
        else:
            raise RuntimeErrorBase('companion_persona_kind_unknown')

    if missing_hard:
        sections['conduct'].append(UNCERTAINTY + '\n')
    text = ''
    section_rows = []
    for key, title, purpose in SECTIONS:
        if key == 'examples' and not sections[key]:
            continue
        block = '\n' + title + '\n' + purpose + '\n' + ''.join(sections[key])
        text += block
        section_rows.append({'id': key, 'characters': len(block), 'content_hash': fingerprint(block)})
    budgets['total'] = sum(budgets.values())
    if budgets['total'] > 3000 or budgets['examples'] > 600:
        raise RuntimeErrorBase('companion_persona_budget_exceeded')
    diagnostic = {
        'version': VERSION, 'instruction_version': INSTRUCTION_VERSION,
        'sections': section_rows, 'fields': rows, 'budgets': budgets,
        'selected_parts': [{'kind': p['kind'], 'id': p['id']} for p in parts],
        'included_parts': [{'kind': p['kind'], 'id': p['id']} for p in included],
        'hard_uncertainty_fields': missing_hard,
        'history_symptom_review': {'status': 'not_reviewed', 'source': None},
        'characters': len(text), 'content_hash': fingerprint(text), 'details_omitted': False,
    }
    return text, diagnostic, included
