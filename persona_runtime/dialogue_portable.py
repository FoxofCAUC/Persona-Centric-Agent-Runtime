"""Versioned projection of approved expression assets into a direct persona reply."""
import json
import re

from .archive import read_exact, turn_scope
from .dialogue_assembly import DialogueAssembly, fill, moment, wire
from .dialogue_repair import projection_reason, repair_messages
from .expression_corpus_sources import require


PROFILE = 'direct-reviewed-speaker-roles-v2'
RECENT_TURNS = 2
EXCLUSIONS = frozenset({
    'source_speaker_state', 'source_event_or_plan', 'world_or_time_condition',
    'concrete_referent', 'unresolved_example', 'not_reviewed_for_projection',
})


def review_manifest(original, portable_ids, excluded_reasons=None):
    """Create private review metadata; the selector and style strings stay verbatim."""
    ids = list(original['candidate_numbers'].values())
    allowed = set(portable_ids)
    reasons = dict(excluded_reasons or {})
    require(not original.get('assembly_profile') and len(allowed) == len(portable_ids)
            and allowed <= set(ids) and set(reasons) <= set(ids) - allowed
            and set(reasons.values()) <= EXCLUSIONS, 'portable_review_scope')
    return original | {'assembly_profile':PROFILE, 'expression_projection':{
        key: 'portable' if key in allowed else reasons.get(key, 'not_reviewed_for_projection')
        for key in ids}}


def adapt_bundle(assembly):
    bundle_id = assembly._current_bundle_id
    manifest, _ = assembly._bundles[bundle_id]
    version = manifest.get('assembly_profile')
    if version is None:
        return assembly
    scope = manifest.get('expression_projection')
    require(version == PROFILE and isinstance(scope, dict)
            and set(scope) == set(manifest['candidate_numbers'].values())
            and set(scope.values()) <= EXCLUSIONS | {'portable'}, 'portable_profile')
    result = PortableAssembly(assembly.store)
    result._bundles[bundle_id] = assembly._bundles[bundle_id]
    result._current_bundle_id = bundle_id
    return result


