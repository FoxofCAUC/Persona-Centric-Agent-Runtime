"""Question coverage and literal witnesses for reviewed local-first acquisition."""
from datetime import datetime, timedelta
import json
import re

from . import local_knowledge as library
from .archive import encoded, fingerprint
from .models import RuntimeErrorBase


VERSION = 'local-first-v4'
STUDY_VERSION = 'source-facts-v2'
OUTPUT_TOKENS = 8192
RETRY_OUTPUT_TOKENS = 32768
KINDS = {'applicability', 'certainty', 'attribution', 'location_parent', 'occurrence_date'}


def truncated_response(audit):
    diagnostics = (audit or {}).get('diagnostics') or {}
    return diagnostics.get('response_headers_received') is True and diagnostics.get('finish_reason') == 'length'


def aligned_runs(original, label):
    """Return uniquely located contiguous pieces of an order-preserving copy."""
    forward=[]
    pos=0
    for char in label:
        pos=original.find(char,pos)
        if pos<0:return []
        forward.append(pos)
        pos+=1
    stop=len(original)
    for char,start in zip(reversed(label),reversed(forward)):
        stop=original.rfind(char,0,stop)
        if stop!=start:return []
    runs=[]
    for index in forward:
        if runs and runs[-1][1]==index:
            runs[-1][1]=index+1
        else:
            runs.append([index,index+1])
    return runs


def requirements(data, question):
    if not isinstance(data, dict) or set(data) != {'requirements', 'facts'}:
        raise RuntimeErrorBase('knowledge_study_invalid_result')
    parts = data['requirements']
    if (not isinstance(parts, list) or not 1 <= len(parts) <= 8
            or any(not isinstance(p, str) or not p.strip() or len(p) > 180 for p in parts)
            or len(set(parts)) != len(parts)
            or not isinstance(data['facts'], list) or len(data['facts']) > 8):
        raise RuntimeErrorBase('knowledge_study_invalid_result')
    anchored=[]
    for i,part in enumerate(parts):
        if part in question:
            anchored.append(part)
            continue
        # A split compound question may copy the common subject and a later facet.
        # Only keep an exact, discriminating piece when every copied character aligns uniquely.
        peers=parts[:i]+parts[i+1:]
        runs=[question[a:b] for a,b in aligned_runs(question,part) if b-a>=2]
        candidates=sorted((v for v in runs if not any(v in p for p in peers)),key=len,reverse=True)
        if not candidates or len(candidates)>1 and len(candidates[0])==len(candidates[1]):
            raise RuntimeErrorBase('knowledge_study_invalid_result')
        anchored.append(candidates[0])
    if len(set(anchored))!=len(parts):
        raise RuntimeErrorBase('knowledge_study_invalid_result')
    return anchored, data['facts']


def premise_dependencies(data, parts):
    if not isinstance(data,list) or len(data)>len(parts)-1:
        raise RuntimeErrorBase('knowledge_study_invalid_result')
    parents={}
    for edge in data:
        if not isinstance(edge,dict) or set(edge)!={'item','premise'}:
            raise RuntimeErrorBase('knowledge_study_invalid_result')
        item,premise=edge['item'],edge['premise']
        if type(item) is not int or type(premise) is not int or not 0<=premise<item<len(parts) or item in parents:
            raise RuntimeErrorBase('knowledge_study_invalid_result')
        parents[item]=premise
    return [{'item':item,'premise':parents[item]} for item in sorted(parents)]


def witness(container, text, occurrence):
    """Resolve a zero-based occurrence in the offered field, retaining exact offsets."""
    if type(occurrence) is not int or occurrence < 0:
        raise RuntimeErrorBase('local_knowledge_invalid_span')
    positions = re.finditer(re.escape(text), container)
    for i, match in enumerate(positions):
        if i == occurrence:
            return match.span()
    raise RuntimeErrorBase('local_knowledge_evidence_mismatch')


def located(source, location, limit):
    if (not isinstance(location, dict) or set(location) != {'field', 'text', 'occurrence'}
            or location['field'] not in ('title', 'text')):
        raise RuntimeErrorBase('knowledge_study_invalid_fact')
    text = library._text(location['text'], limit)
    start, end = witness(source[location['field']], text, location['occurrence'])
    offset = source['start'] if location['field'] == 'text' else 0
    return {'field': location['field'], 'text': text, 'span': [start + offset, end + offset]}


