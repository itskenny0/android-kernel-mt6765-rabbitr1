#!/usr/bin/env python3
"""Check Health identity and startup deadlines without Binder or device access."""
import hashlib
import json
import resource
from pathlib import Path
import shutil
import subprocess
import tempfile

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
SOURCE = Path(__file__).resolve().parents[1]
OUTPUT = Path('/rabbitr1/out/health-readiness-tests')
OUTPUT.mkdir(parents=True, exist_ok=True)
compiler = shutil.which('clang++')
if compiler is None:
    raise SystemExit('clang++ is required')
inputs = [SOURCE/'android/device/health/Readiness.cpp',
          SOURCE/'android/device/health/Readiness.h',
          SOURCE/'tests/health/readiness.cpp', SOURCE/'tests/health/deadlines.cpp']
record = {'scope': 'Actual readiness code with synthetic sysfs and modeled clocks; no Binder, init or hardware execution',
          'sources': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
          'commands': [], 'passed': False}
flags = [compiler, '-std=c++20', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
         '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
         '-I'+str(SOURCE/'android/device/health')]

def run(argv):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True)
    output = result.stdout + result.stderr
    log = OUTPUT/f'{len(record["commands"]):02d}.log'
    log.write_text(output)
    record['commands'].append({'argv': [str(x) for x in argv], 'exit_code': result.returncode,
                               'log': str(log), 'sha256': hashlib.sha256(output.encode()).hexdigest()})
    if output:
        print(output, end='')
    if result.returncode:
        raise RuntimeError('Health readiness check failed; see '+str(log))

try:
    with tempfile.TemporaryDirectory(prefix='run-', dir=OUTPUT) as tmp:
        temp = Path(tmp)
        for name in ['readiness', 'deadlines']:
            extra = ['-Wl,--wrap=stat'] if name == 'readiness' else []
            run(flags + [inputs[0], SOURCE/f'tests/health/{name}.cpp', *extra, '-o', temp/name])
            run([temp/name, temp/'sysfs'] if name == 'readiness' else [temp/name])
    record['passed'] = True
finally:
    (OUTPUT/'result.json').write_text(json.dumps(record, indent=2)+'\n')