class PortableAssembly(DialogueAssembly):
    @staticmethod
    def material(*args, **kwargs):
        return DialogueAssembly.material(*args, **kwargs) | {'version':PROFILE, 'recent_projection_ids':[]}

    def with_recent(self, material, prior_projected_ids):
        require(material['version'] == PROFILE and isinstance(prior_projected_ids, (list, tuple))
                and len(prior_projected_ids) <= RECENT_TURNS, 'portable_history')
        known = set(self._bundles[material['bundle_id']][0]['candidate_numbers'].values())
        require(all(isinstance(ids, (list, tuple)) and len(ids) <= 5
                    and all(isinstance(key, str) and key in known for key in ids)
                    and len(set(ids)) == len(ids) for ids in prior_projected_ids), 'portable_history_ids')
        return material | {'recent_projection_ids':[list(ids) for ids in prior_projected_ids]}

    def _freeze(self, db, turn, bundle_id, model, messages, phase, details):
        material = super()._freeze(db, turn, bundle_id, model, messages, phase, details)
        if phase != 'main':
            return material
        return self.with_recent(material, self._recent(db, turn, bundle_id))

    def guard(self, db, turn, material):
        super().guard(db, turn, material)
        if material['phase'] == 'main':
            require(material['recent_projection_ids'] == self._recent(db, turn, material['bundle_id']),
                    'portable_history_changed')

    @staticmethod
    def _recent(db, turn, bundle_id):
        # Count the last two confirmed main replies, including replies made without assets.
        rows = db.execute("""
            SELECT t.id, u.material_json, s.artifact_json FROM turns t
            LEFT JOIN expression_speech_units spoken ON spoken.turn_id=t.id
                AND spoken.phase='main' AND spoken.source_kind='dialogue_reply'
            LEFT JOIN expression_speech_publications pub ON pub.utterance_id=spoken.id
                AND pub.canonical_event_id=t.reply_event_id AND pub.state='complete'
            LEFT JOIN dialogue_generation_stages reply ON reply.id=spoken.source_id
                AND reply.stage='reply' AND pub.canonical_event_id IS NOT NULL
            LEFT JOIN dialogue_generation_units u ON u.id=reply.unit_id AND u.turn_id=t.id AND u.phase='main'
            LEFT JOIN dialogue_generation_stages s ON s.unit_id=u.id AND s.stage='selection'
                AND s.state='completed' AND s.validation_error IS NULL
            WHERE t.persona_id=? AND t.session_id=? AND t.status='completed' AND t.reply_event_id IS NOT NULL
              AND t.rowid<(SELECT rowid FROM turns WHERE id=?)
            ORDER BY t.rowid DESC LIMIT ?""", (turn['persona_id'],turn['session_id'],turn['id'],RECENT_TURNS)).fetchall()
        prior = []
        for prior_turn, raw, artifact in reversed(rows):
            previous = json.loads(raw) if raw else {}
            ids = []
            if artifact and previous.get('bundle_id') == bundle_id and previous.get('version') == PROFILE:
                archived = read_exact(db, turn_scope(db, turn), {'kind':'turn', 'id':prior_turn})
                if archived.get('usable'):
                    ids = json.loads(artifact)
            prior.append(ids)
        return prior

    def candidate_pool(self, material):
        require(material['version'] == PROFILE, 'portable_material_version')
        manifest, assets = self._bundles[material['bundle_id']]
        recent = {key for ids in material['recent_projection_ids'] for key in ids}
        eligible, omitted = [], {}
        for key in manifest['candidate_numbers'].values():
            rule = manifest['expression_projection'][key]
            reason = rule if rule != 'portable' else projection_reason(assets[key])
            if not reason and key in recent:
                reason = 'recently_projected'
            if reason:
                omitted[key] = reason
            else:
                eligible.append(key)
        return {'eligible_ids':eligible, 'omitted':omitted}

    def eligible_ids(self, material):
        return self.candidate_pool(material)['eligible_ids']

    def selectable(self, material):
        return material['phase'] == 'main' and bool(self.eligible_ids(material))

    def selector_message(self, material):
        manifest, assets = self._bundles[material['bundle_id']]
        original = manifest['selector_system']
        numbers = manifest['candidate_numbers']
        live = set(self.eligible_ids(material))
        # The reviewed format has one numbered row per candidate with no trailing prose.
        lines = original['content'].splitlines(keepends=True)
        chosen, found = [], []
        for line in lines:
            match = re.match(r'^([1-9][0-9]*):', line)
            if not match:
                require(not found or not line.strip(), 'portable_selector_row_format')
                chosen.append(line)
                continue
            number = match[1]
            require(number in numbers and number not in found, 'portable_selector_row_number')
            key = numbers[number]
            require(assets[key]['situation'] in line and assets[key]['style'] in line, 'portable_selector_row_changed')
            found.append(number)
            if key in live:
                chosen.append(line)
        require(found == [str(i) for i in range(1, len(numbers) + 1)], 'portable_selector_rows_missing')
        return {'role':original['role'], 'content':''.join(chosen)}

    def selection_audit(self, body, material):
        requested = super().parse_selection(body, material)
        pool = self.candidate_pool(material)
        return {'requested_ids':requested, 'applied_ids':[key for key in requested if key in pool['eligible_ids']],
                'omitted':{key:pool['omitted'][key] for key in requested if key in pool['omitted']}}

    def parse_selection(self, body, material):
        return self.selection_audit(body, material)['applied_ids']

    def request(self, stage, turn, material, thought, selected_ids):
        require(material['version'] == PROFILE, 'portable_material_version')
        if stage == 'search_notice':
            return super().request(stage, turn, material, thought, selected_ids)
        require(material['phase'] == 'main', 'wrong_stage')
        manifest, assets = self._bundles[material['bundle_id']]
        variables = manifest['variables']
        time_block = variables['time_block']['prefix'] + moment(material['accepted_at'])
        current = material['current']
        if stage == 'understanding':
            line = variables['dialogue_prompt']['line_format']
            messages = [{'role':'system','content':manifest['flash_system']}]
            for row in material['history']:
                require(len(row) in (3,4), 'history_shape')
                messages.extend([
                    {'role':'user','content':line.format(time=moment(row[2]),speaker=manifest['speaker'],text=row[0])},
                    {'role':'assistant','content':line.format(time=moment(row[-1]),speaker=manifest['persona'],text=row[1])}
                ])
            latest = [line.format(time=moment(at), speaker=manifest['speaker'], text=content)
                      for content, at in material.get('current_lines', [(current, material['accepted_at'])])]
            text = fill(manifest['flash_user'], {'time_block':time_block, 'dialogue_prompt':'\n'.join(latest),
                                                'extra_info_block':''})
            messages.append({'role':'user','content':text})
            return wire(manifest['flash_model'], messages)
        if stage == 'selection':
            require(self.selectable(material), 'portable_pool_empty')
            reference = variables['planner_reasoning']['format'].format(thought=thought)
            return wire(manifest['selector_model'], [self.selector_message(material), {'role':'user','content':time_block},
                        {'role':'user','content':current}, {'role':'user','content':reference}])
        require(stage == 'reply' and len(selected_ids) <= 5 and len(set(selected_ids)) == len(selected_ids)
                and set(selected_ids) <= set(self.eligible_ids(material)), 'selected_scope')
        # Unverified interpretations inform selection. Only the original speaker turns
        # and the allowed expression projection are passed to the character model.
        messages, _, _ = repair_messages(material['messages'], {'variables':variables}, '',
                                         [assets[key] for key in selected_ids], material['messages'][-1]['content'])
        return wire(material['base_model'] | {'name':'deepseek-v4-pro'}, messages)
