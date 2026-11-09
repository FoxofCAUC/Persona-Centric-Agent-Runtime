"""Authenticated application controls. These never launch a detached host."""
import json
from .config import ConfigStore,atomic_json
from .instance import InstanceLock
from .lifecycle import require_console
from .models import RuntimeErrorBase,identifier
from .organization import digest


def current(paths):
    result={'instance_id':ConfigStore(paths).load()['instance_id'],'settings':ConfigStore(paths).load()['runtime']}
    from .lifecycle import read_console
    console=read_console(paths) or {}
    from .instance import process_identity_alive
    result['display_state']=console.get('display_state','visible');result['owner_running']=console.get('state')=='active' and process_identity_alive(console.get('process'))
    try:
        progress=json.loads((paths.home/'runtime/application-closing.json').read_text(encoding='utf-8'))
        result['exit_progress']=progress if progress.get('token')==console.get('token') else None
    except (OSError,ValueError):result['exit_progress']=None
    for name,file in [('health','application.json'),('disk','disk-health.json'),('last_exit','last-exit.json')]:
        try:result[name]=json.loads((paths.home/'runtime'/file).read_text(encoding='utf-8'))
        except (OSError,ValueError):result[name]=None
    if console.get('previous_exit_unconfirmed'):
        result['last_exit']=(result.get('last_exit') or {})|{'logout_confirmed':False,'logout_reason':'previous_exit_receipt_unconfirmed'}
    return result


def update(paths,components,expected_version,expected_instance,operation_id):
    lock=InstanceLock(paths.home/'runtime/config-write.lock')
    try:
        config=ConfigStore(paths);cfg=config.load();root=paths.home/'runtime/control-operations'
        if not isinstance(operation_id,str) or not operation_id or len(operation_id)>128:raise RuntimeErrorBase('operation_id_required')
        key=digest(operation_id);file=root/(key+'.json');request=digest([components,expected_version,expected_instance])
        if file.exists():
            old=json.loads(file.read_text())
            if old['request_hash']!=request:raise RuntimeErrorBase('operation_conflict')
            if not old.get('committed',True):
                if cfg['runtime']!=old['result']:
                    if cfg['runtime']['revision']!=old['before_revision']:raise RuntimeErrorBase('runtime_operation_requires_audit')
                    cfg['runtime']=old['result'];atomic_json(config.file,cfg)
                atomic_json(file,old|{'committed':True})
            return old['result']
        if cfg['instance_id']!=expected_instance or cfg['runtime']['revision']!=expected_version:raise RuntimeErrorBase('version_conflict')
        if not isinstance(components,dict) or not set(components)<=set(cfg['runtime']['components']) or any(type(v) is not bool for v in components.values()):
            raise RuntimeErrorBase('runtime_components_invalid')
        before=cfg['runtime']['revision']
        cfg['runtime']['components'].update(components);cfg['runtime']['revision']+=1
        ConfigStore.validate(cfg);result=cfg['runtime']
        entry={'request_hash':request,'result':result,'before_revision':before,'committed':False}
        atomic_json(file,entry);atomic_json(config.file,cfg);atomic_json(file,entry|{'committed':True})
        return result
    finally:lock.close()


def exit_application(paths,expected_instance,operation_id):
    cfg=ConfigStore(paths).load();lease=require_console(paths)
    if cfg['instance_id']!=expected_instance:raise RuntimeErrorBase('instance_identity_changed')
    if not isinstance(operation_id,str) or not operation_id:raise RuntimeErrorBase('operation_id_required')
    atomic_json(paths.home/'runtime/wakeup.json',{'id':operation_id,'token':lease['token'],'action':'exit'})
    return {'accepted':True,'owner':'workbench'}
