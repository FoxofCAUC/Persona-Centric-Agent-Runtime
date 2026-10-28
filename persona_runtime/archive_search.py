"""Administrator lexical archive queries and read-only working-set previews."""
import sqlite3
import time

from .archive import encoded, read_exact, reference_group, reference_selection, serialize_reference_block
from .archive_corpus import capture, window_manifest
from .archive_index import ArchiveIndex
from .archive_query import (Budget, SearchFailure, validate, normal, lexical, matches,
                            epoch, source_epoch, MAX_CANDIDATES, MAX_RESULT_CHARS)
from .models import identifier, RuntimeErrorBase


PARTITION_FIELDS={'persona_id','channel','bot','peer','session_id','window'}


def partition_spec(args):
    if set(args)-PARTITION_FIELDS:
        raise SearchFailure('invalid_query')
    return validate(args | {'query':'status'})


def protected(method):
    def invoke(self, **args):
        try:
            return method(self, **args)
        except SearchFailure as error:
            code=str(error)
            if code not in {'invalid_query','unavailable','cache_busy','cache_generation_changed','operation_conflict',
                            'index_stale','query_budget_exceeded'}:
                return {'status':'unavailable','reason':code}
            return {'status':code}
        except sqlite3.OperationalError as error:
            code='query_budget_exceeded' if 'interrupt' in str(error) else 'cache_busy' if 'locked' in str(error) else 'unavailable'
            return {'status':code,'reason':'storage_error'}
        except (sqlite3.Error,OSError,ValueError):
            return {'status':'unavailable','reason':'storage_error'}
    return invoke


