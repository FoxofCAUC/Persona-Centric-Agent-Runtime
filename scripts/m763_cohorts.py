"""Explicit supplier transition for isolated evaluation; never alters old runs.

Each supplier/model cohort owns all 300 slots. Reusing frozen case labels is
not permission to reuse old outputs, erase failures, or create retry directories.
"""
from hashlib import sha256
import json
from pathlib import Path

from persona_runtime.archive import fingerprint
from persona_runtime.config import ConfigStore
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_personas import safe,provider_identity
from scripts.m763_r04 import model_identity


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def save_once(path,value):
    if path.exists():
        if read(path)!=value:raise ValueError('evaluation_cohort_registration_conflict')
    else:
        with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def register_run(home,out,frozen,approved,supplier_change_from=None):
    root=safe(home/'exports/acceptance');out=safe(out,root)
    original_path=root/'m763-r06-real-run.json'
    registration={'output':str(out),'fixture_sha256':frozen['fixture_sha256'],'prepared_hash':fingerprint(frozen)}
    if not original_path.exists():
        if supplier_change_from is not None:raise ValueError('supplier_parent_missing')
        save_once(original_path,registration);return
    original=read(original_path)
    if original==registration:
        if supplier_change_from is not None:raise ValueError('supplier_transition_requires_distinct_run')
        return
    if supplier_change_from is None:raise ValueError('Resume registered run; directory bypass forbidden')
    parent=safe(supplier_change_from,root)
    if str(parent)!=original['output'] or out==parent:raise ValueError('supplier_parent_not_registered')
    parent_prepared=read(parent/'prepared.json');parent_freeze=read(parent/'execution-freeze.json')
    fixture=sha256((out/'independent-v1.json').read_bytes()).hexdigest()
    if (fingerprint(parent_prepared)!=original['prepared_hash'] or
        fixture!=original['fixture_sha256'] or fixture!=frozen['fixture_sha256'] or
        sha256((parent/'independent-v1.json').read_bytes()).hexdigest()!=fixture or
        parent_freeze['fixture_hash']!=fixture):raise ValueError('supplier_transition_fixture_changed')
    previous=ConfigStore(RuntimePaths(Path(parent_prepared['cases'][0]['runtime_roots']['A']))).load()
    models={k:model_identity(approved[k]) for k in ('model','character_selector')}
    old_models={k:model_identity(previous[k]) for k in models}
    connections={k:provider_identity(approved[k]) for k in models}
    old_connections={k:provider_identity(previous[k]) for k in models}
    if old_models!=parent_freeze['models']:raise ValueError('supplier_parent_model_changed')
    if connections==old_connections:raise ValueError('supplier_transition_requires_changed_provider_or_model')
    # Credential/parameter changes cannot be used to create a second run for
    # the same provider/model combination after viewing its outputs.
    cohort_id=fingerprint(connections)
    folder=root/'m763-model-cohorts';folder.mkdir(exist_ok=True)
    proof={'version':'supplier-cohort-v1','cohort_id':cohort_id,'registration':registration,
        'parent_registration_hash':fingerprint(original),'parent_output':str(parent),
        'parent_execution_hash':fingerprint(parent_freeze),'models':models,'connections':connections,
        'old_outputs_reused':False,'frozen_labels_unchanged':True,
        'reason':'Explicit user supplier switch; independent accounting for all three arms'}
    save_once(safe(folder/(cohort_id+'.json'),root),proof)
    save_once(out/'supplier-cohort.json',proof)
