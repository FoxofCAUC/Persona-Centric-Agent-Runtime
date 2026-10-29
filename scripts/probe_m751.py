"""One bounded real-local-model smoke batch, with a fresh offline restart worker."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from persona_runtime.character import CharacterService, empty_character
from persona_runtime.character_embedding import CharacterEmbeddingProvider, safe_path, file_hash
from persona_runtime.character_semantic_index import CharacterSemanticIndex
from persona_runtime.models import generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.storage import Store


def peak_memory():
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
            'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage',
            'PagefileUsage','PeakPagefileUsage')]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    value = Counters(); value.cb = ctypes.sizeof(value)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.PeakWorkingSetSize


def fixtures():
    data = empty_character()
    rows = [
        ('reading', '安静的夜晚', '角色喜欢在下班后沏一杯热茶，坐在安静的书店阅读小说。'),
        ('hiking', '山林周末', '角色休息日喜欢去山间徒步，走林间小路，看山顶日出，亲近自然。'),
        ('train', '交通知识', '虚构城市的列车在中央车站停靠，时刻表按工作日和周末分别编制。'),
        ('long', '长背景', '这是虚构书店的日常背景，书架每天按照编号整理。' * 65 +
         '特别设定：蓝鲸灯塔的守夜人叫阿沐，灯塔地下存放着一枚琥珀色的机械钥匙。'),
    ]
    for item, title, text in rows:
        data['lorebook'].append({'id':item,'title':title,'category':'背景','keywords':[title],
            'text':text,'priority':0,'enabled':True,'visibility':'conversation','dependencies':[]})
    data['lorebook'].append(data['lorebook'][0] | {'id':'private','visibility':'creator_only'})
    data['lorebook'].append(data['lorebook'][1] | {'id':'disabled','enabled':False})
    data['examples'] = [{'id':'greeting','scenario':'日常问候','keywords':['你好'],'text':'嗨，今天过得怎么样','enabled':True}]
    return data


def worker(paths, model, evidence, mode):
    sandbox = RuntimePaths(evidence / 'sandbox')
    store = Store(sandbox)
    if mode == 'initial':
        store.initialize()
        service = RuntimeService(store)
        pid = service.create_persona(generic_persona())['id']; service.use_persona(pid)
        characters = CharacterService(store)
        version = characters.update(pid, fixtures(), characters.current(pid)['id'], identifier())
        state = {'persona_id':pid,'version_id':version['id']}
        (evidence/'fixture-state.json').write_text(json.dumps(state), encoding='utf-8')
    else:
        state = json.loads((evidence/'fixture-state.json').read_text(encoding='utf-8'))
    before = file_hash(store.path)
    provider = CharacterEmbeddingProvider(paths, model)
    start = time.perf_counter()
    diagnostic = provider.diagnose()
    assert diagnostic['status'] == 'ready', diagnostic
    cold = time.perf_counter() - start
    index = CharacterSemanticIndex(store, provider)
    start = time.perf_counter()
    built = index.build(state['persona_id'], state['version_id'])
    build_seconds = time.perf_counter() - start
    tests = [
        ('reading', ['忙完一天，我想找个清静地方喝点茶看看故事书']),
        ('hiking', ['周末想出去爬山，沿着树林里的小道走走看日出']),
        ('long', ['蓝鲸灯塔守夜人的名字，以及地下的琥珀机械钥匙']),
        ('hiking', ['先打个招呼', '休息时想去山间徒步，感受自然']),
    ]
    results = []
    timings = []
    for expected, members in tests:
        start = time.perf_counter()
        result = index.query(state['persona_id'], state['version_id'], members)
        timings.append(time.perf_counter() - start)
        assert result['candidates'][0]['item_id'] == expected, result
        assert not any(r['item_id'] in ('private','disabled') for r in result['candidates'])
        if len(members)>1:
            assert result['candidates'][0]['member_index']==1
        results.append({'expected':expected,'result':result})
    # Full coverage proof from the actual pinned tokenizer, including whitespace and tail.
    long_text = fixtures()['lorebook'][3]['text']
    chunks = provider.chunks(long_text)
    covered = 0
    for chunk in chunks:
        assert chunk['start'] <= covered
        assert chunk['token_end'] - chunk['token_start'] <= 384
        covered = max(covered, chunk['end'])
    assert covered == len(long_text) and len(chunks)>1
    embed_times, cache_times = [], []
    spec=index._spec(index._snapshot(state['persona_id'],state['version_id']))
    from persona_runtime.character_semantic_index import sources
    source=sources(index._snapshot(state['persona_id'],state['version_id']))
    for _ in range(5):
        start=time.perf_counter(); provider.embed_query(['安静地喝热茶看书']); embed_times.append(time.perf_counter()-start)
        start=time.perf_counter(); index._read(spec,source); cache_times.append(time.perf_counter()-start)
    assert file_hash(store.path) == before
    return {'mode':mode,'pid':os.getpid(),'model':provider.identity(),'cold_load_seconds':cold,
            'build_seconds':build_seconds,'build':built,'query_seconds':timings,
            'warm_embedding_seconds':embed_times,'cache_read_seconds':cache_times,
            'peak_working_set_bytes':peak_memory(),'index_bytes':index._path(spec).stat().st_size,
            'checks':results,'long_text_characters':len(long_text),'long_text_chunks':len(chunks),
            'authority_unchanged':True,'paid_calls':0,'qq_sends':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-directory',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--worker',choices=['initial','restart'])
    args=parser.parse_args()
    paths=RuntimePaths.from_environment()
    safe_path(paths.home,args.evidence);args.evidence.mkdir(parents=True,exist_ok=True)
    if args.worker:
        # Test process only: block DNS and connect, rather than changing system networking.
        with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')) as connect, \
             patch('socket.getaddrinfo',side_effect=AssertionError('DNS forbidden')) as dns:
            report=worker(paths,args.model_directory,args.evidence,args.worker)
            assert connect.call_count==0 and dns.call_count==0
            report['network_attempts']=0
        (args.evidence/(args.worker+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return
    if (args.evidence/'fixture-state.json').exists():
        raise RuntimeError('Existing smoke evidence: choose a new run directory; never overwrite old results')
    for mode in ('initial','restart'):
        result=subprocess.run([sys.executable,'-B',__file__,'--model-directory',str(args.model_directory),
            '--evidence',str(args.evidence),'--worker',mode],capture_output=True,text=True,encoding='utf-8')
        (args.evidence/(mode+'-process.txt')).write_text(result.stdout+result.stderr,encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'{mode} failed; inspect preserved evidence')
    first=json.loads((args.evidence/'initial.json').read_text(encoding='utf-8'))
    restart=json.loads((args.evidence/'restart.json').read_text(encoding='utf-8'))
    assert first['pid'] != restart['pid'] and restart['build']['status']=='hit'
    summary={'status':'local_smoke_passed_not_stage_acceptance',
             'initial_cold_seconds':first['cold_load_seconds'],
             'restart_cold_seconds':restart['cold_load_seconds'],
             'warm_embedding_median_ms':statistics.median(first['warm_embedding_seconds'])*1000,
             'cache_read_median_ms':statistics.median(first['cache_read_seconds'])*1000,
             'query_median_ms':statistics.median(first['query_seconds'])*1000,
             'peak_memory_mib':max(first['peak_working_set_bytes'],restart['peak_working_set_bytes'])/1024**2,
             'index_bytes':first['index_bytes'],'network_attempts':0,'paid_calls':0,'qq_sends':0}
    (args.evidence/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary))


if __name__=='__main__':
    main()
