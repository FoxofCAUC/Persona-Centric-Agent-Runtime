"""Time-aware projection of immutable local knowledge evidence."""
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .archive import fingerprint
from . import knowledge_catalog


VERSION = 'local-first-v3'
_VERSION = re.compile(r'(?:(?:版本|\bversion)\s*[:：]?\s*v?(\d+(?:\.\d+)*)|(?<![a-z0-9])v(\d+(?:\.\d+)*)|(\d+(?:\.\d+)*)\s*版(?:本)?)', re.I)
_STATE = re.compile(r'爆料|传闻|公告|官宣|实装|实测')
_EXTRA = re.compile(r'原先|原来|之前|此前|当时|以前|怎么说|是什么|已更正|如今|更新后|上线后')


def version_number(label):
    m = _VERSION.fullmatch(label.strip())
    if m:
        parts = [int(n) for n in next(g for g in m.groups() if g).split('.')]
        while len(parts)>1 and parts[-1]==0:
            parts.pop()
        return tuple(parts)
    return None


def split_conditions(conditions):
    versions = {v for c in conditions if (v := version_number(c)) is not None}
    rest = tuple(c for c in conditions if version_number(c) is None)
    return versions, rest


def requested_state(text):
    matches = _STATE.findall(text)
    if not matches:
        return None
    return {'爆料':'rumor','传闻':'rumor','公告':'announced','官宣':'announced',
            '实装':'observed','实测':'observed'}[matches[-1]]


def query_parts(text):
    versions = {version_number(m[0]) for m in _VERSION.finditer(text)}
    return versions, _EXTRA.sub(' ', _STATE.sub(' ', _VERSION.sub(' ', text)))


def quoted_versions(quote):
    return {m[0] for m in _VERSION.finditer(quote)}


def match_condition(text,condition):
    from .local_knowledge import _matches,_norm
    rest=_matches(text,[condition])
    if rest is not None:return rest
    label=re.fullmatch(r'(平台|渠道|地区|区服|服务器)\s*[:是为]\s*(.+)',_norm(condition))
    if not label:return None
    key,value=label.groups()
    return _matches(text,[value+key,value+'端' if key=='平台' else value,value])


def canonical_url(url):
    parts = urlsplit(url)
    host = parts.hostname.lower()
    if ':' in host:
        host = '['+host+']'
    if parts.port and parts.port != (443 if parts.scheme=='https' else 80):
        host += ':'+str(parts.port)
    return urlunsplit((parts.scheme.lower(),host,parts.path,parts.query,''))


def model_material(value):
    """Keep multi-source audit history local while forwarding the chosen literal citation."""
    if isinstance(value,list):
        return [model_material(item) for item in value]
    if isinstance(value,dict):
        if {'claim_id','revision_id','evidence','source_ids'}<=value.keys():
            return {k:v for k,v in value.items() if k not in ('claim_id','revision_id','evidence','witnesses')}|{'source_ids':value['source_ids'][:1]}
        return {k:model_material(v) for k,v in value.items()}
    return value


def subject_key(spec):
    from .local_knowledge import _norm
    _,conditions = split_conditions(spec['conditions'])
    slot = knowledge_catalog.entry(spec)
    return (_norm(spec['subject']),_norm(spec['predicate']),spec['unit'],
            tuple(sorted(_norm(c) for c in conditions)),slot[:2] if slot else None)


def _edge_prefix(ident):
    return 'private.study.successor.'+ident+'.'


