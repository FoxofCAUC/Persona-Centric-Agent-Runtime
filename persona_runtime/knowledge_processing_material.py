"""Read actual sections and preserve exact source offsets across model chunks."""
from contextlib import closing
import hashlib
import json
import sqlite3

from .archive import fingerprint
from .knowledge_service import package_path, read, REGISTRY, validate_used
from .models import RuntimeErrorBase


def source_id(evidence):
    return fingerprint({k: v for k, v in evidence.items() if k != 'quote'})


def collect(service, turn, results):
    candidates, overlay, seen = [], [], set()
    for result in results:
        for item in result.get('evidence', []):
            if item['kind'] == 'knowledge_claim' and source_id(item) not in seen:
                seen.add(source_id(item)); overlay.append(item)
    for rank in range(5):
      for result in results:
        for row in result.get('candidates', [])[rank:rank+1]:
            if row.get('kind') == 'knowledge_claim': continue
            identity = result.get('package_sha256')
            key = (identity, row['article_id'])
            if identity and key not in seen:
                seen.add(key); candidates.append((identity, row))
    materials = []
    with service.store.connection() as db:
        registry = read(db, 'registry', REGISTRY)['data']
        for identity, article in candidates[:5]:
            record = registry['packages'][identity]
            package = package_path(service.paths, record)
            with closing(sqlite3.connect((package / record['manifest']['database']['file']).as_uri()+'?mode=ro', uri=True)) as source:
                sections = source.execute('SELECT id,title,content FROM sections WHERE article_id=? ORDER BY id', (article['article_id'],)).fetchall()
            for section_id, title, body in sections:
                if not body: continue
                evidence = {'kind': 'encyclopedia', 'package_sha256': identity,
                    'data_revision': record['manifest']['data_revision'], 'article_id': article['article_id'],
                    'article_title': article['title'], 'section_id': section_id, 'section_title': title or '',
                    'section_sha256': hashlib.sha256(body.encode()).hexdigest(),
                    'source_snapshot_at': record['manifest'].get('source_snapshot_at'),
                    'validity': 'package_snapshot', 'start': 0, 'end': len(body),
                    'url': 'https://zh.wikipedia.org/wiki/' + article['title'].replace(' ', '_')}
                validate_used(db, service.paths, turn['persona_id'], turn['channel'], {'evidence': [evidence]})
                materials.append({'id': source_id(evidence), 'text': body, 'evidence': evidence})
        for evidence in overlay:
            validate_used(db, service.paths, turn['persona_id'], turn['channel'], {'evidence': [evidence]})
            # A reviewed claim's existing exact span is the relevant source unit.
            materials.append({'id': source_id(evidence), 'text': evidence['quote'], 'evidence': {k:v for k,v in evidence.items() if k != 'quote'}})
    return materials


def chunk(material, limit):
    start=0
    while start<len(material['text']):
        end=min(len(material['text']),start+limit)
        body = material['text'][start:end]
        evidence = dict(material['evidence'], start=material['evidence']['start']+start,
                        end=material['evidence']['start']+start+len(body))
        yield {'id': source_id(evidence), 'text': body, 'evidence': evidence}
        if end==len(material['text']):break
        start=max(start+1,end-180)


def groups(items, budget):
    batch, size = [], 0
    for item in items:
        length = len(json.dumps(item, ensure_ascii=False))
        if length > budget: raise RuntimeErrorBase('knowledge_processing_input_budget')
        if batch and size+length > budget:
            yield batch
            batch, size = [], 0
        batch.append(item); size += length
    if batch: yield batch


def bind(units, materials):
    available = {m['id']: m for m in materials}
    evidence, points, positions, spans = [], [], {}, []
    for unit in units:
        refs = []
        for ref in unit['refs']:
            material = available[ref['material_id']]
            offset = material['text'].index(ref['quote'])
            item = dict(material['evidence'], start=material['evidence']['start']+offset,
                        end=material['evidence']['start']+offset+len(ref['quote']), quote=ref['quote'])
            key = source_id(item)
            if key not in positions:
                positions[key] = len(evidence); evidence.append(item)
                spans.append({'material_id':material['id'],'start':offset,'end':offset+len(ref['quote']),'members':{positions[key]}})
            refs.append(positions[key])
        points.append({'text': unit['text'], 'kind': unit['kind'], 'evidence_indexes': refs})
    # Coalesce citation spans in the SAME original chunk. This changes source
    # windows, never the translated facts or their independent review verdict.
    while len(spans)>6 or sum(s['end']-s['start'] for s in spans)>3000:
        pairs=[]
        for i,a in enumerate(spans):
            for j in range(i+1,len(spans)):
                b=spans[j]
                if a['material_id']!=b['material_id']:continue
                left,right=min(a['start'],b['start']),max(a['end'],b['end'])
                delta=right-left-(a['end']-a['start'])-(b['end']-b['start'])
                pairs.append((delta,i,j,left,right))
        if not pairs:break
        delta,i,j,left,right=min(pairs)
        if delta>0 and sum(s['end']-s['start'] for s in spans)+delta>3000:break
        a,b=spans[i],spans[j]
        spans[i]={'material_id':a['material_id'],'start':left,'end':right,'members':a['members']|b['members']}
        spans.pop(j)
    merged=[];remap={}
    for index,span in enumerate(spans):
        material=available[span['material_id']]
        merged.append(dict(material['evidence'],start=material['evidence']['start']+span['start'],
            end=material['evidence']['start']+span['end'],quote=material['text'][span['start']:span['end']]))
        for old in span['members']:remap[old]=index
    evidence=merged
    for point in points:point['evidence_indexes']=list(dict.fromkeys(remap[i] for i in point['evidence_indexes']))
    if len(evidence) > 6 or len({(e.get('package_sha256'), e['article_id']) for e in evidence}) > 3 or sum(len(e['quote']) for e in evidence) > 3000:
        raise RuntimeErrorBase('knowledge_processing_evidence_budget')
    return points, evidence
