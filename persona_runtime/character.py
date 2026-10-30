"""Creator-owned character canon, immutable versions and native interchange. No model calls."""
import calendar
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math

from .models import LOCAL_USER_ID, PersonaSpec, RuntimeErrorBase, identifier, timestamp
from .profile import encode, snapshot as profile_snapshot

FIELDS = {
    'identity': {'aliases': [], 'species': None, 'gender': None, 'pronouns': None},
    'age': {'policy': 'unspecified', 'fixed_age': None, 'birth_date': None, 'birthday': None},
    'appearance': {'height_cm': None, 'weight_kg': None, 'blood_type': None,
                   'hair_color': None, 'eye_color': None, 'description': None},
    'biography': {'hometown': None, 'occupation': None, 'education': None,
                  'family': None, 'relationships': None},
    'interests': {'likes': [], 'dislikes': [], 'hobbies': [], 'notes': None},
    'social': {'public_image': None, 'humor': None, 'context_rules': None},
    'world': {'name': None, 'description': None},
}
PATHS = {f'{group}.{field}' for group, fields in FIELDS.items() for field in fields} | {'age.years'}
MAX_BYTES = 128 * 1024


def empty_character():
    return {'schema_version': 1, **deepcopy(FIELDS), 'lorebook': [], 'examples': []}


def today_shanghai():
    return datetime.now(timezone(timedelta(hours=8))).date()


