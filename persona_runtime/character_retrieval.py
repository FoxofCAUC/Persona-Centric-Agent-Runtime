"""Pure M7.5.2 selection; character_runtime owns dialogue policy and auditing."""
from copy import deepcopy
import json
from pathlib import Path

from .character import CharacterService, FIELDS, read_fact
from .character_context import ALIASES, render_parts, select_context
from .character_embedding import SemanticFailure, digest
from .models import RuntimeErrorBase
from .profile import encode

ALGORITHM = 'character-hybrid-rrf60-v1'
FALLBACK_CODES = frozenset({
    'embedding_unavailable', 'embedding_dependency_missing', 'embedding_dependency_mismatch',
    'embedding_model_invalid', 'embedding_busy', 'embedding_inference_failed',
    'embedding_invalid_vector', 'character_cache_missing', 'character_cache_invalid', 'character_cache_busy'})


def literal_match(term, text):
    term, text = term.casefold(), text.casefold()
    if not term:
        return False
    position = text.find(term)
    while position >= 0:
        end = position + len(term)
        if not term.isascii() or ((position == 0 or not ascii_word(text[position-1])) and
                                  (end == len(text) or not ascii_word(text[end]))):
            return True
        position = text.find(term, position + 1)
    return False


def ascii_word(char):
    return char.isascii() and (char.isalnum() or char == '_')


def config_checked(config):
    value = deepcopy(config)
    if value.get('algorithm') != ALGORITHM or value.get('rrf_k') != 60:
        raise SemanticFailure('character_retrieval_config_invalid')
    for kind in ('lore', 'example'):
        threshold = value.get('thresholds', {}).get(kind)
        if type(threshold) not in (float, int) or not .4 <= threshold <= .9 or abs(threshold*40-round(threshold*40))>1e-7:
            raise SemanticFailure('character_retrieval_config_invalid')
    if not value.get('model_fingerprint'):
        raise SemanticFailure('character_retrieval_config_invalid')
    return value


def load_config():
    return config_checked(json.loads((Path(__file__).parent/'character_retrieval_config.json').read_text(encoding='utf-8')))


def dependency_group(rows, root):
    def visit(item, depth, trail):
        row = rows.get(item)
        if row is None or not row['enabled'] or row['visibility'] != 'conversation':
            raise SemanticFailure('dependency_unavailable')
        if depth > 2 or item in trail:
            raise SemanticFailure('dependency_depth_or_cycle')
        result=[]
        for dep in row['dependencies']:
            result.extend(visit(dep,depth+1,trail|{item}))
        return list(dict.fromkeys(result+[item]))
    return visit(root,0,set())


def rank_candidates(version, members, semantic, config, scenarios=(), *, apply_threshold=True):
    """Pure deterministic fusion; no cache or authority reads here."""
    ranked, rejected = {}, []
    for kind, rows in (('lore',version['data']['lorebook']),('example',version['data']['examples'])):
        allowed={r['id']:r for r in rows if r['enabled'] and (kind!='lore' or r['visibility']=='conversation')}
        matches={}; terms={}
        for item,row in allowed.items():
            labels=(([row['title']] if kind=='lore' else []) + row['keywords'])
            terms[item]={label.casefold() for label in labels}
            hits=[]
            for i,member in enumerate(members):
                hits.extend({'member_index':i,'term':label,'source':'title' if kind=='lore' and label==row['title'] else 'keyword'}
                            for label in dict.fromkeys(labels) if literal_match(label,member))
            if kind=='example':
                hits.extend({'member_index':None,'term':signal,'source':'scenario'}
                            for signal in scenarios if literal_match(signal,row['scenario']))
            matches[item]=hits
        ambiguous=set(); supported=set()
        for item,hits in matches.items():
            for hit in hits:
                if hit['source']=='scenario':
                    continue
                owners={key for key,labels in terms.items() if hit['term'].casefold() in labels}
                if len(owners)<2:
                    continue
                distinct=[owner for owner in owners if any(
                    h['member_index']==hit['member_index'] and
                    sum(h['term'].casefold() in labels for labels in terms.values())==1
                    for h in matches[owner])]
                # Ambiguity resolved only by another literal clue in the same member.
                if not distinct:
                    ambiguous.update(owners)
                else:
                    ambiguous.update(owners-set(distinct))
                    supported.update(distinct)
        ambiguous.difference_update(supported)
        for item in sorted(ambiguous):
            rejected.append({'kind':kind,'item_id':item,'reason':'ambiguous_alias'})
        lex=sorted((key for key,hits in matches.items() if hits and key not in ambiguous),
                   key=lambda key:(-sum(2 if h['source']=='title' else 1 for h in matches[key]),
                                   -allowed[key].get('priority',0),key))
        sem=[]; raw={}
        for candidate in semantic.get(kind,[]):
            item=candidate['item_id']
            if item not in allowed or candidate.get('source_hash') != digest(allowed[item]):
                raise SemanticFailure('character_source_changed')
            raw[item]=candidate
            if item in ambiguous:
                continue
            if apply_threshold and candidate['score'] < config['thresholds'][kind] and item not in lex:
                rejected.append({'kind':kind,'item_id':item,'reason':'below_threshold','score':candidate['score']})
                continue
            sem.append(item)
        positions=[{key:i+1 for i,key in enumerate(order)} for order in (lex,sem)]
        values=[]
        for item in set(lex+sem):
            hits=matches[item]
            values.append({'kind':kind,'item_id':item,'origin_type':'creator_setting',
                'source_hash':digest(allowed[item]), 'lexical_hits':hits,
                'match_types':(['lexical'] if item in positions[0] else [])+(['semantic'] if item in positions[1] else []),
                'score':raw[item]['score'] if item in raw else None,
                'semantic_member_index':raw[item].get('member_index') if item in raw else None,
                'rrf':sum(1/(60+pos[item]) for pos in positions if item in pos),
                'lexical_strength':sum(2 if h['source']=='title' else 1 for h in hits),
                'priority':allowed[item].get('priority',0)})
        ranked[kind]=sorted(values,key=lambda r:(-r['rrf'],-r['lexical_strength'],-r['priority'],r['item_id']))
    return ranked,rejected


