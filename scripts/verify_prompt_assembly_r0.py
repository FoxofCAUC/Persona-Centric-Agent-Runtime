"""Targeted offline acceptance with loopback-only sockets and frozen source oracle.

The oracle was saved before assembly edits. It is integrity checked, never refreshed.
Only synthetic tests are loaded; formal files are hashed, never used as fixtures.
"""
from contextlib import ExitStack
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
HOME=Path('D:/Persona-Centric Agent Runtime')
OUT=HOME/'exports/acceptance/M7.6.3-R0-prompt-baseline'
os.environ['TEMP']=os.environ['TMP']=str(HOME/'tmp')
os.environ['PYTHONDONTWRITEBYTECODE']='1'

SUITES=[
    'tests.test_prompt_assembly','tests.test_prompt_assembly_desktop',
    'tests.test_m72.ContextTests','tests.test_m72_desktop','tests.test_m72_acceptance',
    'tests.test_m762','tests.test_m762_interfaces','tests.test_dialogue_voice',
    'tests.test_reply_protocol','tests.test_semantic_bubbles','tests.test_semantic_bubbles_desktop',
    'tests.test_bubble_identity_defaults.BubbleIdentityDefaultsTests','tests.test_m763_context_audit',
]


def formal_hashes():
    paths=[HOME/'config/workbench.json', HOME/'data/database/runtime.sqlite3',
           HOME/'data/database/runtime.sqlite3-wal',HOME/'private/personas/registry.json']
    # Frozen review packages stay untouched. Hash evidence, do not parse its contents.
    for name in ('M7.6.3-voice-v2/independent-v2',):
        folder=HOME/'exports/acceptance'/name
        paths.extend(p for p in folder.rglob('*') if p.is_file())
    return {str(p.relative_to(HOME)):hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--test',action='append',dest='tests')
    parser.add_argument('--label',default='')
    args=parser.parse_args();suites=args.tests or SUITES
    if args.label and not args.label.replace('-','').isalnum():parser.error('invalid label')
    prefix=args.label+'-' if args.label else ''
    OUT.mkdir(parents=True,exist_ok=True)
    before=formal_hashes();oracle_path=OUT/'original-context_m4.py'
    integrity=json.loads((OUT/'baseline-integrity.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(oracle_path.read_bytes()).hexdigest()==integrity['original_context_sha256']
    assert hashlib.sha256((ROOT/'tests/fixtures/prompt-baseline-r0.json').read_bytes()).hexdigest()==integrity['baseline_sha256']
    spec=importlib.util.spec_from_file_location('persona_runtime._r0_original',oracle_path)
    oracle=importlib.util.module_from_spec(spec);spec.loader.exec_module(oracle)
    from persona_runtime import context_m4
    from persona_runtime.archive import fingerprint
    from persona_runtime.provider_compat import build_request
    original_new=context_m4.build_context;comparisons=[];network={'loopback_connections':0,'external_attempts':0}
    def outcome(call,*args,**kw):
        try:
            messages,details=call(*args,**kw)
            return {'messages':messages,'details':{k:v for k,v in details.items() if k!='prompt_assembly'}}
        except (ValueError,TypeError,KeyError) as exc:
            return {'error_type':type(exc).__module__+'.'+type(exc).__name__,'error_code':str(exc)}
    def compare(*args,**kw):
        expected=outcome(oracle.build_context,*args,**kw)
        actual=outcome(original_new,*args,**kw)
        equal=json.dumps(expected,ensure_ascii=False)==json.dumps(actual,ensure_ascii=False)
        turn=args[2]
        row={'ordinal':len(comparisons)+1,'turn_id':turn['id'],'channel':turn['channel'],
             'reply_mode':turn.get('reply_mode_snapshot'),'equal':equal,
             'expected_hash':fingerprint(expected),'actual_hash':fingerprint(actual),'original':expected}
        comparisons.append(row)
        if not equal:raise AssertionError('R0 original request mismatch: '+str(row['ordinal']))
        # Keep actual diagnostic and original exception object/traceback behavior.
        return original_new(*args,**kw)
    real_connect=socket.socket.connect;real_connect_ex=socket.socket.connect_ex
    def guard(address):
        host=address[0]
        try:allowed=ipaddress.ip_address(host).is_loopback
        except ValueError:allowed=host=='localhost'
        if not allowed:
            network['external_attempts']+=1;raise AssertionError('R0 external network forbidden')
        network['loopback_connections']+=1
    def connect(sock,address):guard(address);return real_connect(sock,address)
    def connect_ex(sock,address):guard(address);return real_connect_ex(sock,address)
    with ExitStack() as stack:
        stack.enter_context(patch('socket.socket.connect',connect))
        stack.enter_context(patch('socket.socket.connect_ex',connect_ex))
        stack.enter_context(patch.object(context_m4,'build_context',compare))
        with (OUT/(prefix+'targeted-tests.log')).open('w',encoding='utf-8') as log:
            result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(suites))
    after=formal_hashes()
    summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
             'equivalence_checks':len(comparisons),'equivalence_passed':all(r['equal'] for r in comparisons),
             'network':network,'formal_files_unchanged':before==after,'real_model_calls':0,'qq_sends':0,
             'suites':suites,'passed':result.wasSuccessful() and before==after and network['external_attempts']==0}
    (OUT/(prefix+'integration-equivalence.json')).write_text(json.dumps(comparisons,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/(prefix+'protected-file-hashes.json')).write_text(json.dumps({'before':before,'after':after},ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/(prefix+'targeted-results.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    if not summary['passed']:sys.exit(1)


if __name__=='__main__':main()
