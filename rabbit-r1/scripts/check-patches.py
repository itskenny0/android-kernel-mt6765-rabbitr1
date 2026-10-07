#!/usr/bin/env python3
"""Apply the series to pristine affected files and compare with the working tree."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path('/rabbitr1')
os.environ.update(TMPDIR=str(ROOT/'.tmp'), GIT_CONFIG_GLOBAL=str(ROOT/'.gitconfig'), GIT_CONFIG_NOSYSTEM='1')
src = ROOT/'src/mainline'
commit = json.loads((ROOT/'sources.lock.json').read_text())['repositories']['mainline']['commit']
patches = sorted((ROOT/'patches/mainline').glob('*.patch'))
paths = sorted({path for patch in patches for path in re.findall(r'^\+\+\+ b/(.*)$',patch.read_text(),re.M)})
with tempfile.TemporaryDirectory(prefix='patch-check-',dir=ROOT/'.tmp') as directory:
    scratch = Path(directory)
    subprocess.run(['git','init','-q',str(scratch)],check=True)
    for path in paths:
        dest = scratch/path
        if not dest.resolve().is_relative_to(scratch):
            raise SystemExit('Patch escapes scratch directory')
        original = subprocess.run(['git','-C',str(src),'show',f'{commit}:{path}'],capture_output=True)
        if original.returncode == 0:
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(original.stdout)
        elif subprocess.run(['git','-C',str(src),'cat-file','-e',commit],capture_output=True).returncode:
            raise SystemExit('Missing source base')
    for patch in patches:
        subprocess.run(['git','-C',str(scratch),'apply','--check',str(patch)],check=True)
        subprocess.run(['git','-C',str(scratch),'apply',str(patch)],check=True)
        print('PASS: apply',patch.name)
    for path in paths:
        if (scratch/path).read_bytes() != (src/path).read_bytes():
            raise SystemExit('Working tree differs from patch series: '+path)
    print(f'PASS: {len(paths)} patched files match the working tree')
