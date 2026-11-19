"""Local activation and immutable material for the approved Jev scene route."""
import json
import re
from hashlib import sha256
from pathlib import Path

from .archive import encoded, fingerprint
from .config import ConfigStore, atomic_json
from .private_personas import guard_cloud_dispatch, provider_identity, safe
from .scene_voice import SCENES, VERSION, require

KEY = 'scene_voice'
PREFIX = 'persona.scene_voice.'
IMPORTED_PREFIX = 'persona.import_card.scene.'
BASELINE = 'companion-runtime-v8'
JEV = 'jev-1.14'
PRO = 'deepseek-v4-pro'


def read(db, key):
    entry = db.execute('SELECT value FROM knowledge_control WHERE key=?', (key,)).fetchone()
    return json.loads(entry[0]) if entry else None


def save(db, key, value):
    db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, encoded(value)))


def active(db, persona_id, store=None):
    entry = read(db, PREFIX + persona_id)
    if entry:
        return entry if not entry.get('paused') else None
    if store is not None:
        imported = read(db, IMPORTED_PREFIX + persona_id)
        if imported and not imported.get("paused"):
            from .import_card_publish import imported_scene_available
            if imported_scene_available(store, db, persona_id, imported):
                return imported
    return None


def pause(db, persona_id=None):
    sql = "UPDATE knowledge_control SET value=json_set(value,'$.paused',1) WHERE "
    if persona_id is None:
        return db.execute(sql+"key LIKE ? OR key LIKE ?",(PREFIX+'%',IMPORTED_PREFIX+'%')).rowcount
    return db.execute(sql+"key IN (?,?)",(PREFIX+persona_id,IMPORTED_PREFIX+persona_id)).rowcount


def frozen(turn):
    context = turn.get('context_policy_snapshot') or {}
    if isinstance(context, str):
        context = json.loads(context)
    return context.get(KEY)


def head(db, persona_id):
    from .profile import snapshot
    row = db.execute('SELECT version_id FROM character_heads WHERE persona_id=?',(persona_id,)).fetchone()
    require(row, 'persona_unavailable')
    return row[0], fingerprint(snapshot(db,persona_id))


def digest(value):
    return isinstance(value,str) and bool(re.fullmatch('[0-9a-f]{64}',value))


def material(paths, policy):
    revision = policy['catalog_sha256']
    require(digest(revision),'catalog_identifier')
    path = safe(paths.home/'private'/'scene-voice'/revision/'catalog.json',paths.home)
    try:
        document = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        from .models import RuntimeErrorBase
        raise RuntimeErrorBase('scene_catalog_unavailable') from error
    require(fingerprint(document)==revision and document.get('version')==VERSION
            and document.get('persona')==policy['persona_id']
            and set(document.get('cards',{}))==set(SCENES)
            and set(document.get('criteria',{}))==set(SCENES)
            and all(isinstance(v,str) and v.strip() for v in [document.get('instruction'),*document['criteria'].values()])
            and all(digest(v) for v in document['cards'].values()),'catalog_changed')
    approval = safe(path.parent/'author-approval.private.json',paths.home)
    require(not (path.parent/'approval.withdrawn').exists() and approval.is_file()
            and sha256(approval.read_bytes()).hexdigest()==policy['approval_sha256'],'approval_changed')
    return document, path.parent


def card(paths, policy, scene):
    document, directory = material(paths,policy)
    require(scene in SCENES and not (directory/(scene+'.withdrawn')).exists(),'card_unavailable')
    path = safe(directory/(scene+'.json'),paths.home)
    try:
        body = json.loads(path.read_bytes())['body']
    except (OSError, ValueError, KeyError) as error:
        from .models import RuntimeErrorBase
        raise RuntimeErrorBase('scene_card_unavailable') from error
    require(isinstance(body,str) and body.strip() and fingerprint(body)==document['cards'][scene],'card_changed')
    return body


