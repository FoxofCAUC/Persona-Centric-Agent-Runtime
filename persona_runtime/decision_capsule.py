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


VOICE_TEMPLATES={
    'respond':'把自己的回应说出来，直接接住话题。',
    'acknowledge':'这一刻重点是跟对方一起确认、庆祝或感叹，反应可以很短。',
    'ask':'目前还缺完成任务的关键资料。问清那一个实际缺口即可。',
    'clarify':'把真正冲突或不清楚的那一点说开。',
    'support':'站在对方这边陪这一刻，说你对这件具体事情的反应。支持可以是心疼、替他不值、一起吐槽或安静接话，不用复述情绪，不急着修理处境。',
    'explain':'把自己的思路和为什么这样想说清楚，让对方能跟上。日常建议像交换想法，技术任务保留完整步骤。',
    'challenge':'对方希望听到质疑，坦率指出站不住的地方并说出依据。',
    'defer':'如实说清这次做不了的地方。',
    'set_boundary':'把有来源的那条界限说清楚，语气自然。',
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


@dataclass(frozen=True)
class LiveAdmission:
    """Process-only grant. Development requires the original isolated Assist guard.

    Production release must supply a frozen review package plus platform/quality
    evidence; absence of either never enables a policy through an HTTP request.
    """
    isolation: AssistAdmission
    release_report: Path | None = None
    initialize_new: bool = False

    def check(self, store, config, persona_id):
        if self.release_report is None:
            self.isolation.check(store,config,persona_id)
            return
        import json
        from .private_personas import safe
        path=safe(self.release_report,store.paths.home/'exports/acceptance')
        report=json.loads(path.read_text(encoding='utf-8'))
        from .evaluation_metrics import quality_gates
        from hashlib import sha256
        code={p.name:sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
        from .evaluation import EvaluationStore
        evaluation=EvaluationStore(store.paths)
        with evaluation.connect(report['package_id']) as db:
            frozen=json.loads(db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()[0])
            simulated=json.loads(db.execute("SELECT value FROM metadata WHERE key='simulated'").fetchone()[0])
            rows=[dict(r) for r in db.execute('SELECT * FROM reviews ORDER BY case_id')]
            judged=[dict(r) for r in db.execute('SELECT c.left_arm,r.body FROM cases c JOIN reviews r ON r.case_id=c.id')]
        observed={'natural_wins':0,'b_task_success':0,'c_task_success':0,'critical_errors':0}
        for row in judged:
            review=json.loads(row['body']);cside='left' if row['left_arm']=='C' else 'right';bside='right' if cside=='left' else 'left'
            observed['natural_wins']+=review['natural']==cside
            observed['b_task_success']+=review[bside+'_task']=='pass'
            observed['c_task_success']+=review[cside+'_task']=='pass'
            observed['critical_errors']+=review[cside+'_task']=='critical_error'
        if (simulated or any(report.get('counts',{}).get(k)!=v for k,v in observed.items()) or
            not all(quality_gates(report.get('counts')).values()) or report.get('code_hashes')!=code or
            config.paths.home.resolve()!=store.paths.home.resolve() or not frozen or len(rows)!=100 or fingerprint(rows)!=report.get('review_hash') or
            report.get('instance_id')!=config.load()['instance_id'] or
            report.get('home')!=str(store.paths.home.resolve()) or
            not all(report.get('gates',{}).get(k) is True for k in
                ('quality','human_review','platform','privacy','rollback')) or
            not report.get('author_configuration_confirmed') or
            (persona_id not in self.isolation.persona_ids and not self.initialize_new)):
            raise RuntimeErrorBase('live_release_not_admitted')


def require_admission(engine, persona_id, mode='assist'):
    if mode=='live':
        grant=getattr(engine,'live_admission',None)
        if not isinstance(grant,LiveAdmission):raise RuntimeErrorBase('live_not_admitted')
        grant.check(engine.store,engine.config,persona_id)
        return
    grant = getattr(engine, 'assist_admission', None)
    if not isinstance(grant, AssistAdmission):
        raise RuntimeErrorBase('assist_not_admitted')
    grant.check(engine.store, engine.config, persona_id)


def load_live_release(store,config):
    """Only an explicitly installed, verified release artifact enables production.

    R05 never installs this artifact. The R07 release procedure owns publication.
    """
    import json
    from .private_personas import safe
    path=safe(store.paths.home/'exports/acceptance/disposition-live-release.json',store.paths.home)
    if not path.exists():return None
    raw=path.read_bytes()
    if len(raw)>128*1024:raise RuntimeErrorBase('live_release_not_admitted')
    report=json.loads(raw);ids=report.get('target_persona_ids')
    if (type(ids) is not list or not 1<=len(ids)<=100 or
        any(type(x) is not str or not 1<=len(x)<=200 for x in ids) or
        type(report.get('initialize_new')) is not bool):raise RuntimeErrorBase('live_release_not_admitted')
    return LiveAdmission(AssistAdmission(store.paths.home,config.load()['instance_id'],frozenset(ids),store.paths.home),path,report['initialize_new'])


def new_identity_mode(store,persona_id):
    callback=getattr(store,'new_identity_mode',None)
    if callback:return callback(persona_id)
    from .config import ConfigStore
    grant=load_live_release(store,ConfigStore(store.paths))
    if grant is None or not grant.initialize_new:return 'baseline'
    grant.check(store,ConfigStore(store.paths),persona_id)
    return 'live'


def capsule(prepared):
    """Only fixed templates from an eligible decision, never author prose."""
    if not prepared or prepared.get('mode') not in ('assist','live') or prepared.get('status') != 'observed':
        return ''
    record = prepared.get('record') or {}
    if not record.get('activated') or record.get('status') == 'unknown_respond':
        return ''
    action = record.get('chosen_action')
    if action not in TEMPLATES:
        raise RuntimeErrorBase('capsule_invalid_action')
    from .dialogue_voice import SUPPORTED_VERSIONS
    if prepared.get('_voice_version') in SUPPORTED_VERSIONS:
        return '\n这一轮更在意：'+VOICE_TEMPLATES[action]+'\n这是交流重心，不是句式或固定顺序。'
    text = '\n当轮交流方式要求（不得覆盖权限、事实、核心边界、当前合法任务或回复协议）：\n' + encoded({
        'schema_version': VERSION, 'action': action, 'requirements': [TEMPLATES[action],
        '完整完成当前合法任务；不得把设定或策略当作真实经历，不增加工具、后台任务或主动联系权限。']})
    if len(text) > 600:
        raise RuntimeErrorBase('capsule_too_large')
    return text


def metadata(prepared, text='', removed=(), reason=None):
    mode=prepared.get('mode','assist')
    return {'requested_mode': mode, 'effective_mode': mode if text else 'baseline',
            'renderer': 'decision-capsule-voice-v2' if prepared.get('_voice_version') else VERSION, 'action': (prepared.get('record') or {}).get('chosen_action'),
            'applied_to_prompt': bool(text), 'capsule_hash': fingerprint(text) if text else None,
            'characters': len(text), 'removed': list(removed),
            'degrade_reason': reason or (None if text else prepared.get('status'))}
