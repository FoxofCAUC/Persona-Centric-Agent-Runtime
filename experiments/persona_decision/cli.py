"""Explicit artificial-fixture CLI. Never reads runtime configuration."""
import argparse
from pathlib import Path
import json
import sys

from .contracts import (ACTIONS, CATEGORIES, VERSION, ContractError, canonical,
                        fingerprint, keys, reject, validate_input)
from .engine import fixture_frame, load_policy, read_json, run_case


def validate_corpus(corpus):
    keys(corpus,'schema_version synthetic description development contracts')
    if type(corpus['schema_version']) is not int or corpus['schema_version']!=1 or corpus['synthetic'] is not True:
        reject('artificial_fixture_required')
    if type(corpus['development']) is not list or type(corpus['contracts']) is not list:
        reject()
    seen=set()
    for group in ('development','contracts'):
        for case in corpus[group]:
            required='id group pair_id input expected fixture_frame' if group=='development' else 'id label input expected'
            keys(case,required)
            ident=case['id']
            if type(ident) is not str or not ident.isascii() or not ident.isalnum() or len(ident)>32 or ident in seen:
                reject('invalid_case_id')
            seen.add(ident)
            expected=case['expected']
            if type(expected) is not dict or not set(expected)<= {'error','chosen_action','categories','excluded_reason'}:
                reject()
            if 'chosen_action' in expected and expected['chosen_action'] not in ACTIONS:reject()
            if 'categories' in expected and (type(expected['categories']) is not list or not set(expected['categories'])<=set(CATEGORIES)):reject()
            try:
                snapshot=validate_input(case['input'])
            except ContractError as exc:
                if str(exc)!=expected.get('error'):raise
                continue
            if 'error' in expected:reject('expected_error_not_raised')
            if group=='development':fixture_frame(case['fixture_frame'],snapshot.context)
    return corpus


def matches(record,expected):
    return (record.chosen_action==expected.get('chosen_action')
        and ('categories' not in expected or record.categories==tuple(sorted(expected['categories'])))
        and ('excluded_reason' not in expected or expected['excluded_reason'] in {r[1] for r in record.excluded}))


def evaluate(corpus,policy):
    validate_corpus(corpus);rows=[]
    for group in ('development','contracts'):
        for case in corpus[group]:
            expected=case['expected']
            try:
                actual=run_case(case,'text',policy)
                passed='error' not in expected and matches(actual,expected)
                row={'id':case['id'],'group':group,'passed':passed,'text':actual.to_dict()}
                if group=='development':
                    diagnostic=run_case(case,'fixture',policy)
                    neutral=run_case(case,'neutral',policy)
                    row.update(pair_id=case['pair_id'],development_group=case['group'],
                        fixture=diagnostic.to_dict(),neutral=neutral.to_dict(),
                        text_categories_passed=actual.categories==tuple(sorted(expected['categories'])),
                        fixture_action_passed=diagnostic.chosen_action==expected['chosen_action'])
            except ContractError as exc:
                row={'id':case['id'],'group':group,'passed':str(exc)==expected.get('error'),'error':str(exc)}
            rows.append(row)
    development=[r for r in rows if r['group']=='development'];contracts=[r for r in rows if r['group']=='contracts']
    # Distinguish classification from action evaluation. No fabricated M7 model comparison.
    return {'schema_version':1,'prototype':VERSION,'fixture_hash':fingerprint(corpus),'policy_hash':policy.config_hash,
        'summary':{'development_passed':sum(r['passed'] for r in development),'development_count':len(development),
            'contracts_passed':sum(r['passed'] for r in contracts),'contract_count':len(contracts),
            'text_categories_passed':sum(r.get('text_categories_passed',False) for r in development),
            'fixture_action_passed':sum(r.get('fixture_action_passed',False) for r in development)},
        'passed':all(r['passed'] and r.get('fixture_action_passed',True) for r in rows),'cases':rows,
        'model_calls':0,'production_integrated':False,'evidence_scope':'artificial_development_and_contracts_only'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('validate','evaluate'))
    parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--rules',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    try:
        policy=load_policy(args.rules);corpus=validate_corpus(read_json(args.fixture))
        if args.operation=='validate':
            result={'valid':True,'fixture_hash':fingerprint(corpus),'policy_hash':policy.config_hash,
                    'development_count':len(corpus['development']),'contract_count':len(corpus['contracts'])}
        else:result=evaluate(corpus,policy)
        rendered=canonical(result)+'\n'
        if args.output:
            # Exclusive creation protects historical evidence, including concurrent CLI invocations.
            with args.output.open('x',encoding='utf-8',newline='\n') as output:
                output.write(rendered)
        else:sys.stdout.write(rendered)
        return 0 if result.get('passed',True) else 1
    except ContractError as exc:
        sys.stderr.write(canonical({'error':str(exc)})+'\n');return 2
    except FileExistsError:
        sys.stderr.write('{"error":"output_exists"}\n');return 2
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        sys.stderr.write('{"error":"invalid_fixture_or_io"}\n');return 2


if __name__=='__main__':
    raise SystemExit(main())