def quote_location(source, raw):
    if isinstance(raw, str):
        text = library._text(raw, 1200)
        if source['text'].count(text) != 1:
            raise RuntimeErrorBase('knowledge_study_quote_not_unique')
        raw = {'field':'text', 'text':text, 'occurrence':0}
    return located(source, raw, 1200)


def literal_value(text, proposed):
    value=library._text(proposed,600)
    if value in text:
        return value
    runs=aligned_runs(text,value)
    if len(runs)<2 or min(runs[0][1]-runs[0][0],runs[-1][1]-runs[-1][0])<2:
        raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    from .knowledge_study import _LIMIT
    for before,after in zip(runs,runs[1:]):
        gap=text[before[1]:after[0]]
        if (_LIMIT.search(gap) or re.search(r'[。！？!?;；\n…]|\.(?=\s|$)|\b(?:not|except|unless|without|only|might|may)\b',gap,re.I)):
            raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
    return library._text(text[runs[0][0]:runs[-1][1]],600)


def subject_location(source, raw, quote):
    if not isinstance(raw, str):
        return located(source, raw, 128)
    text = library._text(raw, 128)
    if quote['text'].count(text) == 1:
        start = quote['span'][0] + quote['text'].index(text)
        return {'field':'text', 'text':text, 'span':[start,start+len(text)]}
    if source['title'].count(text) == 1:
        return located(source, {'field':'title', 'text':text, 'occurrence':0}, 128)
    raise RuntimeErrorBase('knowledge_study_ungrounded_subject')


def complete_excerpt(source, quote, value):
    text = source['text']
    offset = source['start']
    start, end = (position - offset for position in quote['span'])
    # A decimal point inside a number is not a sentence boundary.
    breaks = [m.end() for m in re.finditer(r'[。！？!?；;\n]|(?<!\d)\.(?=\s|$)|\.{3,}', text)]
    left = max((b for b in breaks if b <= start), default=0)
    right = min((b for b in breaks if b >= end), default=len(text))
    omitted = text[left:start] + text[end:right]
    if not omitted.strip(' \t\n。！？!?；;.'):
        return quote
    # Keep extra context visible. A bare value cropped from a larger assertion proves no relation.
    if quote['text'].strip(' \t\n。！？!?；;.') == value.strip(' \t\n。！？!?；;.') or right-left > 1200:
        raise RuntimeErrorBase('knowledge_study_omitted_qualifier')
    from .knowledge_study import _LIMIT
    if _LIMIT.search(text[left:start]) or _LIMIT.search(text[end:right]):
        raise RuntimeErrorBase('knowledge_study_omitted_qualifier')
    return {'field':'text','text':text[left:right],'span':[offset+left,offset+right]}


def adjacent_date_context(source, quote, ref):
    """Retain a neighboring source sentence as context without assigning its date to the claim."""
    offset=source['start']
    text=source['text']
    a,b=(p-offset for p in quote['span'])
    c,d=(p-offset for p in ref['span'])
    if ref['field']!='text' or not (d<=a or b<=c):
        return None
    gap=text[d:a] if d<=a else text[b:c]
    if '\n' in gap or '…' in gap or '...' in gap or len(re.findall(r'[。！？!?;；]|\.(?=\s|$)',gap))>1:
        return None
    start,end=min(a,c),max(b,d)
    breaks=[m.end() for m in re.finditer(r'[。！？!?;；\n]|\.(?=\s|$)',text)]
    start=max((p for p in breaks if p<=start),default=0)
    end=min((p for p in breaks if p>=end),default=len(text))
    if end-start>1200 or '\n\n' in text[start:end]:
        return None
    return {'field':'text','text':text[start:end],'span':[offset+start,offset+end]}


