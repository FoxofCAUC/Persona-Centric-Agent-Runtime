"""The owner-only text transport shared by the two OneBot clients."""
import asyncio

from .models import RuntimeErrorBase


async def probe_client(client, cfg):
    backend = cfg.get('qq_backend', 'napcat')
    routing = {'self_id': int(cfg['bot_qq'])} if backend == 'snowluma' else {}
    login = await asyncio.wait_for(client.get_login_info(**routing), 3)
    status = await asyncio.wait_for(client.get_status(**routing), 3)
    account_matches = str(login.get('user_id', '')) == cfg['bot_qq']
    backend_matches = None
    if backend == 'snowluma':
        version = await asyncio.wait_for(client.get_version_info(**routing), 3)
        backend_matches = str(version.get('app_name', '')).strip().casefold() == 'snowluma'
    verified = account_matches and backend_matches is not False
    online = status.get('online') is True if backend == 'snowluma' else bool(status.get('online'))
    return {'verified': verified, 'online': verified and online,
            'connected': True, 'backend': backend, 'backend_matches': backend_matches,
            'account_matches': account_matches}


async def require_snowluma(client, cfg):
    if cfg.get('runtime', {}).get('components', {}).get('napcat') is False:
        raise RuntimeErrorBase('qq_connection_disabled')
    proof = await probe_client(client, cfg)
    if not proof['verified'] or not proof['online']:
        raise RuntimeErrorBase('qq_connection_unverified')


async def send_private_text(client, cfg, text):
    message = [{'type': 'text', 'data': {'text': text}}]
    if cfg.get('qq_backend', 'napcat') == 'snowluma':
        await require_snowluma(client, cfg)
        return await client.send_msg(message_type='private', user_id=int(cfg['owner_qq']),
                                     self_id=int(cfg['bot_qq']), message=message)
    return await client.send_private_msg(user_id=int(cfg['owner_qq']), message=message)
