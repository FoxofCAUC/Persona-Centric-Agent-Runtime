"""Explicit M8.E tools. No model dispatch or production initialization on import."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.config import ConfigStore, atomic_json
from persona_runtime.paths import RuntimePaths, DEFAULT_HOME
from persona_runtime.lifecycle import ServiceController
from persona_runtime.models import identifier
from persona_runtime.deployment import onebot_config
from persona_runtime.character_embedding import private_directory


EVIDENCE = DEFAULT_HOME/'exports/acceptance/M8.E-20261108'
ISOLATED = DEFAULT_HOME/'tmp/pcar-m8e-acceptance/e0'


def probe(resume=False):
    """A console-owned capability probe, not a paid acceptance position."""
    if ISOLATED.exists() and not resume:
        raise RuntimeError('E0 root already exists: inspect existing effects, never replace it')
    formal = ServiceController(RuntimePaths(DEFAULT_HOME))
    if formal.has_resident_processes():
        raise RuntimeError('Formal components must be stopped before E0')
    test = json.loads((DEFAULT_HOME/'private/qq-testing.json').read_text(encoding='utf-8'))
    if test['version'] != 2 or test['state'] != 'inactive':
        raise RuntimeError('Expected inactive dedicated test Bot policy')
    paths = RuntimePaths(ISOLATED)
    private_directory(DEFAULT_HOME, ISOLATED)
    cfg_store = ConfigStore(paths)
    cfg = cfg_store.initialize()
    cfg.update(bot_qq=test['test_bot'], owner_qq=test['test_peer'],
        service_port=18765, napcat_port=16399, astrbot_port=16285, onebot_port=16299)
    atomic_json(cfg_store.file, cfg)
    source = DEFAULT_HOME/'components/napcat'
    target = paths.home/'components/napcat'
    target.mkdir(parents=True, exist_ok=True)
    for name in ('native', 'node_modules', 'static', 'worker'):
        if not (target/name).exists(): shutil.copytree(source/name, target/name)
    for item in source.iterdir():
        if item.is_file() and item.suffix in ('.js','.mjs','.dll','.exe','.json'):
            if not (target/item.name).exists():shutil.copy2(item, target/item.name)
    private_directory(paths.home, target/'config')
    atomic_json(target/'config/webui.json', {'host':'127.0.0.1','port':cfg['napcat_port'],
        'token':identifier(), 'autoLoginAccount':'', 'disableWebUI':False})
    atomic_json(target/'config/napcat.json', {'fileLog':False,'consoleLog':False,
        'fileLogLevel':'error','consoleLogLevel':'error','packetBackend':'auto'})
    atomic_json(target/'config'/f"onebot11_{cfg['bot_qq']}.json", onebot_config(cfg,identifier()))
    controller = ServiceController(paths)
    if resume: (EVIDENCE/'E0-finish.request').unlink(missing_ok=True)
    controller.start_console()
    try:
        controller.start('napcat')
        record = controller.record('napcat')
        root = paths.home/'runtime/qq-session'/record['session_token']
        deadline = time.monotonic()+25
        while not (root/'capability.json').exists() and time.monotonic()<deadline:
            time.sleep(.2)
        cap = json.loads((root/'capability.json').read_text(encoding='utf-8'))
        safe = {k:cap[k] for k in ('offline','msfStatus','pid','at')}
        atomic_json(EVIDENCE/'E0-capability.json', safe)
        print(json.dumps(safe), flush=True)
        # The active lease remains owned by this probe. No chat bridge is started.
        while not (EVIDENCE/'E0-finish.request').exists():
            time.sleep(.5)
    finally:
        controller.end_console()
        # The existing host lifetime job reclaims exactly this probe's children.


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action', choices=('probe',))
    parser.add_argument('--resume', action='store_true')
    args=parser.parse_args()
    if args.action=='probe': probe(args.resume)