def admit(db, turn, base, request, row, needed, verified_at):
    from .knowledge_store import SOURCE_POLICY
    fields = {'source_id', 'subject', 'predicate', 'value', 'quote', 'state', 'qualifiers', 'supports'}
    optional = {'unit', 'valid_from', 'valid_until'}
    if not isinstance(row, dict) or not fields <= row.keys():
        raise RuntimeErrorBase('knowledge_study_invalid_fact')
    source = next((s for s in request['sources'] if s['id'] == row['source_id']), None)
    if not source:
        raise RuntimeErrorBase('knowledge_study_source_not_offered')
    supports = row['supports']
    if (not isinstance(supports, list) or len(supports) > len(needed)
            or any(type(i) is not int or not 0 <= i < len(needed) for i in supports)
            or len(set(supports)) != len(supports)):
        raise RuntimeErrorBase('knowledge_study_invalid_fact')
    quote = quote_location(source, row['quote'])
    if quote['field'] != 'text':
        raise RuntimeErrorBase('local_knowledge_invalid_span')
    subject = subject_location(source, row['subject'], quote)
    value = literal_value(quote['text'],row['value'])
    # An extra pointer may repeat an already verified field under a different key.
    # Other extra semantics and conflicting positions remain invalid.
    for key in row.keys()-fields-optional:
        ref=located(source,row[key],1200)
        if ref!=quote and ref!=subject and not (ref['field']=='text' and ref['text']==value
                and quote['span'][0]<=ref['span'][0]<ref['span'][1]<=quote['span'][1]):
            raise RuntimeErrorBase('knowledge_study_invalid_fact')
    quote = complete_excerpt(source, quote, value)
    start, end = quote['span']
    def attached(ref):
        return ref['field'] == 'title' or start <= ref['span'][0] < ref['span'][1] <= end
    if not attached(subject):
        raise RuntimeErrorBase('knowledge_study_ungrounded_subject')
    qualifiers = row['qualifiers']
    if not isinstance(qualifiers, list) or len(qualifiers) > 8:
        raise RuntimeErrorBase('knowledge_study_invalid_fact')
    typed = []
    historical = False
    verified = datetime.fromisoformat(verified_at)
    original_span = (start,end)
    for q in qualifiers:
        if not isinstance(q, dict) or set(q) != {'kind', 'at'} or not isinstance(q['kind'], str) or q['kind'] not in KINDS:
            raise RuntimeErrorBase('knowledge_study_invalid_fact')
        ref = located(source, q['at'], 128)
        in_claim=ref['field']=='title' or original_span[0]<=ref['span'][0]<ref['span'][1]<=original_span[1]
        if not in_claim:
            context=adjacent_date_context(source,quote,ref) if q['kind']=='occurrence_date' else None
            if context is None:
                raise RuntimeErrorBase('knowledge_study_omitted_qualifier')
            quote=context
            start,end=quote['span']
            ref=ref|{'context_only':True}
        if q['kind'] == 'location_parent' and (ref['field'] != 'text' or ref['text'] == value):
            raise RuntimeErrorBase('local_knowledge_evidence_mismatch')
        if q['kind'] == 'occurrence_date':
            day = library._calendar_value(ref['text'])
            if ref['field'] != 'text' or row['state'] not in ('fact','observed'):
                raise RuntimeErrorBase('local_knowledge_invalid_time')
            # Keep partial dates literal and under the normal freshness policy. Only a complete
            # past date can make the observation durable without an expiry.
            if in_claim and day and day[0]:
                if datetime(*day).date() > verified.date():
                    raise RuntimeErrorBase('local_knowledge_invalid_time')
                historical = True
        typed.append({'kind': q['kind'], **ref})
    options = {k: row[k] for k in optional if k in row}
    for key in ('valid_from', 'valid_until'):
        if key in options and (not isinstance(options[key], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', options[key]) or options[key] not in quote['text']):
            raise RuntimeErrorBase('local_knowledge_invalid_time')
    expires = verified + timedelta(seconds=SOURCE_POLICY['freshness_seconds'])
    conditions = [q['text'] for q in typed if q['kind'] == 'applicability']
    definition = row['state'] == 'fact' and row['predicate'] in ('定义','含义','基本原理') and not conditions
    ident = library.admit_turn_source(db, turn['persona_id'], turn['session_id'], turn['id'], source['id'],
        active_turn=turn['id'], instance=base['instance'], consent=base['consent'], versioned=True,
        subject=subject['text'], predicate=row['predicate'], value=value, quote=quote['text'], start=start, end=end,
        evidence_state=row['state'], expires_at=None if historical or definition else expires.isoformat(),
        attributes=[needed[i] for i in supports], conditions=conditions,
        witnesses={'subject': subject, 'qualifiers': typed}, **options)
    return ident, supports


def compatible_places(facts):
    """Only compare finer places when the organizer recorded an explicit parent in the same evidence."""
    if not facts or not all('witnesses' in f and f['kind'] in ('fact','observed','announced') for f in facts):
        return False
    scopes = {(f['subject'], f['predicate'], tuple(f['qualifications']), f['kind'], f.get('unit')) for f in facts}
    if len(scopes) != 1:
        return False
    parents = {}
    for f in facts:
        value = library._norm(f['value'])
        parents.setdefault(value, set()).update(library._norm(q['text']) for q in f['witnesses']['qualifiers']
                                                 if q['kind'] == 'location_parent')
    values = set(parents)
    levels = sorted(len(refs & values) for refs in parents.values())
    return levels == list(range(len(values))) and all(
        (a in parents[b]) != (b in parents[a]) for i,a in enumerate(parents) for b in list(parents)[i+1:])


def saved_plan(db, tid):
    row = db.execute('SELECT value FROM knowledge_control WHERE key=?', ('private.study.coverage.' + tid,)).fetchone()
    if not row:
        return None
    plan=json.loads(row[0])
    relation=db.execute('SELECT value FROM knowledge_control WHERE key=?',('private.study.dependencies.'+tid,)).fetchone()
    return plan|{'dependencies':json.loads(relation[0])} if relation else plan


async def resolve_dependencies(flow,turn,base,material,question,plan,facts):
    if not getattr(flow.providers,'relation_judge',False) or not plan or not facts or len(plan['requirements'])<2:
        return plan
    tid=turn['id']
    key='private.study.dependencies.'+tid
    with flow.store.connection() as db:
        if db.execute('SELECT 1 FROM knowledge_control WHERE key=?',(key,)).fetchone():
            return saved_plan(db,tid)
    parents={edge['item']:edge['premise'] for edge in plan.get('dependencies',[])}
    for item in range(1,len(plan['requirements'])):
        if item in parents:
            continue
        options=('independent',*(f'premise/{i}' for i in range(item)))
        state={'question':question,'requirements':plan['requirements'],'item':item}
        result=await flow.choice(tid,0,'J5.relation',state,options,base,material,
                                 receipt_node=f'study.J5.relation.{item}')
        if result!='independent':
            parents[item]=int(result.split('/')[1])
    edges=[{'item':item,'premise':parents[item]} for item in sorted(parents)]
    with flow.store.connection(write=True) as db:
        _,live,actual=flow._snapshot(db,tid)
        if live!=base or actual!=material:
            raise RuntimeErrorBase('private_reply_source_changed')
        db.execute('INSERT OR IGNORE INTO knowledge_control VALUES(?,?)',(key,encoded(edges)))
        current=saved_plan(db,tid)
        if current!=plan|{'dependencies':edges}:
            raise RuntimeErrorBase('private_reply_source_changed')
    return current


def warm_plan(db, turn, question, facts):
    from .knowledge_native_memory import eligible
    previous = db.execute("""SELECT r.turn_id,k.value FROM knowledge_study_reads r JOIN turns t ON t.id=r.turn_id
        JOIN knowledge_control k ON k.key='private.study.coverage.'||t.id WHERE t.session_id=? AND t.persona_id=?
        AND r.query_hash=? AND t.rowid<(SELECT rowid FROM turns WHERE id=?) ORDER BY t.rowid DESC""",
        (turn['session_id'],turn['persona_id'],fingerprint(question),turn['id']))
    for prior in previous:
        if not eligible(db,prior[0]):
            continue
        plan=saved_plan(db,prior[0])
        # Only re-use requirements still literally present in this authorized question.
        if any(part not in question for part in plan['requirements']):
            continue
        supports=dict(plan['supports'])
        for f in facts:
            if f['id'] in supports or 'witnesses' not in f:
                continue
            spec=db.execute('SELECT spec_json FROM local_knowledge_facts WHERE id=?',(f['id'],)).fetchone()
            labels=json.loads(spec[0])['attributes']
            supports[f['id']]=[i for i,part in enumerate(plan['requirements']) if part in labels]
        return plan|{'supports':supports}
    return None


def complete(plan, facts):
    return plan is None or set(range(len(plan['requirements']))) == {
        i for f in facts for i in plan['supports'].get(f['id'], [])}


def corrections(plan, facts):
    if not plan or not plan.get('dependencies'):
        return {}
    result={}
    edges=plan['dependencies']
    for premise in sorted({e['premise'] for e in edges}):
        excluded=set()
        for edge in edges:
            if edge['premise']==premise or edge['premise'] in excluded:
                excluded.add(edge['item'])
        needed=set(range(len(plan['requirements'])))-excluded
        chosen=[f for f in facts if needed.intersection(plan['supports'].get(f['id'],[]))]
        covered={i for f in chosen for i in plan['supports'][f['id']]}
        if needed<=covered:
            result['correct/'+str(premise)]=chosen
    return result


def question_order(flow, tid, prefix, question, facts):
    """Freeze the same query relevance ordering on cold and warm eligible evidence."""
    if len(facts)<2:
        return facts,False,False
    key=f'private.study.order.{prefix}.{tid}'
    by_id={f['id']:f for f in facts}
    with flow.store.connection(write=True) as db:
        stored=db.execute('SELECT value FROM knowledge_control WHERE key=?',(key,)).fetchone()
        if stored:
            manifest=json.loads(stored[0])
            if manifest['question']!=fingerprint(question) or set(manifest['ids'])!=set(by_id):
                raise RuntimeErrorBase('private_reply_source_changed')
            return [by_id[i] for i in manifest['ids']],manifest.get('complete_first',False),manifest.get('compact_first',False)
        prior=db.execute("SELECT 1 FROM private_reply_nodes WHERE turn_id=? AND node LIKE ? LIMIT 1",
                         (tid,prefix+'.J5.coverage.%')).fetchone()
        words=library._tokens(question)
        if prior or not 0<len(words)<=256:
            return facts,False,False
        expr=' OR '.join('"'+word+'"' for word in words)
        matches=db.execute("""SELECT f.id FROM local_knowledge_fts JOIN local_knowledge_facts f
            ON f.rowid=local_knowledge_fts.rowid WHERE local_knowledge_fts MATCH ?
            AND f.id IN (SELECT value FROM json_each(?)) ORDER BY rank,f.id""",(expr,encoded(list(by_id))))
        ranking=[row[0] for row in matches]
        ranking.extend(sorted(set(by_id)-set(ranking)))
        db.execute('INSERT INTO knowledge_control VALUES(?,?)',(key,encoded({'question':fingerprint(question),'ids':ranking,
                   'complete_first':True,'compact_first':True})))
    return [by_id[i] for i in ranking],True,True


def representatives(plan, facts):
    """Use the frozen relevance order to nominate a literal witness for each requested part."""
    selected=[]
    covered=set()
    for slot in range(len(plan['requirements'])):
        if slot in covered:
            continue
        for index,fact in enumerate(facts):
            supports=plan['supports'].get(fact['id'],[])
            if slot in supports:
                selected.append(index)
                covered.update(supports)
                break
    return selected


async def select(flow, turn, base, material, question, facts, plan, prefix):
    """J5 can remove irrelevant facts. Only a fully covered, accepted set can reach the writer."""
    tid = turn['id']
    conditional=getattr(flow.providers,'conditional_coverage',False)
    if plan and not conditional and 'dependencies' in plan:
        plan={k:v for k,v in plan.items() if k!='dependencies'}
    remaining = [f for f in facts if plan is None or plan['supports'].get(f['id'])]
    full_first=compact_first=False
    if getattr(flow.providers,'relation_judge',False):
        remaining,full_first,compact_first=question_order(flow,tid,prefix,question,remaining)
    for step in range(len(remaining)):
        alternative=corrections(plan,remaining)
        whole=complete(plan,remaining)
        if not whole and not alternative:
            break
        keys = (['complete'] if whole else [])+list(alternative)+['insufficient']
        full_keys=['complete','insufficient']
        choices = {}
        for i, f in enumerate(remaining):
            key = str(i)
            choices[key] = {k: f[k] for k in ('subject','predicate','text','value','title','kind','qualifications','witnesses','unit','date','valid_from','valid_until') if k in f}
            if plan:
                choices[key]['supports'] = plan['supports'][f['id']]
            rest=remaining[:i]+remaining[i+1:]
            if rest and (complete(plan,rest) or corrections(plan,rest)):
                keys.append('drop/' + key)
                if complete(plan,rest):
                    full_keys.append('drop/'+key)
        state = {'question': question, 'requirements': plan['requirements'] if plan else [],
                 'facts': choices}
        if conditional:
            state['dependencies']=(plan or {}).get('dependencies',[])
        if step==0 and compact_first and whole and plan and len(plan['requirements'])>1:
            indices=representatives(plan,remaining)
            if len(indices)<len(remaining):
                compact=state|{'facts':{str(i):choices[str(index)] for i,index in enumerate(indices)}}
                compact_node=f'{prefix}.J5.coverage.compact.{step}'
                short_choice=await flow.choice(tid,0,'J5',compact,('complete','insufficient'),base,material,receipt_node=compact_node)
                if short_choice=='complete':
                    with flow.store.connection() as db:
                        aid=db.execute("SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND node=? AND draft=0 AND state='received'",
                                       (tid,compact_node)).fetchone()[0]
                    return [remaining[i] for i in indices],aid
        node = f'{prefix}.J5.coverage.{step}'
        # Check the complete explanation before offering a shorter correction. Both choices
        # still require the factual judge's approval of the original question and quotes.
        detailed=full_first and whole and any(len(group)<len(remaining) for group in alternative.values())
        option = await flow.choice(tid, 0, 'J5', state, tuple(full_keys if detailed else keys), base, material, receipt_node=node)
        if detailed and option=='insufficient':
            node=f'{prefix}.J5.coverage.correction.{step}'
            fallback=[key for key in keys if key!='complete']
            option=await flow.choice(tid,0,'J5',state,tuple(fallback),base,material,receipt_node=node)
        with flow.store.connection() as db:
            aid = db.execute("SELECT attempt_id FROM private_reply_nodes WHERE turn_id=? AND node=? AND draft=0 AND state='received'",
                             (tid, node)).fetchone()[0]
        if option == 'complete':
            return remaining, aid
        if option in alternative:
            return alternative[option], aid
        if option == 'insufficient':
            return [], aid
        remaining.pop(int(option.split('/')[1]))
    return [], None


def warm_candidates(db, turn, base, question):
    """Rank scoped structured facts; re-read each through revision, grant and source checks."""
    from .knowledge_study import _bound_read
    if not library.enabled(db, turn['persona_id']):
        return []
    if not library.local_only(question) and library._REFRESH.search(question):
        return []
    words = library._tokens(question)
    if not 0 < len(words) <= 256:
        return []
    # Structured study records are session scoped. Filter the grant before ranking.
    consent = base['consent']
    revision = consent['revision'] if consent['enabled'] else 0
    grant = library._grant(db, base['instance'], turn['persona_id'], turn['session_id'], revision) if revision else None
    ids = db.execute("""SELECT f.id FROM local_knowledge_fts JOIN local_knowledge_facts f ON f.rowid=local_knowledge_fts.rowid
        JOIN local_knowledge_documents d ON d.id=f.document_id WHERE local_knowledge_fts MATCH ?
        AND d.scope='session' AND d.persona_id=? AND d.session_id=? AND f.withdrawn=0
        AND (d.consent_revision=0 OR (d.consent_revision=? AND d.grant_hash=?))
        AND json_type(f.spec_json,'$.witnesses')='object' ORDER BY rank,f.id LIMIT 32""",
        (' OR '.join('"'+w+'"' for w in words), turn['persona_id'], turn['session_id'], revision, grant))
    return _bound_read(db, turn, base, question, [r[0] for r in ids], for_selection=True)[1][:8]
