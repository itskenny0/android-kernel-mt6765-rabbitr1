#!/usr/bin/env bash
# Compile the inactive driver without changing the shipping configuration.
set -euo pipefail
source /rabbitr1/scripts/env.sh
r1_build_time "$R1_ROOT/src/mainline"
python3 - "$(dirname "$(realpath "$0")")/.." "$JOBS" <<'PY'
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys

root = Path('/rabbitr1')
src = root/'src/mainline'
out = root/'out/powervr-check'
tooling = Path(sys.argv[1]).resolve()
for path in (src, out, tooling):
    if not path.resolve().is_relative_to(root) or (path == out and path.resolve() != out):
        raise SystemExit(f'Path must stay under /rabbitr1 without an output symlink: {path}')
manifest_path = tooling/'tests/gpu/powervr-7.2.9.json'
manifest = json.loads(manifest_path.read_text())

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify_sources():
    for item in manifest['files']:
        path = src/item['path']
        if not path.resolve().is_relative_to(src.resolve()):
            raise SystemExit(f'Source escapes kernel tree: {path}')
        if sha256(path) != item['sha256']:
            raise SystemExit(f'Source differs from pinned Linux 7.2.9: {item["path"]}')

verify_sources()
defconfig = src/'arch/arm64/configs/rabbit_r1_defconfig'
defconfig_hash = sha256(defconfig)
out.mkdir(parents=True, exist_ok=True)
audit = dict(upstream_commit=manifest['commit'],
             source_manifest_sha256=sha256(manifest_path),
             source_head=subprocess.check_output(['git', '-C', str(src), 'rev-parse', 'HEAD'],
                                                 text=True).strip(),
             production_defconfig_sha256=defconfig_hash,
             source_files_verified=len(manifest['files']),
             commands=[], exit_code=None, hardware_tested=False,
             scope='ARM64 W=1 PowerVR subtree compile; no full kernel link or hardware test')
make = ['make', '-C', str(src), f'O={out}', 'ARCH=arm64',
        'CROSS_COMPILE=aarch64-linux-gnu-']
log_path = out/'build.log'
try:
    with log_path.open('w') as log:
        def run(command):
            audit['commands'].append(command)
            print('+', ' '.join(command), flush=True)
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            log.flush()
            result.check_returncode()

        run(make+['rabbit_r1_defconfig'])
        run([str(src/'scripts/config'), '--file', str(out/'.config'),
             '-e', 'DRM_POWERVR', '-e', 'DEBUG_FS', '-e', 'TRACING'])
        run(make+['olddefconfig'])
        config = (out/'.config').read_text().splitlines()
        for required in ('DRM_POWERVR', 'DEBUG_FS', 'TRACEPOINTS'):
            if f'CONFIG_{required}=y' not in config:
                raise RuntimeError(f'Scratch config did not enable {required}')
        run(make+['-j'+sys.argv[2], 'prepare'])
        # Remove only this subtree's prior objects through Kbuild. This ensures
        # the log and warning check cover every object even on repeated runs.
        run(make+['M=drivers/gpu/drm/imagination', 'clean'])
        run(make+['-j'+sys.argv[2], 'W=1', 'drivers/gpu/drm/imagination/'])

    archive = out/'drivers/gpu/drm/imagination/built-in.a'
    members = subprocess.check_output(['aarch64-linux-gnu-ar', 't', str(archive)], text=True)
    objects = sorted(Path(name).name for name in members.splitlines())
    if objects != manifest['compile_objects']:
        raise RuntimeError('Archive does not contain all 28 expected PowerVR objects')
    log_text = log_path.read_text()
    if re.search(r'\b(?:warning|error):', log_text):
        raise RuntimeError(f'Compiler diagnostics need review: {log_path}')
    verify_sources()
    if sha256(defconfig) != defconfig_hash:
        raise RuntimeError('Production defconfig changed during the check')
    audit.update(exit_code=0, objects=objects, scratch_config_sha256=sha256(out/'.config'),
                 build_log_sha256=sha256(log_path),
                 compiler=subprocess.check_output(['aarch64-linux-gnu-gcc', '--version'],
                                                  text=True).splitlines()[0])
except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
    audit.update(exit_code=getattr(error, 'returncode', 1), error=str(error))
    print(f'PowerVR compile failed: {error}\nBuild log: {log_path}', file=sys.stderr)
finally:
    (out/'audit.json').write_text(json.dumps(audit, indent=2)+'\n')
if audit['exit_code'] != 0:
    raise SystemExit(1)
print(f'PASS: 28 PowerVR objects compiled with W=1; evidence: {out}/audit.json')
PY
