"""Local semantic recall over authorized, versioned source passages."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from threading import Lock

from . import local_knowledge as library
from .archive import encoded, fingerprint
from .character_embedding import SemanticFailure, private_directory, provider_for, safe_path
from .knowledge_store import SOURCE_POLICY
from .models import RuntimeErrorBase, timestamp

_encoder_lock=Lock()
PASSAGE_LIMIT = 6


def active(db, tid):
    return bool(db.execute('SELECT 1 FROM knowledge_control WHERE key=?', ('private.semantic.turn.'+tid,)).fetchone())


def organizer_labels(raw):
    """An organizer can describe a source for the index. The actual passage remains the citation."""
    try:
        data=json.loads(raw)
    except (ValueError,TypeError,RecursionError):
        return {}
    if not isinstance(data,dict) or not isinstance(data.get('facts'),list):
        return {}
    labels={}
    for row in data['facts'][:8]:
        if not isinstance(row,dict) or not isinstance(row.get('source_id'),str):
            continue
        result=labels.setdefault(row['source_id'],[])
        for key in ('subject','predicate'):
            value=row.get(key)
            if isinstance(value,dict):
                value=value.get('text')
            if isinstance(value,str) and value.strip() and len(value)<=128 and value not in result:
                result.append(value)
    return labels


def store_sources(db, turn, base, data, task, labels=None):
    from .private_lookup import intent
    pid,sid,tid=turn['persona_id'],turn['session_id'],turn['id']
    acquisition=intent(db,tid)
    ids=[]
    for source in (data or {}).get('sources',[]):
        kind=source.get('kind')
        if kind not in ('page_content','search_content','native_search_summary','search_summary','user_correction'):
            continue
        if kind=='user_correction' and (not base['consent']['enabled'] or not acquisition or acquisition['origin']!='session'):
            continue
        if task.get('url') and (source['url']!=task['url'] or not source.get('content')):
            continue
        body=source.get('content') or source.get('summary') or ''
        if not body.strip():
            continue
        doc,original,at,_=library.turn_document(db,pid,sid,tid,source['id'],instance=base['instance'],
            consent=base['consent'],active_turn=tid,versioned=True)
        if db.execute('SELECT 1 FROM knowledge_control WHERE key=?',('private.study.withdrawn_document.'+doc,)).fetchone():
            continue
        # Keep scoped claims authoritative where their permissions or effective period are narrower
        # than the document. Else the licensed original is available including material not extracted.
        prior=db.execute("""SELECT id,spec_json,withdrawn FROM local_knowledge_facts WHERE document_id=?
            AND json_extract(spec_json,'$.state')!='source_excerpt'""",(doc,)).fetchall()
        if prior:
            ids.extend(row[0] for row in prior)
            if any(row[2] or _restricted_claim(json.loads(row[1])) or library.revisions.superseded(db,row[0],at,set(),None)
                   for row in prior):
                continue
        if db.execute('SELECT 1 FROM local_knowledge_facts WHERE document_id=? AND withdrawn=1',(doc,)).fetchone():
            continue
        expires=(datetime.fromisoformat(at)+timedelta(seconds=SOURCE_POLICY['freshness_seconds'])).isoformat()
        binding=[tid,source['id'],fingerprint(original)]
        for start in range(0,len(body),432):
            piece=body[start:start+480]
            if piece.strip():
                ident=library.put_fact(db,doc,subject=source['title'][:128],predicate=kind,value=piece,
                    quote=piece,start=start,end=start+len(piece),attributes=(labels or {}).get(source['id'],[]),
                    evidence_state='source_excerpt',turn_source=binding,expires_at=expires,versioned=True,
                    verification_receipt=tid)
                ids.append(ident)
            if start+480>=len(body):
                break
    return list(dict.fromkeys(ids))


def _restricted_claim(spec):
    return bool(spec['legacy_claim'] or spec['valid_from'] or spec['valid_until'] or spec['conditions'])


def import_prior_sources(db, turn, base):
    """Index surviving sources from earlier sessions of this protocol within the current grant and conversation."""
    from .private_lookup import evidence
    pid,sid=turn['persona_id'],turn['session_id']
    rows=db.execute("""SELECT r.turn_id,i.query_json,i.origin,i.consent_revision FROM private_lookup_records r
        JOIN private_lookup_intents i ON i.turn_id=r.turn_id JOIN turns t ON t.id=r.turn_id
        WHERE r.persona_id=? AND r.session_id=? AND r.outcome='acquired' AND r.turn_id!=?
        AND (?='' OR t.rowid<(SELECT rowid FROM turns WHERE id=?))
        AND NOT EXISTS(SELECT 1 FROM knowledge_control WHERE key='private.semantic.sources.'||r.turn_id)
        ORDER BY r.rowid""",(pid,sid,turn['id'],turn['id'],turn['id'])).fetchall()
    for origin,task,scope,revision in rows:
        if not task or scope not in ('session','confirmed','explicit','derived'):
            continue
        if scope=='session' and (not base['consent']['enabled'] or base['consent']['revision']!=revision):
            continue
        data=evidence(db,pid,sid,origin)
        if not data:
            continue
        saved=db.execute("""SELECT result_json FROM private_lookup_calls WHERE turn_id=? AND step IN ('organize','organize_retry')
            AND state='received' ORDER BY rowid DESC LIMIT 1""",(origin,)).fetchone()
        labels=organizer_labels(json.loads(saved[0]).get('raw')) if saved else {}
        store_sources(db,{'id':origin,'persona_id':pid,'session_id':sid},base,data,json.loads(task),labels)
        db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',('private.semantic.sources.'+origin,'true'))


def materials(db, turn, base, question, *, ids=None, now=None):
    pid,sid=turn['persona_id'],turn['session_id']
    if not library.enabled(db,pid) or ids==[]:
        return []
    consent=base['consent']
    revision=consent['revision'] if consent['enabled'] else 0
    grant=library._grant(db,base['instance'],pid,sid,revision) if revision else None
    sql="""SELECT f.id,f.document_id,f.spec_json,d.scope,d.title,d.url,d.source_kind,d.published_at,d.acquired_at
        FROM local_knowledge_facts f JOIN local_knowledge_documents d ON d.id=f.document_id
        WHERE f.withdrawn=0 AND (d.scope='instance' OR (d.persona_id=? AND
        (d.scope='persona' OR (d.session_id=? AND (d.consent_revision=0 OR (d.consent_revision=? AND d.grant_hash=?))))))
        AND NOT EXISTS(SELECT 1 FROM knowledge_control k WHERE k.key='private.study.withdrawn_document.'||d.id)"""
    args=[pid,sid,revision,grant]
    if ids is not None:
        sql+=' AND f.id IN (SELECT value FROM json_each(?))'
        args.append(encoded(ids))
    today=library._utc(now or timestamp())
    as_of=library._AS_OF.search(question)
    try:
        when=datetime(*(int(x) for x in as_of.groups()),tzinfo=timezone.utc).isoformat() if as_of else today
    except ValueError:
        return []
    links={library.revisions.canonical_url(library.public_url(link))
           for link in re.findall(r'https?://[^\s，。；！？）)]+',question)}
    versions,_=library.revisions.query_parts(re.sub(r'https?://\S+',' ',question))
    state=library.revisions.requested_state(question)
    current_information=bool(library._REFRESH.search(question) or re.search('现在|当前|目前|最新|如今',question))
    sources={}
    document_guards={}
    result=[]
    for row in db.execute(sql,args):
        spec=json.loads(row['spec_json'])
        checked=spec.get('verification') or row
        since=checked['published_at'] or row['acquired_at']
        verified=spec.get('verified_at') or row['acquired_at']
        if verified>today or row['acquired_at']>today or since>when or (spec['valid_from'] and spec['valid_from']>when):
            continue
        enforce_freshness=spec['state']!='source_excerpt' or current_information
        if ((spec['valid_until'] and spec['valid_until']<=when) or
                (enforce_freshness and not as_of and spec['expires_at'] and spec['expires_at']<=today)):
            continue
        if links and library.revisions.canonical_url(row['url']) not in links:
            continue
        existing_versions,_=library.revisions.split_conditions(spec['conditions'])
        if versions and existing_versions and not versions.intersection(existing_versions):
            continue
        if state and spec['state'] not in (state,'source_excerpt'):
            continue
        if library.revisions.superseded(db,row['id'],when,versions,state):
            continue
        if spec['state']=='source_excerpt':
            doc=row['document_id']
            if doc not in document_guards:
                claims=db.execute("""SELECT id,spec_json,withdrawn FROM local_knowledge_facts WHERE document_id=?
                    AND json_extract(spec_json,'$.state')!='source_excerpt'""",(doc,))
                def restrict(claim):
                    s=json.loads(claim[1])
                    return (claim[2] or _restricted_claim(s) or (s['expires_at'] and not as_of and s['expires_at']<=today)
                            or library.revisions.superseded(db,claim[0],when,versions,state))
                document_guards[doc]=any(restrict(claim) for claim in claims)
            if document_guards[doc]:continue
            if not versions and checked['published_at'] and db.execute("""SELECT 1 FROM local_knowledge_documents newer
                JOIN local_knowledge_documents old ON old.id=? WHERE newer.url=old.url AND newer.scope=old.scope
                AND newer.persona_id IS old.persona_id AND newer.session_id IS old.session_id
                AND newer.consent_revision=old.consent_revision AND newer.grant_hash IS old.grant_hash
                AND newer.sha256!=old.sha256 AND newer.published_at>? AND newer.published_at<=?
                AND newer.acquired_at<=? LIMIT 1""",(doc,checked['published_at'],when,today)).fetchone():
                continue
        if spec['legacy_claim'] and not library._legacy(db,pid,spec['legacy_claim']):
            continue
        if spec['turn_source']:
            origin,key,digest=spec['turn_source']
            if (origin,key) not in sources:
                source=library._turn_source(db,pid,sid,origin,key,active_turn=turn['id'])
                sources[origin,key]=fingerprint(source) if source else None
            if sources[origin,key]!=digest:
                continue
        result.append({'id':row['id'],'subject':spec['subject'],'predicate':spec['predicate'],'text':spec['quote'],
            'value':spec['value'],'qualifications':spec['conditions'],'kind':spec['state'],'title':checked['title'],
            'date':checked['published_at'],'source_kind':checked['source_kind'],'url':row['url'],'source_ids':[row['document_id']],
            'verified_at':verified,'valid_from':spec['valid_from'],'valid_until':spec['valid_until'],
            'span':[spec['start'],spec['end']],'index_labels':spec['attributes'] if spec['state']=='source_excerpt' else []})
    return result


def public_material(row):
    return {k:v for k,v in row.items() if k!='index_labels'}


def reset_index(paths):
    path=safe_path(paths.home,paths.home/'private/local-knowledge-index/vectors.sqlite3')
    if path.is_file():
        with _encoder_lock,closing(sqlite3.connect(path)) as cache,cache:
            cache.execute('DELETE FROM vectors')


def require_encoder(paths):
    with _encoder_lock:
        if provider_for(paths).diagnose()['status']!='ready':
            raise RuntimeErrorBase('local_knowledge_embedding_unavailable')


def related_questions(paths, current, preceding):
    """Use the local encoder to decide whether the preceding topic helps interpret this question."""
    from .character_retrieval import load_config
    if current==preceding or not current.strip() or not preceding.strip():
        return False
    with _encoder_lock:
        try:
            encoder=provider_for(paths)
            _,left=encoder.embed_query([current[:24000]])
            _,right=encoder.embed_query([preceding[:24000]])
            return float((left@right.T).max())>=load_config()['thresholds']['lore']
        except SemanticFailure as exc:
            raise RuntimeErrorBase('local_knowledge_embedding_unavailable') from exc


def pinned(db, turn, base, question, ids):
    found={r['id']:public_material(r) for r in materials(db,turn,base,question,ids=ids)}
    if not ids or any(i not in found for i in ids):
        raise RuntimeErrorBase('private_reply_source_changed')
    return [found[i] for i in ids]


def rank(paths, question, rows, *, encoder=None, limit=PASSAGE_LIMIT):
    """Rank in one local embedding space; cache derived vectors, never an authority or a verdict."""
    if not rows or not question.strip():
        return []
    with _encoder_lock:
        try:
            return _rank(paths,question,rows,encoder,limit)
        except SemanticFailure as exc:
            raise RuntimeErrorBase('local_knowledge_embedding_unavailable') from exc
        except sqlite3.DatabaseError as exc:
            raise library.storage_error(exc) from exc


def _rank(paths, question, rows, encoder, limit):
    from .character_embedding import DIMENSION,numpy_module
    from .character_retrieval import load_config
    np=numpy_module()
    encoder=encoder or provider_for(paths)
    model=encoder.identity()['fingerprint']
    query=np.concatenate([encoder.embed_query([part])[1] for start in range(0,len(question),24000)
                          if (part:=question[start:start+24000]).strip()])
    cutoff=load_config()['thresholds']['lore']
    docs={}
    for r in rows:
        content='\n'.join([r['title'],*r['index_labels'],r['text']])
        key=fingerprint([model,content])
        docs.setdefault(key,(content,[]))[1].append(r)
    root=safe_path(paths.home,paths.home/'private/local-knowledge-index')
    if not root.exists():private_directory(paths.home,root)
    path=safe_path(paths.home,root/'vectors.sqlite3')
    scores={}
    with closing(sqlite3.connect(path,timeout=10)) as cache:
        cache.execute('CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY, data BLOB NOT NULL)')
        pending=[]
        def embed_pending():
            if not pending:return
            vectors=encoder.embed_documents([piece for _,piece in pending])
            for (key,_),vec in zip(pending,vectors):
                scores[key].append(vec)
            pending.clear()
        for key,(content,_) in docs.items():
            row=cache.execute('SELECT data FROM vectors WHERE id=?',(key,)).fetchone()
            if row and len(row[0]) and len(row[0])%(4*DIMENSION)==0:
                vec=np.frombuffer(row[0],dtype='<f4').reshape(-1,DIMENSION)
                if np.isfinite(vec).all() and np.allclose(np.linalg.norm(vec,axis=1),1,atol=1e-3):
                    scores[key]=vec
                    continue
            scores[key]=[]
            for chunk in encoder.chunks(content):
                pending.append((key,chunk['text']))
                if len(pending)>=32:embed_pending()
        embed_pending()
        with cache:
            for key,vec in scores.items():
                if isinstance(vec,list):
                    vec=np.asarray(vec,dtype='<f4').reshape(-1,DIMENSION)
                    cache.execute('INSERT OR REPLACE INTO vectors VALUES(?,?)',(key,vec.tobytes()))
                scores[key]=float(np.max(vec@query.T))
    ranked=sorted(((scores[key],r) for key,(_,items) in docs.items() for r in items if scores[key]>=cutoff),
                  key=lambda pair:(-pair[0],pair[1]['id']))
    seen=set()
    selected=[]
    for _,r in ranked:
        key=(r['url'],r['text'])
        if key not in seen:
            selected.append(public_material(r))
            seen.add(key)
        if len(selected)==limit:break
    return selected
