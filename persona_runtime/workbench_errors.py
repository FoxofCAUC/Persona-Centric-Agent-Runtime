"""Visible pythonw failures with metadata-only diagnostics in the runtime home."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import traceback

from .config import atomic_json
from .models import identifier, timestamp


def show_error(message):
    """Works before Tk exists and does not depend on pythonw stderr."""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    box = user32.MessageBoxW
    box.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    box.restype = ctypes.c_int
    return bool(box(None, message, 'Persona-Centric Agent Runtime (PCAR)', 0x10010))


def report_failure(paths, error, stage):
    # Never serialize the exception message, command line, locals, configuration,
    # or traceback source text: any of those may contain user data or credentials.
    report = {
        'at': timestamp(), 'event': 'workbench_failure', 'stage': stage,
        'error_type': type(error).__name__,
        'frames': [{'file': Path(frame.f_code.co_filename).name, 'line': line}
                   for frame, line in traceback.walk_tb(error.__traceback__)],
    }
    log_path = paths.home / 'logs' / ('workbench-error-' + identifier() + '.json')
    saved = False
    try:
        atomic_json(log_path, report)
        saved = True
    except Exception:
        # An unavailable/full D drive must still produce a visible failure; do
        # not silently redirect diagnostics to the system drive.
        pass
    if stage == 'instance_lock':
        hint = '无法取得工作台实例锁，请先检查是否已有 PCAR 窗口；也请检查数据目录是否可访问。'
    elif stage == 'cleanup_legacy':
        hint = '遗留服务清理未完成。请检查管理员授权及强停报告后再启动。'
    elif stage in {'console_cleanup', 'lock_release'}:
        hint = '工作台退出清理未完成，请检查相关服务状态。'
    else:
        hint = '工作台未能正常打开或运行，请根据诊断记录排查。'
    message = ('PCAR 工作台发生错误（' + type(error).__name__ + '）。\n\n' + hint + '\n\n'
               + ('诊断日志：' if saved else '诊断日志未能写入，请检查数据盘和目录权限：') + str(log_path))
    try:
        shown = show_error(message)
    except Exception:
        shown = False
    if not shown and saved:
        report['notification_failed'] = True
        try:
            atomic_json(log_path, report)
        except Exception:
            pass
    return {'path': str(log_path), 'saved': saved, 'shown': shown}