def configured_jev(paths):
    from .knowledge_routing import configured_model
    model = configured_model(paths)
    require(model['name']==JEV,'jev_model_changed')
    return model


def cloud(store, db, policy, main):
    jev = configured_jev(store.paths)
    require(main['name']==PRO and (provider_identity(main),provider_identity(jev))==(policy['main'],policy['jev']),
            'provider_changed')
    guard_cloud_dispatch(store,policy['persona_id'],'main',main,db=db)
    guard_cloud_dispatch(store,policy['persona_id'],'knowledge_router',jev,db=db)
    return jev


def install(store, persona_id, source, catalog_sha256, approval_sha256):
    """Enable the version explicitly chosen by the local author for this instance."""
    require(digest(catalog_sha256) and digest(approval_sha256),'approval_identifier')
    source = source.resolve()
    catalog = json.loads((source/'catalog.json').read_bytes())
    expected = fingerprint(catalog)
    require(expected==catalog_sha256 and catalog.get('persona')==persona_id,'catalog_changed')
    approval_bytes = (source/'author-approval.private.json').read_bytes()
    approval = json.loads(approval_bytes)
    require(sha256(approval_bytes).hexdigest()==approval_sha256
            and approval.get('decision')=='approved' and approval.get('version')=='en6-scene-approval-v1'
            and approval.get('catalog_sha256')==catalog_sha256 and digest(approval.get('d4_evidence_sha256')),
            'approval_changed')
    copied = {}
    require(set(catalog.get('cards',{}))==set(SCENES),'choice_catalog')
    for key in SCENES:
        require(not (source/(key+'.withdrawn')).exists(),'card_withdrawn')
        value = json.loads((source/(key+'.json')).read_bytes())
        require(isinstance(value.get('body'),str) and value['body'].strip()
                and fingerprint(value['body'])==catalog['cards'][key],'card_changed')
        copied[key] = {'body':value['body']}
    config = ConfigStore(store.paths).load()
    jev = configured_jev(store.paths)
    policy = {'version':VERSION,'instance':config['instance_id'],'persona_id':persona_id,
              'catalog_sha256':catalog_sha256,'approval_sha256':approval_sha256,
              'main':provider_identity(config['model']),'jev':provider_identity(jev)}
    require(policy['main']==approval.get('main') and policy['jev']==approval.get('jev'),'provider_changed')
    with store.connection(write=True) as db:
        from .context_policy import current
        from .style_study import KEY as STUDY_KEY
        from .dialogue_generation import DialogueGeneration
        context = current(db,persona_id,'desktop')
        require(context.get('companion',{}).get('version')==BASELINE and STUDY_KEY not in context,
                'v8_policy_required')
        require(not DialogueGeneration._policy(db,persona_id)['data']['enabled']
                and not db.execute("SELECT 1 FROM expression_runtime_policies WHERE persona_id=? AND json_extract(data_json,'$.enabled')=1",
                                   (persona_id,)).fetchone(),'competing_generation')
        require(head(db,persona_id)==(catalog['character_version'],catalog['profile_sha256']),'persona_changed')
        cloud(store,db,policy,config['model'])
        directory = safe(store.paths.home/'private'/'scene-voice'/expected,store.paths.home)
        directory.mkdir(parents=True,exist_ok=True)
        for key, value in copied.items():
            require(not (directory/(key+'.withdrawn')).exists(),'card_withdrawn')
            atomic_json(directory/(key+'.json'),value)
        atomic_json(directory/'catalog.json',catalog)
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(dir=directory,delete=False) as stream:
            temp = Path(stream.name)
            stream.write(approval_bytes)
        try:
            temp.replace(directory/'author-approval.private.json')
        finally:
            temp.unlink(missing_ok=True)
        material(store.paths,policy)
        old = active(db,persona_id)
        if old and all(old.get(k)==v for k,v in policy.items()):
            return old
        from .models import identifier
        policy['activation_id'] = identifier()
        save(db,PREFIX+persona_id,policy)
    return policy