def _text(value, field, maximum=2000, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise RuntimeErrorBase('invalid_character_text:' + field)


def _strings(value, field, maximum=50, item_limit=200):
    if not isinstance(value, list) or len(value) > maximum:
        raise RuntimeErrorBase('invalid_character_list:' + field)
    for item in value:
        _text(item, field, item_limit)
    if len(set(value)) != len(value):
        raise RuntimeErrorBase('duplicate_character_value:' + field)


def validate_character(value):
    if not isinstance(value, dict) or set(value) != set(empty_character()):
        raise RuntimeErrorBase('invalid_character_fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise RuntimeErrorBase('unsupported_character_schema')
    data = value
    for group, fields in FIELDS.items():
        row = data[group]
        if not isinstance(row, dict) or set(row) != set(fields):
            raise RuntimeErrorBase('invalid_character_fields:' + group)
        for field, default in fields.items():
            item = row[field]
            if group == 'age' or field in ('height_cm', 'weight_kg'):
                continue
            if isinstance(default, list):
                _strings(item, group+'.'+field)
            else:
                _text(item, group+'.'+field, nullable=True)
    for field in ('height_cm', 'weight_kg'):
        number = data['appearance'][field]
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or number <= 0):
            raise RuntimeErrorBase('invalid_character_measurement:' + field)
    blood = data['appearance']['blood_type']
    if blood not in (None, 'A', 'B', 'AB', 'O', 'other'):
        raise RuntimeErrorBase('invalid_blood_type')
    age = data['age']
    if age['policy'] not in ('unspecified', 'fixed', 'calendar'):
        raise RuntimeErrorBase('invalid_age_policy')
    if age['fixed_age'] is not None and (type(age['fixed_age']) is not int or age['fixed_age'] < 0):
        raise RuntimeErrorBase('invalid_fixed_age')
    birth = None
    if age['birth_date'] is not None:
        try:
            birth = date.fromisoformat(age['birth_date'])
            if birth.isoformat() != age['birth_date'] or birth > today_shanghai():
                raise ValueError()
        except (TypeError, ValueError):
            raise RuntimeErrorBase('invalid_birth_date') from None
    if age['birthday'] is not None:
        try:
            _text(age['birthday'], 'birthday', 5)
            if len(age['birthday']) != 5:
                raise ValueError()
            date.fromisoformat('2000-'+age['birthday'])
        except (TypeError, ValueError):
            raise RuntimeErrorBase('invalid_birthday') from None
    if birth and age['birthday'] and birth.strftime('%m-%d') != age['birthday']:
        raise RuntimeErrorBase('birthday_conflict')
    if age['policy'] == 'calendar' and (birth is None or age['fixed_age'] is not None):
        raise RuntimeErrorBase('calendar_age_requires_birth_date')
    if age['policy'] == 'fixed' and (age['fixed_age'] is None or birth is not None):
        raise RuntimeErrorBase('fixed_age_requires_number_without_birth_date')
    if age['policy'] == 'unspecified' and age['fixed_age'] is not None:
        raise RuntimeErrorBase('unspecified_age_requires_null_number')
    if not isinstance(data['lorebook'], list) or len(data['lorebook']) > 100:
        raise RuntimeErrorBase('too_many_lore_entries')
    lore = {}
    for row in data['lorebook']:
        if not isinstance(row, dict) or set(row) != {'id','title','category','keywords','text','priority','enabled','visibility','dependencies'}:
            raise RuntimeErrorBase('invalid_lore_fields')
        for field, maximum in (('id',100),('title',200),('category',100),('text',2000)):
            _text(row[field], field, maximum)
        _strings(row['keywords'], 'keywords', 30)
        _strings(row['dependencies'], 'dependencies', 10, 100)
        if type(row['priority']) is not int or not -1000 <= row['priority'] <= 1000 or type(row['enabled']) is not bool:
            raise RuntimeErrorBase('invalid_lore_priority_or_enabled')
        if row['visibility'] not in ('conversation', 'creator_only') or row['id'] in lore:
            raise RuntimeErrorBase('invalid_lore_visibility_or_duplicate_id')
        lore[row['id']] = row
    visited = set()
    def visit(entry, trail):
        if entry in trail:
            raise RuntimeErrorBase('lore_dependency_cycle')
        if entry in visited:
            return
        for dep in lore[entry]['dependencies']:
            if dep not in lore:
                raise RuntimeErrorBase('lore_dependency_missing')
            if lore[entry]['visibility'] == 'conversation' and lore[dep]['visibility'] == 'creator_only':
                raise RuntimeErrorBase('lore_dependency_private')
            visit(dep, trail | {entry})
        visited.add(entry)
    for entry in lore:
        visit(entry, set())
    if not isinstance(data['examples'], list) or len(data['examples']) > 50:
        raise RuntimeErrorBase('too_many_expression_examples')
    ids = set()
    for row in data['examples']:
        if not isinstance(row, dict) or set(row) != {'id','scenario','keywords','text','enabled'}:
            raise RuntimeErrorBase('invalid_example_fields')
        for field, maximum in (('id',100),('scenario',200),('text',500)):
            _text(row[field], field, maximum)
        _strings(row['keywords'], 'example_keywords', 30)
        if type(row['enabled']) is not bool or row['id'] in ids:
            raise RuntimeErrorBase('invalid_example_enabled_or_duplicate_id')
        ids.add(row['id'])
    try:
        size = len(encode(data).encode('utf-8'))
    except (TypeError, UnicodeError, ValueError):
        raise RuntimeErrorBase('invalid_character_encoding') from None
    if size > MAX_BYTES:
        raise RuntimeErrorBase('character_too_large')
    return deepcopy(data)


def head(conn, persona_id):
    row = conn.execute('SELECT v.* FROM character_heads h JOIN character_versions v ON v.id=h.version_id '
                       'WHERE h.persona_id=? AND h.user_id=?', (persona_id, LOCAL_USER_ID)).fetchone()
    if row is None:
        raise RuntimeErrorBase('character_not_initialized')
    return decode_version(row)


def decode_version(row):
    value = dict(row)
    value['data'] = json.loads(value.pop('data_json'))
    value['origin_type'] = 'creator_setting'
    return value


def append(conn, persona_id, data, actor='user', restored_from=None):
    old = conn.execute('SELECT version_id FROM character_heads WHERE persona_id=?', (persona_id,)).fetchone()
    revision = conn.execute('SELECT coalesce(max(revision),0)+1 FROM character_versions WHERE persona_id=?', (persona_id,)).fetchone()[0]
    version = identifier()
    conn.execute('INSERT INTO character_versions VALUES (?,?,?,?,?,?,?,?,?)',
                 (version, persona_id, LOCAL_USER_ID, revision, encode(data), actor, old[0] if old else None, restored_from, timestamp()))
    conn.execute('INSERT INTO character_heads VALUES (?,?,?) ON CONFLICT(persona_id) DO UPDATE SET version_id=excluded.version_id',
                 (persona_id, LOCAL_USER_ID, version))
    return head(conn, persona_id)


def initialize_characters(conn, persona_id=None, actor='migration'):
    for row in conn.execute('SELECT id FROM personas WHERE (? IS NULL OR id=?)', (persona_id,persona_id)).fetchall():
        if not conn.execute('SELECT 1 FROM character_heads WHERE persona_id=?',(row[0],)).fetchone():
            append(conn, row[0], empty_character(), actor)


def read_fact(version, path, on_date=None):
    if path not in PATHS:
        raise RuntimeErrorBase('unknown_character_path')
    data = version['data']
    if path == 'age.years':
        age = data['age']; value = None
        if age['policy'] == 'fixed':
            value = age['fixed_age']
        elif age['policy'] == 'calendar':
            birth = date.fromisoformat(age['birth_date']); now = on_date or today_shanghai()
            day = min(birth.day, calendar.monthrange(now.year,birth.month)[1])
            value = now.year-birth.year-((now.month,now.day)<(birth.month,day))
    else:
        group, field = path.split('.')
        value = data[group][field]
    return {'path':path,'value':value,'status':'unset' if value is None or value == [] else 'value',
            'unit':{'appearance.height_cm':'cm','appearance.weight_kg':'kg','age.years':'years'}.get(path),
            'version_id':version['id'],'origin_type':'creator_setting'}


class CharacterService:
    def __init__(self, store):
        self.store = store

    def _check(self, conn, persona_id, write=False):
        if write:
            from .private_personas import guard_acceptance_write
            guard_acceptance_write(self.store,persona_id)
        if not conn.execute('SELECT 1 FROM personas WHERE id=?',(persona_id,)).fetchone():
            raise RuntimeErrorBase('Persona not found')
        if write and self.store.current(conn) != persona_id:
            raise RuntimeErrorBase('character_selection_conflict')

    def _version(self, conn, persona_id, version_id=None):
        from .persona_management import active
        active(conn,persona_id)
        self._check(conn,persona_id)
        if version_id is None:
            return head(conn,persona_id)
        row = conn.execute('SELECT * FROM character_versions WHERE id=? AND persona_id=? AND user_id=?',
                           (version_id,persona_id,LOCAL_USER_ID)).fetchone()
        if row is None:
            raise RuntimeErrorBase('record_not_in_selected_persona')
        return decode_version(row)

    def current(self, persona_id):
        with self.store.connection() as conn:
            result = self._version(conn,persona_id)
            result['profile'] = profile_snapshot(conn,persona_id)
            return result

    def show(self, persona_id, version_id):
        with self.store.connection() as conn:
            return self._version(conn,persona_id,version_id)

    def history(self, persona_id):
        with self.store.connection() as conn:
            self._check(conn,persona_id)
            current = head(conn,persona_id)['id']
            return [decode_version(r) | {'current':r['id']==current} for r in conn.execute(
                'SELECT * FROM character_versions WHERE persona_id=? AND user_id=? ORDER BY revision DESC',(persona_id,LOCAL_USER_ID))]

    def compare(self, persona_id, left_id, right_id):
        with self.store.connection() as conn:
            left = self._version(conn,persona_id,left_id)['data']; right = self._version(conn,persona_id,right_id)['data']
            changes = {}
            for group in FIELDS:
                for field in FIELDS[group]:
                    if left[group][field] != right[group][field]:
                        changes[group+'.'+field] = {'before':left[group][field],'after':right[group][field]}
            for field in ('lorebook','examples'):
                if left[field] != right[field]:
                    changes[field] = {'before':left[field],'after':right[field]}
            return {'left_id':left_id,'right_id':right_id,'changes':changes}

    def _write(self, conn, persona_id, data, expected_version, operation_id, restored_from=None):
        self._check(conn,persona_id,True)
        _text(operation_id,'operation_id',200)
        data = validate_character(data)
        fingerprint = hashlib.sha256(encode([persona_id,data,expected_version,restored_from]).encode()).hexdigest()
        old = conn.execute('SELECT * FROM character_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if old:
            if old['persona_id'] != persona_id or old['fingerprint'] != fingerprint:
                raise RuntimeErrorBase('submission_conflict')
            return self._version(conn,persona_id,old['version_id']) | {'already_applied':True}
        current = head(conn,persona_id)
        if expected_version != current['id']:
            raise RuntimeErrorBase('character_version_conflict')
        result = current if data == current['data'] and restored_from is None else append(
            conn,persona_id,data,'rollback' if restored_from else 'user',restored_from)
        conn.execute('INSERT INTO character_operations VALUES (?,?,?,?,?)',
                     (operation_id,persona_id,LOCAL_USER_ID,fingerprint,result['id']))
        return result

    def update(self, persona_id, data, expected_version, operation_id):
        with self.store.connection(write=True) as conn:
            result=self._write(conn,persona_id,data,expected_version,operation_id)
        callback=getattr(self.store,'character_committed',None)
        if callback:callback(persona_id)
        return result

    def restore(self, persona_id, version_id, expected_version, operation_id):
        with self.store.connection(write=True) as conn:
            data = self._version(conn,persona_id,version_id)['data']
            result=self._write(conn,persona_id,data,expected_version,operation_id,version_id)
        callback=getattr(self.store,'character_committed',None)
        if callback:callback(persona_id)
        return result

    def fact(self, persona_id, path, version_id=None):
        with self.store.connection() as conn:
            return read_fact(self._version(conn,persona_id,version_id),path)

    def preview(self, persona_id, text, version_id=None):
        from .character_context import select_context
        if not isinstance(text,str) or len(text)>24000:
            raise RuntimeErrorBase('invalid_preview_text')
        with self.store.connection() as conn:
            return select_context(self._version(conn,persona_id,version_id),text)

    def export(self, persona_id, name=None):
        from .service import RuntimeService
        service = RuntimeService(self.store)
        with self.store.connection() as conn:
            self._check(conn,persona_id)
            package = {'format':'pcar_character_v1','persona_spec':service._persona(conn,persona_id)['spec'],
                       'character':head(conn,persona_id)['data']}
        path = service._output_path('exports',name,'.json')
        created = False
        try:
            with path.open('x',encoding='utf-8') as output:
                created = True
                json.dump(package,output,ensure_ascii=False,indent=2)
                output.write('\n')
        except BaseException:
            if created:path.unlink(missing_ok=True)
            raise
        return {'path':str(path),'persona_id':persona_id}

    def import_package(self, persona_id, package, operation_id):
        from .service import RuntimeService
        if not isinstance(package,dict) or set(package) != {'format','persona_spec','character'} or package['format']!='pcar_character_v1':
            raise RuntimeErrorBase('invalid_character_package')
        spec = PersonaSpec.from_dict(package['persona_spec']); data = validate_character(package['character'])
        _text(operation_id,'operation_id',200)
        fingerprint = hashlib.sha256(encode(['import',persona_id,package]).encode()).hexdigest()
        with self.store.connection(write=True) as conn:
            self._check(conn,persona_id,True)
            old=conn.execute('SELECT * FROM character_operations WHERE operation_id=?',(operation_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise RuntimeErrorBase('submission_conflict')
                return RuntimeService._persona(conn,old['persona_id']) | {'already_applied':True}
            new_id=identifier()
            conn.execute('INSERT INTO personas VALUES (?,?,?,?,?)',(new_id,spec.name,1,encode(spec.to_dict()),timestamp()))
            from .profile import initialize_profiles
            initialize_profiles(conn,new_id,actor='create')
            version=append(conn,new_id,data,'import')
            from .context_policy import initialize as initialize_context_policies
            initialize_context_policies(conn,new_id,new_persona=True)
            conn.execute('INSERT INTO character_operations VALUES (?,?,?,?,?)',(operation_id,new_id,LOCAL_USER_ID,fingerprint,version['id']))
            result=RuntimeService._persona(conn,new_id)
        callback=getattr(self.store,'character_committed',None)
        if callback:callback(new_id)
        return result
