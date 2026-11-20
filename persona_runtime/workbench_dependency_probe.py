"""Standalone, read-only Windows dependency check for the selected interpreter."""
import base64
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import sys


def verified_packages(expected,overlay):
    if overlay:sys.path.insert(0,overlay)
    for name,version in expected.items():
        package=distribution(name)
        if package.version!=version or not package.files:return False
        environment=Path(package.locate_file("")).resolve().parent.parent
        for member in package.files:
            if member.hash is None:continue
            path=Path(package.locate_file(member)).resolve()
            if not path.is_relative_to(environment) or not path.is_file():return False
            if member.size is not None and path.stat().st_size!=member.size:return False
            with path.open("rb") as stream:
                hashed=hashlib.file_digest(stream,member.hash.mode).digest()
            if base64.urlsafe_b64encode(hashed).decode().rstrip('=')!=member.hash.value:return False
    return True


if __name__=="__main__":
    try:ready=verified_packages(json.loads(sys.argv[1]),sys.argv[2])
    except Exception:ready=False
    if not ready:sys.exit(1)
    try:
        import webview
        import clr
        from webview.platforms import winforms
        available=callable(webview.create_window) and winforms.renderer=='edgechromium'
    except Exception:available=False
    sys.exit(0 if available else 2)