def link_observation(db,ident,doc,spec):
    """Record evidenced order in either ingestion order; a newer crawl alone establishes no correction."""
    from .archive import encoded
    from .local_knowledge import _norm
    version, _ = split_conditions(spec['conditions'])
    published=spec.get('verification',doc)['published_at']
    effective=spec['valid_from'] or published or spec.get('verified_at') or doc['acquired_at']
    for row in db.execute("""SELECT f.id,f.spec_json,d.url,d.published_at,d.acquired_at FROM local_knowledge_facts f
            JOIN local_knowledge_documents d ON d.id=f.document_id
            WHERE f.id!=? AND d.scope=? AND d.persona_id IS ? AND d.session_id IS ? AND d.grant_hash IS ?
            AND json_extract(f.entities,'$[0]')=? AND json_extract(f.attributes,'$[0]')=?""",
            (ident,doc['scope'],doc['persona_id'],doc['session_id'],doc['grant_hash'],_norm(spec['subject']),_norm(spec['predicate']))):
        other=json.loads(row['spec_json'])
        if subject_key(other)!=subject_key(spec):continue
        other_version,_=split_conditions(other['conditions'])
        if len(version)>1 or len(other_version)>1:continue
        old_published=other.get('verification',row)['published_at']
        old_time=other['valid_from'] or old_published or other.get('verified_at') or row['acquired_at']
        left=(row['id'],other,old_time,other_version,old_published)
        right=(ident,spec,effective,version,published)
        if old_time>effective:left,right=right,left
        _,before,t0,v0,published0=left
        _,after,t1,v1,published1=right
        if bool(v0)!=bool(v1):continue
        if t0==t1:
            ranks={'rumor':0,'announced':1,'observed':2,'fact':2}
            if (sorted(v0),ranks[before['state']])>(sorted(v1),ranks[after['state']]):
                left,right=right,left
                _,before,t0,v0,published0=left
                _,after,t1,v1,published1=right
        if v0!=v1:
            ordered=max(v1)>max(v0) and after['state'] in ('observed','fact')
        elif before['state']!=after['state']:
            ordered=(before['state']=='rumor' and after['state'] in ('announced','observed','fact')
                     or before['state']=='announced' and after['state']=='observed')
        else:
            ordered=(canonical_url(row['url'])==canonical_url(doc['url']) and published0 and published1
                     and published0<published1 and before['value']!=after['value'])
        if ordered:
            edge={'effective':t1,'state':after['state'],'versions':sorted(v1)}
            db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',(_edge_prefix(left[0])+right[0],encoded(edge)))
            db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',('private.study.effective.'+right[0],encoded(t1)))


def superseded(db,ident,when,versions,state):
    # A withdrawn replacement does not reinstate a revision already retired.
    for row in db.execute("""SELECT f.spec_json,d.published_at,d.acquired_at FROM local_knowledge_facts f
            JOIN local_knowledge_documents d ON d.id=f.document_id WHERE f.previous_id=?""",(ident,)):
        spec=json.loads(row['spec_json'])
        new_versions,_=split_conditions(spec['conditions'])
        if ((versions and new_versions!=versions) or
                (state in ('rumor','announced') and state!=spec['state'])):continue
        if (spec['valid_from'] or spec.get('verification',row)['published_at'] or spec.get('verified_at') or row['acquired_at'])<=when:
            return True
    prefix=_edge_prefix(ident)
    for row in db.execute('SELECT value FROM knowledge_control WHERE key>=? AND key<?',(prefix,prefix+'~')):
        edge=json.loads(row[0])
        if versions and versions!={tuple(v) for v in edge['versions']}:continue
        if state in ('rumor','announced') and state!=edge['state']:continue
        if edge['effective']<=when:return True
    return False


