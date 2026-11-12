"""Join exact Wiki web revisions to existing maintenance without copying grants."""
import json
from .archive import fingerprint
from .knowledge_service import read
from .models import RuntimeErrorBase

def page_identity(row):
    if row.get('provider')!='wikimedia_api':return None
    return fingerprint(['wikipedia_zh',str(row['upstream']['page_id'])])

def documents(db):
    return [json.loads(row[0])['data'] for row in db.execute("SELECT value FROM knowledge_control WHERE key LIKE 'knowledge.web.document.%'")]

def withdrawn_version(db,persona_id,page_id,revision):
    invalid=read(db,'web.invalidations',{})['data']
    return any(row['owner_persona']==persona_id and page_identity(row)==page_id
        and row['upstream']['revision_id']==revision and invalid.get(row['id'],{}).get('state')=='withdrawn' for row in documents(db))

def check_page(db,row):
    identity=page_identity(row)
    if identity is None:return
    page=db.execute('SELECT state,latest_revision FROM knowledge_pages WHERE id=?',(identity,)).fetchone()
    target=db.execute('SELECT state FROM knowledge_targets WHERE source_id=? AND public_id=?',('wikipedia_zh',row['title'])).fetchone()
    if (page and (page['state']=='withdrawn' or row['upstream']['revision_id']<page['latest_revision'])
            or target and target['state']=='withdrawn'
            or withdrawn_version(db,row['owner_persona'],identity,row['upstream']['revision_id'])):
        raise RuntimeErrorBase('web_document_withdrawn')

def invalidate_versions(db,page_id,revision=None,reason='upstream_withdrawn',title=None):
    invalid=read(db,'web.invalidations',{});changed=False
    for row in documents(db):
        if row.get('provider')!='wikimedia_api':continue
        same=page_identity(row)==page_id if page_id else row['title']==title
        if not same or revision is not None and row['upstream']['revision_id']>=revision:continue
        if invalid['data'].get(row['id'],{}).get('state')=='withdrawn':continue
        value={'state':'corrected' if revision is not None else 'withdrawn','reason':reason}
        if invalid['data'].get(row['id'])!=value:invalid['data'][row['id']]=value;changed=True
    if changed:
        invalid['revision']+=1
        db.execute('INSERT INTO knowledge_control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('knowledge.web.invalidations',json.dumps(invalid)))

def observe(db,row):
    identity=page_identity(row)
    if identity is None:return
    check_page(db,row);revision=row['upstream']['revision_id']
    page=db.execute('SELECT latest_revision FROM knowledge_pages WHERE id=?',(identity,)).fetchone()
    if page and revision>page['latest_revision']:
        # Old claims cease to be current immediately. The background fetch then
        # obtains wikitext for extraction; HTML and wikitext hashes are not mixed.
        db.execute('UPDATE knowledge_pages SET latest_revision=?,head_document_id=NULL WHERE id=?',(revision,identity))
    invalidate_versions(db,identity,revision,'new_wiki_revision_observed')

def track(maintenance,db,persona_id,channel,row):
    if page_identity(row) is None:return None
    from .knowledge_store import enabled
    if not enabled(db,persona_id,channel,'wikipedia_zh'):return None
    try:maintenance.grant(db,persona_id,'wikipedia_zh')
    except RuntimeErrorBase:return None  # Reading and learning remain separate grants.
    return maintenance._enqueue(db,persona_id,{'source_id':'wikipedia_zh','public_id':row['title']},True,'on_demand')

def discover_existing(maintenance,db,persona_id):
    rows=sorted((r for r in documents(db) if r['owner_persona']==persona_id and page_identity(r)),key=lambda r:r['fetched_epoch'],reverse=True)[:100]
    for row in rows:
        try:
            check_page(db,row)
            maintenance._enqueue(db,persona_id,{'source_id':'wikipedia_zh','public_id':row['title']},True,'daily')
        except RuntimeErrorBase:continue
