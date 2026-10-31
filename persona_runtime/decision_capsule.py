"""Bounded rendering and process-local admission. No model or database at import."""
from dataclasses import dataclass
from pathlib import Path
import os

from .archive import encoded, fingerprint
from .models import RuntimeErrorBase
from .paths import DEFAULT_HOME

VERSION = 'decision-capsule-v1'
TEMPLATES = {
    'respond': '直接回应当前内容，不为了展示理解而追问。',
    'acknowledge': '先简短确认或祝贺，再完成用户当前要求。',
    'ask': '只询问完成任务确实缺少的必要信息，已有信息不要重复询问。',
    'clarify': '指出当前明确的歧义或冲突，做必要澄清，不擅自替用户选择。',
    'support': '先简短回应用户的倾诉并表达支持，避免诊断、说教或无必要追问。',
    'explain': '优先清楚解释依据和可行步骤，保留必要答案与细节。',
    'challenge': '按用户明确要求检验或批评观点，给出理由，尊重对方。',
    'defer': '如实说明当前暂不能处理及已知限制，不承诺后台处理或主动联系。',
    'set_boundary': '说明已有授权来源支持的边界，保持尊重，不扩大权限或编造限制。',
}


@dataclass(frozen=True)
class AssistAdmission:
    home: Path
    instance_id: str
    persona_ids: frozenset
    formal_home: Path

    def check(self, store, config, persona_id):
        from .private_personas import safe
        home = safe(store.paths.home)
        formal = self.formal_home.resolve()
        db = home / 'data/database/runtime.sqlite3'
        original = formal / 'data/database/runtime.sqlite3'
        safe(db,home)
        if (config.paths.home.resolve()!=home or home != self.home.resolve() or home in (formal, DEFAULT_HOME.resolve()) or
                not self.persona_ids or persona_id not in self.persona_ids or
                config.load()['instance_id'] != self.instance_id or
                (original.exists() and db.exists() and os.path.samefile(db, original)) or
                (home / 'private/personas/registry.json').exists()):
            raise RuntimeErrorBase('assist_not_admitted')
        from .private_personas import PrivatePersonas
        registry = PrivatePersonas(type(store.paths)(formal))
        if registry.root.exists():
            if any(r.get('persona_id') == persona_id for r in registry.load(True)['records']):
                raise RuntimeErrorBase('assist_not_admitted')


def require_admission(engine, persona_id):
    grant = getattr(engine, 'assist_admission', None)
    if not isinstance(grant, AssistAdmission):
        raise RuntimeErrorBase('assist_not_admitted')
    grant.check(engine.store, engine.config, persona_id)


def capsule(prepared):
    """Only fixed templates from an eligible decision, never author prose."""
    if not prepared or prepared.get('mode') != 'assist' or prepared.get('status') != 'observed':
        return ''
    record = prepared.get('record') or {}
    if not record.get('activated') or record.get('status') == 'unknown_respond':
        return ''
    action = record.get('chosen_action')
    if action not in TEMPLATES:
        raise RuntimeErrorBase('capsule_invalid_action')
    text = '\n当轮交流方式要求（不得覆盖权限、事实、核心边界、当前合法任务或回复协议）：\n' + encoded({
        'schema_version': VERSION, 'action': action, 'requirements': [TEMPLATES[action],
        '完整完成当前合法任务；不得把设定或策略当作真实经历，不增加工具、后台任务或主动联系权限。']})
    if len(text) > 600:
        raise RuntimeErrorBase('capsule_too_large')
    return text


def metadata(prepared, text='', removed=(), reason=None):
    return {'requested_mode': 'assist', 'effective_mode': 'assist' if text else 'baseline',
            'renderer': VERSION, 'action': (prepared.get('record') or {}).get('chosen_action'),
            'applied_to_prompt': bool(text), 'capsule_hash': fingerprint(text) if text else None,
            'characters': len(text), 'removed': list(removed),
            'degrade_reason': reason or (None if text else prepared.get('status'))}