def project(candidates, *, when, today, historical, archived_state, current_only, state, versions, after_commit, fact_id,allow_rumor=False):
    """Resolve applicability before freshness, so expired updates cannot revive older values."""
    from .local_knowledge import _norm,_calendar_value,compatible_dates,compatible_launch_sites,clock_precision
    groups={}
    for row,spec,fact in candidates:
        key=(row['scope'],row['persona_id'],row['session_id'],row['consent_revision'],row['grant_hash'],subject_key(spec))
        groups.setdefault(key,[]).append((row,spec,fact))
    if len({(*key[:-1],key[-1][:-1]) for key in groups})>1:
        return 'conflict',[]
    selected=[]
    outcome='insufficient'
    for key,rows in groups.items():
        confirmed_versions=[v for _,s,_ in rows if s['state']!='rumor' for v in split_conditions(s['conditions'])[0]]
        archived=not (current_only or historical) and (archived_state or bool(versions and confirmed_versions and max(versions)<max(confirmed_versions)))
        if versions:
            rows=[item for item in rows if split_conditions(item[1]['conditions'])[0]==versions
                  or (not split_conditions(item[1]['conditions'])[0] and versions==query_parts(item[1]['subject'])[0])]
            if rows and not (current_only or historical) and all(s['valid_until'] and s['valid_until']<=today for _,s,_ in rows):
                archived=True
        elif confirmed_versions and state=='observed':
            rows=[item for item in rows if split_conditions(item[1]['conditions'])[0]=={max(confirmed_versions)}]
        if state:
            rows=[item for item in rows if item[1]['state']==state]
        elif not allow_rumor or any(s['state']!='rumor' for _,s,_ in rows):
            rows=[item for item in rows if item[1]['state']!='rumor']
        if not rows:continue
        numbered=[v for _,s,_ in rows for v in split_conditions(s['conditions'])[0]]
        if numbered:
            wanted=versions or {max(numbered)}
            rows=[item for item in rows if split_conditions(item[1]['conditions'])[0]==wanted]
        if not rows:continue
        if not state and any(s['state']=='observed' for _,s,_ in rows):
            rows=[item for item in rows if item[1]['state']!='announced']
        # A newer dated revision of the same URL replaces older statements from that URL.
        latest={}
        for r,s,_ in rows:
            if r['published_at']:
                source=(canonical_url(r['url']),s['state'])
                at,values=latest.get(source,('',set()))
                if r['published_at']>at:at,values=r['published_at'],set()
                if r['published_at']==at:values.add(_norm(s['value']))
                latest[source]=at,values
        valid=[]
        for r,s,f in rows:
            if r['published_at']:
                newest,values=latest[canonical_url(r['url']),s['state']]
                if r['published_at']<newest and _norm(s['value']) not in values:continue
            if not archived and s['valid_until'] and when>=s['valid_until']:
                outcome='expired';continue
            if not archived and not historical and s['expires_at'] and today>=s['expires_at']:
                outcome='expired';continue
            valid.append((r,s,f))
        # Compare precise, eligible observations within this claim family. A context-relative
        # clock with the same reading supplies no competing calendar date.
        clocks=[(clock_precision(f['value']),s['state']) if 'witnesses' in s else (None,None) for _,s,f in valid]
        dated={(c[1],c[2],state) for c,state in clocks if c and c[0]=='absolute'}
        if dated:
            valid=[row for row,(c,state) in zip(valid,clocks) if not (c and c[0]=='relative' and
                   any(time==c[1] and phase==state and (zone==c[2] or not zone or not c[2]) for time,zone,phase in dated))]
        equal={}
        for r,s,f in valid:
            slot=knowledge_catalog.entry(f)
            value=slot[2] if slot else _norm(f['value'])
            if re.search('日期|date',f['predicate'],re.I) and (date:=_calendar_value(value)) and date[0]:
                value='%04d-%02d-%02d'%date
            revision=(value,s['state'],tuple(sorted(split_conditions(s['conditions'])[0])),s['valid_from'],s['valid_until'])
            equal.setdefault(revision,[]).append((r,s,f))
        # Conflicts are assessed before selecting a specific receipt or fresh acquisition.
        if len({k[0] for k in equal})>1:
            combined=[f for _,_,f in valid]
            from .knowledge_coverage import compatible_places
            if not (compatible_dates(combined) or compatible_launch_sites(combined) or compatible_places(combined)):
                return 'conflict',[]
        for revision,items in equal.items():
            items.sort(key=lambda item:(item[2]['verified_at'],item[2]['id']),reverse=True)
            primary=next((f for r,s,f in items if (fact_id is None or f['id']==fact_id)
                          and (not after_commit or (s['turn_source'] and s['turn_source'][0]==after_commit))),None)
            if primary is None:continue
            claim=fingerprint(key)
            since=revision[3] or max((r.get('effective_since') or '' for r,_,_ in items),default='')
            docs={}; urls=set(); hashes=set()
            # Present the selected source first; preserve all independent and repeated receipts.
            for r,s,f in sorted(items,key=lambda item:item[2]['id']!=primary['id']):
                url=canonical_url(r['url'])
                if r['id'] not in docs:
                    relation='primary' if not docs else 'same_url' if url in urls else 'same_content' if r['sha256'] in hashes else 'additional_source'
                    docs[r['id']]=dict(document_id=r['id'],url=f['url'],title=f['title'],text=f['text'],span=f['span'],
                                       date=f['date'],source_kind=f['source_kind'],sha256=r['sha256'],relation=relation,receipts=[])
                docs[r['id']]['receipts'].append(f['id'])
                hashes.add(r['sha256']);urls.add(url)
            selected.append(primary|{'claim_id':claim,'revision_id':fingerprint([claim,revision,since]),
                            'source_ids':list(docs),'evidence':list(docs.values())})
    if not selected:return outcome,[]
    # Preserve the existing order for numbered outlines and the local material budget.
    order=knowledge_catalog.catalog_order(selected)
    if order is None and len({(p[:2] if (p:=knowledge_catalog.entry(f)) else None) for f in selected})>1:
        return 'conflict',[]
    if order:selected.sort(key=lambda f:order[f['id']])
    return 'hit',selected[:8]
