#!/usr/bin/env python3
"""Fetch the pinned AOSP lpmake and its workspace runtime libraries."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile

from locked_download import download_locked

ROOT = Path('/rabbitr1')
SOURCE = Path(__file__).resolve().parents[1]
DEST = ROOT/'toolchains/android-lp'


def local(path):
    if path.resolve() != path or not path.is_relative_to(ROOT):
        raise ValueError('Path leaves the workspace or follows a symlink: '+str(path))
    return path


def verify(path, pin):
    local(path)
    if not path.is_file() or path.stat().st_size != pin['bytes']:
        raise ValueError('Preserving missing or mismatched file: '+str(path))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != pin['sha256']:
        raise ValueError('Preserving mismatched content: '+str(path))
    return data


manifest = json.loads(local(SOURCE/'android/lp-tools.json').read_text())
for pin in manifest['files']:
    relative = Path(pin['path'])
    if (relative.is_absolute() or len(relative.parts) != 2 or
            (relative != Path('bin/lpmake') and
             not (relative.parts[0] == 'lib64' and relative.suffix == '.so'))):
        raise ValueError('Unexpected LP tool path')
    target = local(DEST/relative)
    if target.exists():
        verify(target, pin)
        continue
    download = pin['download']
    cached = local(ROOT/download['cache'])
    if not cached.is_relative_to(ROOT/'downloads/android-lp'):
        raise ValueError('Unexpected download cache path')
    cached.parent.mkdir(parents=True, exist_ok=True)
    if not cached.exists():
        download_locked(download, cached)
    data = base64.b64decode(verify(cached, download), validate=True)
    if len(data) != pin['bytes'] or hashlib.sha256(data).hexdigest() != pin['sha256']:
        raise ValueError('Decoded LP tool differs from its pin')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.verified-', delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
        temporary.chmod(0o755 if relative == Path('bin/lpmake') else 0o644)
        target.hardlink_to(temporary)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    verify(target, pin)
print('Verified '+str(len(manifest['files']))+' pinned LP tool files in '+str(DEST))
