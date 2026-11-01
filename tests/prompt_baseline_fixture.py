"""R0 artificial, fixed-ID/time inputs. No configured clients or private materials."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from itertools import count
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from persona_runtime.archive import fingerprint, scope
from persona_runtime.character import empty_character
from persona_runtime.context_m4 import build_context
from persona_runtime.dialogue_voice import freeze
from persona_runtime.decision_capsule import capsule
from persona_runtime.models import generic_persona, LOCAL_USER_ID
from persona_runtime.paths import RuntimePaths
from persona_runtime.profile import snapshot, append_version
from persona_runtime.provider_compat import build_request
from persona_runtime.reply_batch import expression_snapshot
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store
from tests.m71_fixtures import seed_turn, insert

DAY = '2026-11-01T00:00:00.000000+00:00'


class FixedTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromisoformat(DAY).astimezone(tz or timezone.utc)


def cases():
    result = [{'name': mode, 'mode': mode} for mode in
              ('legacy_full', 'context_full', 'context_structured', 'natural_structured')]
    result += [{'name': f'expression-v{v}', 'mode': 'natural_structured', 'expression': v} for v in (1, 2, 3)]
    result += [{'name': f'{provider}-probe-{mode}', 'provider': provider, 'probe': True, 'mode': mode}
               for provider in ('generic', 'deepseek') for mode in ('legacy_full', 'natural_structured', 'context_structured')]
    result += [{'name': name, **options} for name, options in (
        ('v2', {'voice': True, 'mode': 'natural_structured', 'expression': 3}),
        ('v2-old-format', {'voice': True, 'mode': 'context_structured', 'expression': 1}),
        ('v2-probe', {'voice': True, 'probe': True, 'mode': 'natural_structured', 'expression': 3}),
        ('ablation-a', {'voice': True, 'ablation': True}),
        ('invalid-voice', {'invalid_voice': True}),
        ('invalid-mode', {'mode': 'unknown'}),
        ('invalid-expression', {'mode': 'natural_structured', 'invalid_expression': True}),
        ('shadow', {'decision_mode': 'shadow'}),
        ('assist', {'decision_mode': 'assist'}),
        ('assist-v2', {'decision_mode': 'assist', 'voice': True}),
        ('assist-not-applicable', {'decision_mode': 'assist', 'unknown': True}),
        ('capsule-fallback', {'decision_mode': 'assist', 'large_anchor': True}),
        ('trim-examples', {'limit': 1500}),
        ('trim-history-memory', {'limit': 800}),
        ('required-input-too-large', {'limit': 10}),
        ('state-expired', {'expired': True}),
        ('relationship-blocked', {'blocked': True}),
        ('explicit-reference', {'reference': True}),
        ('reference-invalid', {'reference': True, 'blocked': True}),
        ('reference-over-budget', {'reference': True, 'limit': 10}),
        ('qq-sent-history', {'channel': 'qq'}),
    )]
    return result


def capture(case, builder=build_context):
    ids = count(100)
    with ExitStack() as stack:
        stack.enter_context(patch('persona_runtime.models.uuid4', side_effect=lambda: UUID(int=next(ids))))
        stack.enter_context(patch('persona_runtime.models.datetime', FixedTime))
        stack.enter_context(patch('persona_runtime.profile.datetime', FixedTime))
        root = stack.enter_context(tempfile.TemporaryDirectory(prefix='r0-artificial-'))
        store = Store(RuntimePaths(Path(root))); store.initialize()
        service = RuntimeService(store)
        pid = service.create_persona(generic_persona())['id']; service.use_persona(pid)
        sid = service.create_session()['id']
        channel = case.get('channel', 'desktop'); bot, peer = ('fake-bot', 'fake-peer') if channel == 'qq' else ('', '')
        with store.connection(write=True) as db:
            for i in range(3):
                seed_turn(db, pid, sid, f'h{i}', ['青禾预算三百元', '更正，预算四百元'],
                          day=DAY, channel=channel, bot=bot, peer=peer, reply='已发送的人工片段。')
            seed_turn(db, pid, sid, 'current', ['想聊青禾预算', '先等等，更正成五百元。你多高？'],
                      day=DAY, channel=channel, bot=bot, peer=peer, status='queued')
            append_version(db, pid, 'relationship', {'description': '人工关系说明'}, source_ids=['h0-e1'])
            append_version(db, pid, 'state', {'text': '人工临时状态', 'expires_at': '2000-01-01T00:00:00+00:00' if case.get('expired') else '2099-01-01T00:00:00+00:00'})
            if case.get('reference'):
                from persona_runtime.references import capture as bind
                bind(db, 'current-e1', scope(pid, channel, bot, peer), {'kind': 'event', 'id': 'h0-e1'})
            if case.get('blocked'):
                insert(db, 'source_exclusions', dict(event_id='h0-e1', persona_id=pid, user_id=LOCAL_USER_ID, reason='artificial withdrawal', created_at=DAY))
            if case.get('summary'):
                insert(db,'organization_tasks',dict(id='synthetic-summary-task',request_id='synthetic-summary-task',
                    persona_id=pid,user_id=LOCAL_USER_ID,session_id=sid,channel=channel,mode='summary',
                    fingerprint='synthetic',payload_json='{}',model_snapshot='{}',status='completed',created_at=DAY,updated_at=DAY))
                insert(db,'summaries',dict(id='synthetic-summary',persona_id=pid,user_id=LOCAL_USER_ID,
                    session_id=sid,channel=channel,task_id='synthetic-summary-task',revision=1,start_row=1,end_row=1,
                    items_json=json.dumps([{'text':'人工青禾摘要','sources':[{'event_id':'h0-e1'}]}],ensure_ascii=False),
                    sources_json='["h0-e1"]',covered_json='[1]',status='active',created_at=DAY))
        service.add_memory('preference', '青禾预算只用于人工测试。', 'h1-e1')
        mode = case.get('mode', 'context_full')
        model = {'base_url': 'https://api.deepseek.com' if case.get('provider') == 'deepseek' else 'https://example.invalid',
                 'name': 'deepseek-flash' if case.get('provider') == 'deepseek' else 'fake',
                 'max_tokens': 2048, 'max_input_chars': case.get('limit', 18000)}
        if case.get('voice'): model['_dialogue_profile'] = freeze('fixed-artificial-seed', ['想聊青禾预算'])
        if case.get('invalid_voice'): model['_dialogue_profile'] = {'version': 'invalid'}
        expression = expression_snapshot(mode, 'fixed-expression-policy', version=case.get('expression', 1))
        if case.get('invalid_expression'): expression['version'] = 99
        data = empty_character(); data['identity']['species'] = '人工测试角色'
        data['appearance']['height_cm'] = 170
        data['examples'] = [dict(id=f'ex{i}', enabled=True, scenario='explain', keywords=['青禾'], text='人工样例 '+str(i)+' 啊，五百元那就够啦。') for i in range(4)]
        version = {'id': 'fixed-character', 'revision': 1, 'data': data}
        from persona_runtime.character_context import select_context
        character = select_context(version, '青禾 你多高？', example_limit=4)
        if case.get('large_anchor'):
            character['parts'] = [{'kind': 'anchor', 'id': 'world.name', 'required': True, 'text': '人工必需资料' * 600}]
        decision = {'mode': case.get('decision_mode', 'baseline'), 'status': 'observed',
                    'record': {'activated': [] if case.get('unknown') else ['synthetic'], 'chosen_action': 'support', 'status': 'selected'}}
        if case.get('voice'): decision['_voice_version'] = 'natural-dialogue-v2'
        engine = SimpleNamespace()
        if case.get('ablation'): engine.evaluation_session = SimpleNamespace(get=lambda *args: {'arm': 'A'})
        with store.connection() as db:
            turn = dict(db.execute("SELECT * FROM turns WHERE id='current'").fetchone())
            turn.update(profile_snapshot=json.dumps(snapshot(db, pid), ensure_ascii=False),
                        character_snapshot=json.dumps(version, ensure_ascii=False), model_snapshot=json.dumps(model),
                        reply_mode_snapshot=mode, expression_snapshot=json.dumps(expression))
            if case.get('probe'): turn['channel'] = 'test'
            try:
                messages, details = builder(engine, db, turn, character=character, capsule=capsule(decision))
                # Only this additive observation is outside the frozen old details contract.
                details = {k:v for k,v in details.items() if k != 'prompt_assembly'}
                payload, wire = build_request(model | {'_reply_mode': mode, '_expression_snapshot': turn['expression_snapshot']}, messages)
                return {'messages': messages, 'input_snapshot_hash': fingerprint(messages), 'provider_payload': payload,
                        'provider_contract': wire, 'details': details}
            except Exception as exc:
                return {'error_type': type(exc).__module__+'.'+type(exc).__name__, 'error_code': str(exc)}


def capture_all():
    return {case['name']: capture(case) for case in cases()}
