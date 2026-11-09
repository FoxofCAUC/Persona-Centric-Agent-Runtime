"""Instance-scoped local control of the QQ session, never a process-name logout."""
import json
import time
import hashlib
import urllib.request
import threading
from uuid import UUID

from .config import atomic_json
from .models import identifier, RuntimeErrorBase

# Local WebUI credentials remain in RAM. Re-authenticating every heartbeat
# would consume the vendor's per-IP login limit, including the user's browser.
_health_credentials={}
_health_auth_lock=threading.Lock()


def session_request(paths, record, action, *, timeout=8):
    token = record.get('session_token')
    if not token or action not in ('capability', 'logout'):
        raise RuntimeErrorBase('qq_session_adapter_unavailable')
    try:
        if str(UUID(token))!=token:raise ValueError
    except (ValueError,TypeError,AttributeError):raise RuntimeErrorBase('qq_session_identity_mismatch') from None
    root = paths.home / 'runtime/qq-session' / token
    request_id = identifier()
    atomic_json(root/'request.json', {'id': request_id, 'token': token,
        'instance': record['instance_id'], 'bot': record['bot'], 'action': action, 'at': time.time()})
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            result = json.loads((root/(request_id+'.json')).read_text(encoding='utf-8'))
            if (result['id'] != request_id or result['instance'] != record['instance_id'] or
                    result['bot'] != record['bot'] or result['pid'] != record['qq_listener']['ProcessId']):
                raise RuntimeErrorBase('qq_session_identity_mismatch')
            return result
        except FileNotFoundError:
            time.sleep(.1)
    raise RuntimeErrorBase('qq_session_adapter_timeout')


def web_call(paths, action, *, timeout=2, credential=None):
    """Caller verifies the retained project QQ identity before opening this port."""
    from .config import ConfigStore
    cfg=ConfigStore(paths).load()
    payload={}
    if credential is None:
        web=json.loads((paths.home/'components/napcat/config/webui.json').read_text(encoding='utf-8-sig'))
        payload={'hash':hashlib.sha256((web['token']+'.napcat').encode()).hexdigest()}
    request=urllib.request.Request(f"http://127.0.0.1:{cfg['napcat_port']}/api"+action,
        data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',
        **({'Authorization':'Bearer '+credential} if credential else {})})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=timeout) as response:
        value=json.load(response)
    if value.get('code')!=0:
        reason='qq_web_credential_expired' if value.get('message')=='Unauthorized' else 'qq_login_status_unavailable'
        raise RuntimeErrorBase(reason)
    return value['data']


def logout_verified(paths,record,*,timeout=10):
    from .lifecycle import listener,same_process
    from .config import ConfigStore
    deadline=time.monotonic()+timeout
    identity=record.get('qq_listener')
    actual=listener(ConfigStore(paths).load()['napcat_port'])
    if not identity or not same_process(identity) or not actual or any(
            identity.get(k)!=actual.get(k) for k in ('ProcessId','Created','ExecutablePath')):
        raise RuntimeErrorBase('qq_session_identity_mismatch')
    remaining=lambda: max(.1,min(2,deadline-time.monotonic()))
    credential=web_call(paths,'/auth/login',timeout=remaining())['Credential']
    info=web_call(paths,'/QQLogin/GetQQLoginInfo',timeout=remaining(),credential=credential)
    if str(info.get('uin'))!=record['bot']:raise RuntimeErrorBase('qq_session_account_mismatch')
    result=session_request(paths,record,'logout',timeout=max(.1,deadline-time.monotonic()-1))
    while time.monotonic()<deadline:
        status=web_call(paths,'/QQLogin/CheckLoginStatus',timeout=remaining(),credential=credential)
        if result.get('invoked') and status.get('isLogin') is False and status.get('coreReady') is False:
            return {'logout_confirmed':True,'logout_reason':result.get('method'),'logout_at':time.time()}
        time.sleep(.1)
    return {'logout_confirmed':False,'logout_reason':result.get('reason','remote_logout_unconfirmed')}


def login_status_verified(paths,record,*,timeout=2):
    """Read the owned native session; cached OneBot identity is not login proof."""
    from .lifecycle import listener,same_process
    from .config import ConfigStore
    cfg=ConfigStore(paths).load();identity=record.get('qq_listener')
    if record.get('instance_id')!=cfg['instance_id'] or record.get('bot')!=cfg['bot_qq']:
        raise RuntimeErrorBase('qq_session_identity_mismatch')
    actual=listener(cfg['napcat_port'])
    if not identity or not same_process(identity) or not actual or any(
            identity.get(k)!=actual.get(k) for k in ('ProcessId','Created','ExecutablePath')):
        raise RuntimeErrorBase('qq_session_identity_mismatch')
    deadline=time.monotonic()+timeout
    def remaining():
        if time.monotonic()>=deadline:raise RuntimeErrorBase('qq_login_status_unavailable')
        return max(.1,deadline-time.monotonic())
    key=(str(paths.home.resolve()),record['instance_id'],record.get('session_token'),
         identity['ProcessId'],identity['Created'],record['bot'])
    with _health_auth_lock:
        cached=_health_credentials.get(key)
        if cached and 0<=time.monotonic()-cached[1]<3300:credential=cached[0]
        else:
            credential=web_call(paths,'/auth/login',timeout=remaining())['Credential']
            _health_credentials.clear()
            _health_credentials[key]=credential,time.monotonic()
    try:status=web_call(paths,'/QQLogin/CheckLoginStatus',timeout=remaining(),credential=credential)
    except RuntimeErrorBase as error:
        if str(error)=='qq_web_credential_expired':
            with _health_auth_lock:_health_credentials.pop(key,None)
        raise
    if type(status.get('isLogin')) is not bool:raise RuntimeErrorBase('qq_login_status_unavailable')
    matched=None
    if status['isLogin']:
        info=web_call(paths,'/QQLogin/GetQQLoginInfo',timeout=remaining(),credential=credential)
        matched=str(info.get('uin'))==cfg['bot_qq']
    logged_in=status['isLogin'] and matched is True
    reason=('account_mismatch' if matched is False else 'session_expired'
        if 'KickedOffLine' in str(status.get('loginError','')) else 'login_required')
    return {'account_logged_in':logged_in,'account_matches':matched,
        'login_required':not logged_in,'login_reason':None if logged_in else reason,
        'core_ready':status.get('coreReady') if type(status.get('coreReady')) is bool else None}