class ArchiveSearchService:
    def __init__(self,store):
        self.store=store
        self.index=ArchiveIndex(store)

    @protected
    def status(self,**args):
        spec=partition_spec(args)
        # Validate existence without disclosing other personas or opening a cache.
        with self.store.connection() as conn:
            if not conn.execute('SELECT 1 FROM personas WHERE id=?', (spec['scope']['persona_id'],)).fetchone():
                raise SearchFailure('unavailable')
        return {'status':'ok','effective_scope':spec['scope'],'window':spec['window'],**self.index.status(spec)}

    @protected
    def search(self,**args):
        spec=validate(args);budget=Budget();query_id=identifier()
        with self.index.locked():
            first=capture(self.store,spec,budget)
            state=self.index.synchronize(first,spec,budget)
            docs,path=self.index.candidates(spec,state,budget)
        # Re-reading the full partition also detects newly completed sources
        # that were absent from the old candidate set, including zero hits.
        current=capture(self.store,spec,budget)
        if first['digest']!=current['digest']:
            raise SearchFailure('index_stale')
        canonical_documents={d['document_id']:d for d in current['documents']}
        if any(not isinstance(doc,dict) or doc!=canonical_documents.get(doc.get('document_id')) for doc in docs):
            raise SearchFailure('index_stale')
        axis=spec['time_axis'];start=epoch(spec['start']);end=epoch(spec['end'])
        missing=sum(source_epoch(d['times'][axis],axis) is None for d in current['documents'])
        have_time=len(current['documents'])-missing
        terms=lexical(spec['query'])[0]
        selected=[]
        for doc in docs:
            budget.check()
            when=source_epoch(doc['times'][axis],axis)
            if ((start is not None or end is not None) and when is None
                    or start is not None and when<start or end is not None and when>=end):
                continue
            if not matches(doc['text'],spec['query'],spec['match_mode'],
                           'substring' if not state['fts'] else 'bigram'):
                continue
            doc_terms=set(lexical(doc['text'],document=True)[0])
            rank=(not bool(spec['query'].strip() and normal(spec['query']) in normal(doc['text'])),
                  -len(set(terms)&doc_terms), -(when if when is not None else float('-inf')),
                  doc['source_kind'],doc['document_id'])
            selected.append((rank,doc))
        selected.sort(key=lambda item:item[0])
        limited=len(selected)>MAX_CANDIDATES
        selected=selected[:MAX_CANDIDATES]
        groups_by_id={g['turn_id']:g for g in current['groups']}
        ordered={}
        for _,doc in selected:
            target=doc['target']
            item=ordered.setdefault(doc['turn_id'],{'targets':[],'matches':[]})
            if target not in item['targets']:
                item['targets'].append(target)
                item['matches'].append({'target':target,'source_kind':doc['source_kind'],
                    'times':doc['times'],'snippet':doc['text'][:240],'snippet_is_complete':len(doc['text'])<=240})
        groups=[];omitted=[];characters=0
        for tid,hit in ordered.items():
            if len(groups)>=spec['limit']:
                break
            group=groups_by_id[tid] | {'matched_targets':hit['targets'],'matches':hit['matches']}
            size=len(encoded(group))
            if characters+size>MAX_RESULT_CHARS:
                omitted.append({'turn_id':tid,'matched_targets':hit['targets'],'reason':'group_budget_exceeded'})
                continue
            characters+=size;groups.append(group)
        status='ok' if groups else 'query_budget_exceeded' if omitted else 'coverage_gap' if (start is not None or end is not None) and missing and not have_time else 'no_matches'
        result={'status':status,'query_id':query_id,'effective_scope':spec['scope'],
            'effective_query':{'query':spec['query'],'match_mode':spec['match_mode'],
                               'effective_match_mode':'literal_substring' if not state['fts'] or lexical(spec['query'])[1] else spec['match_mode'],
                               'normalization':'NFKC+casefold'},
            'time_filter':{'axis':axis,'start':spec['start'],'end':spec['end'],'interval':'[start,end)'},
            'window_manifest':window_manifest(current),**state,'read_at':current['read_at'],'freshness':'verified_at_read',
            'degraded':not state['fts'],'path':path,'groups':groups,
            'coverage':{'missing_selected_time':missing,'time_covered':have_time,'candidate_limit':limited,
                        'unexpanded_groups':omitted,'pagination':False,'result_limit':spec['limit']},
            'budget':{'body_characters':characters,'max_body_characters':MAX_RESULT_CHARS,
                      'source_documents':len(current['documents']),'source_characters':current['characters']},
            'elapsed_ms':round((time.monotonic()-budget.started)*1000,3)}
        encoded(result);budget.check()
        return result

    @protected
    def rebuild(self,**args):
        operation={k:args.pop(k,None) for k in ('cache_instance_id','expected_generation','operation_id')}
        if (type(operation['expected_generation']) is not int or operation['expected_generation']<0
                or not isinstance(operation['operation_id'],str) or not 0<len(operation['operation_id'])<=200
                or '\x00' in operation['operation_id'] or (operation['cache_instance_id'] is not None
                    and (not isinstance(operation['cache_instance_id'],str) or len(operation['cache_instance_id'])>200))):
            raise SearchFailure('invalid_query')
        spec=partition_spec(args);budget=Budget()
        with self.index.locked():
            corpus=capture(self.store,spec,budget)
            before=self.index.status(spec)
            if before['state']=='corrupt':
                if operation['cache_instance_id'] is not None or operation['expected_generation']!=0:
                    raise SearchFailure('cache_generation_changed')
                self.index.rebuild_corrupt()
            state=self.index.synchronize(corpus,spec,budget,force=True,operation=operation)
        return {'status':'ok',**state,'freshness':'unknown','operation_id':operation['operation_id']}

    @protected
    def preview(self,**args):
        spec=validate(args,preview=True);budget=Budget()
        first=capture(self.store,spec,budget)
        allowed={g['turn_id'] for g in first['groups']}
        selected={};selections=[]
        with self.store.connection() as conn:
            budget.watch(conn)
            for target in spec['targets']:
                budget.check()
                item=read_exact(conn,spec['scope'],target)
                if item['status']!='found' or item['turn_id'] not in allowed:
                    raise SearchFailure('unavailable')
                tid=item['turn_id']
                if tid not in selected:
                    selected[tid]=reference_group(item)
                if target not in selected[tid]['targets']:
                    selected[tid]['targets'].append(target)
                selections.append(reference_selection('00000000-0000-0000-0000-000000000000',item))
        current=capture(self.store,spec,budget)
        if current['digest']!=first['digest']:
            raise SearchFailure('index_stale')
        count=len(serialize_reference_block(list(selected.values()),selections))
        fits=count<=12000
        result={'status':'ok' if fits else 'query_budget_exceeded','effective_scope':spec['scope'],
                'window_manifest':window_manifest(current),'selected_targets':spec['targets'],
                'groups':list(selected.values()) if fits else [],'reference_characters':count,
                'within_reference_budget':fits,'total_input_checked':False,'creates_request':False,
                'assumed_input_event_id_characters':36,'usage':'retrieval_preview','read_at':current['read_at']}
        encoded(result);budget.check()
        return result
