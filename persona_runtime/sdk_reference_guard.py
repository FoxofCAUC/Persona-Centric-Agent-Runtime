"""Project-process-only SDK adapter guard; never edit an installed package."""
import functools
import inspect
from .models import RuntimeErrorBase


def install(adapter_class=None):
    if adapter_class is None:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        adapter_class = AiocqhttpAdapter
    original = adapter_class._convert_handle_message_event
    if getattr(original, '_pcar_local_references', False): return
    parameters = inspect.signature(original).parameters
    if tuple(parameters) != ('self', 'event', 'get_reply') or not inspect.iscoroutinefunction(original):
        raise RuntimeErrorBase('sdk_reference_guard_incompatible')

    @functools.wraps(original)
    async def guarded(self, event, get_reply=True):
        return await original(self, event, get_reply=False)
    guarded._pcar_local_references = True
    adapter_class._convert_handle_message_event = guarded