def assemble(version,members,ranked,example_limit):
    """Groups are rendered from their union so shared dependencies survive trimming."""
    data=version['data'];parts=[];removed=[];facts=[];anchor_used=0;dynamic_used=0
    def part(kind,item,content,required=False):
        return {'kind':kind,'id':item,'text':encode({'kind':kind,'id':item,'data':content}),'required':required}
    for group in ('identity','social','world'):
        for field in FIELDS[group]:
            value=data[group][field]
            if value is None or value==[]:continue
            p=part('anchor',group+'.'+field,value)
            cost=len(p['text'])+1
            if anchor_used+cost<=800:parts.append(p);anchor_used+=cost
            else:removed.append({'kind':'anchor','item_id':p['id'],'reason':'character_budget'})
    for path,aliases in ALIASES.items():
        if not any(path in member.casefold() or (('你' in member or '角色' in member) and
                any(alias in member.casefold() for alias in aliases)) for member in members):continue
        fact=read_fact(version,path)
        if path=='age.birthday' and fact['value'] is None and data['age']['birth_date']:
            fact['value']=data['age']['birth_date'][5:];fact['status']='value'
        p=part('fact',path,{k:fact[k] for k in ('value','status','unit')},True)
        dynamic_used+=len(p['text'])+1
        if dynamic_used>1600:raise RuntimeErrorBase('character_required_fact_too_large')
        parts.append(p);facts.append(path)
    lore={r['id']:r for r in data['lorebook']};groups=[];chosen_examples=[]
    def lore_part(current):
        ids=list(dict.fromkeys(item for group in current for item in group['members']))
        return ids, part('lore_group','selected-lore',[
            {'id':item,'title':lore[item]['title'],'text':lore[item]['text']} for item in ids]) if ids else None
    for candidate in ranked['lore']:
        root=candidate['item_id']
        try:members_in_group=dependency_group(lore,root)
        except SemanticFailure as exc:
            removed.append({'kind':'lore','item_id':root,'reason':str(exc)});continue
        proposed=groups+[{'root':root,'members':members_in_group}]
        ids,p=lore_part(proposed)
        if len(ids)>4 or dynamic_used+len(p['text'])+1>1600:
            removed.append({'kind':'lore','item_id':root,'reason':'item_limit' if len(ids)>4 else 'character_budget'});continue
        groups=proposed
    examples={r['id']:r for r in data['examples']};example_used=0
    for candidate in ranked['example']:
        item=candidate['item_id'];row=examples[item]
        p=part('example',item,{'scenario':row['scenario'],'text':row['text']})
        if len(chosen_examples)>=example_limit or example_used+len(p['text'])+1>600:
            removed.append({'kind':'example','item_id':item,'reason':'item_limit' if len(chosen_examples)>=example_limit else 'character_budget'});continue
        chosen_examples.append(p);example_used+=len(p['text'])+1
    def materialize():
        ids,p=lore_part(groups)
        return parts+([p] if p else [])+chosen_examples,ids
    while True:
        complete,ids=materialize()
        if len(render_parts(complete))<=3000:break
        if chosen_examples:
            dropped=chosen_examples.pop();kind='example';item=dropped['id']
        elif groups:
            dropped=groups.pop();kind='lore';item=dropped['root']
        else:
            at=next((i for i in range(len(parts)-1,-1,-1) if not parts[i]['required']),None)
            if at is None:raise RuntimeErrorBase('character_required_fact_too_large')
            dropped=parts.pop(at);kind='anchor';item=dropped['id']
        removed.append({'kind':kind,'item_id':item,'reason':'character_total_budget'})
    assert all(set(dependency_group(lore,g['root'])).issubset(ids) for g in groups)
    budgets={name:sum(len(p['text'])+1 for p in complete if p['kind'] in kinds)
             for name,kinds in {'anchor':('anchor',),'dynamic':('fact','lore_group'),'examples':('example',)}.items()}
    budgets['total']=len(render_parts(complete))
    return {'version_id':version['id'],'revision':version['revision'],'origin_type':'creator_setting',
            'parts':complete,'text':render_parts(complete),'fact_paths':facts,'lore_ids':ids,
            'example_ids':[p['id'] for p in chosen_examples],'dependency_groups':groups,
            'removed':removed,'budgets':budgets,'miss_reason':None if complete else 'no_relevant_character_data'}


class CharacterCandidateRetriever:
    def __init__(self,store,index,config=None):
        self.characters=CharacterService(store);self.index=index
        self.config=config_checked(config) if config is not None else load_config()

    def retrieve(self,persona_id,version_id,members,*,mode='hybrid',scenario=None,example_limit=2):
        if (not persona_id or not version_id or mode not in ('lexical','hybrid') or
                type(example_limit) is not int or example_limit not in (2,4) or
                not isinstance(members,(list,tuple)) or not 1<=len(members)<=20 or
                any(not isinstance(t,str) or not t.strip() or len(t)>24000 for t in members)):
            raise SemanticFailure('character_retrieval_invalid_input')
        if scenario is not None and not isinstance(scenario,(str,list,tuple)):
            raise SemanticFailure('character_retrieval_invalid_input')
        scenarios=(scenario,) if isinstance(scenario,str) else tuple(scenario or ())
        if any(not isinstance(s,str) or not s or len(s)>200 for s in scenarios) or len(scenarios)>20:
            raise SemanticFailure('character_retrieval_invalid_input')
        version=self.characters.show(persona_id,version_id)
        before=digest(version)
        degraded=None;semantic={};metadata={};ranked={'lore':[],'example':[]};rejected=[]
        if mode=='hybrid':
            try:
                response=self.index.query(persona_id,version_id,members,per_kind_limit=10)
                if response['version_id']!=version_id:
                    raise SemanticFailure('character_source_changed')
                if response['model_fingerprint']!=self.config['model_fingerprint']:
                    raise SemanticFailure('embedding_dependency_mismatch')
                semantic=response['by_kind']
                metadata={k:response[k] for k in ('model_fingerprint','index_fingerprint')}
            except SemanticFailure as exc:
                if str(exc) not in FALLBACK_CODES:raise
                degraded=str(exc)
            except (OSError,MemoryError):
                degraded='character_semantic_resource_failure'
        if mode=='lexical' or degraded:
            selection=select_context(version,'\n\n'.join(members),members=members,scenario=scenarios,example_limit=example_limit)
        else:
            ranked,rejected=rank_candidates(version,members,semantic,self.config,scenarios)
            selection=assemble(version,members,ranked,example_limit)
        if digest(self.characters.show(persona_id,version_id))!=before:
            raise SemanticFailure('character_source_changed')
        if 'budgets' not in selection:
            selection['budgets']={name:sum(len(p['text'])+1 for p in selection['parts'] if p['kind'] in kinds)
                for name,kinds in {'anchor':('anchor',),'dynamic':('fact','lore_group'),'examples':('example',)}.items()}
            selection['budgets']['total']=len(selection['text'])
            selection['dependency_groups']=[{'root':p['id'],'members':[r['id'] for r in json.loads(p['text'])['data']]}
                                            for p in selection['parts'] if p['kind']=='lore_group']
        selected=[{'kind':kind,'item_id':item,'origin_type':'creator_setting'} for kind,items in
                  (('fact',selection['fact_paths']),('lore',selection['lore_ids']),('example',selection['example_ids'])) for item in items]
        return selection | {'persona_id':persona_id,'algorithm':self.config['algorithm'],
            'raw_candidates':{kind:[{k:r[k] for k in ('item_id','score','member_index') if k in r}
                                    for r in rows[:10]] for kind,rows in semantic.items()},
            'config_fingerprint':digest(self.config),'requested_mode':mode,
            'effective_mode':'lexical' if degraded or mode=='lexical' else 'hybrid',
            'selected':selected,'rejected':rejected,'candidates':ranked,'degrade_reason':degraded,
            'activation_authorized':False,**metadata}

    @staticmethod
    def diagnostics(result):
        return {k:v for k,v in result.items() if k not in ('parts','text')}
