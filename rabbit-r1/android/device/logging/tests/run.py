#!/usr/bin/env python3
"""Build and execute the real expdb loader with every device syscall mocked."""
from pathlib import Path
import os
import resource
import subprocess

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
root = Path('/rabbitr1')
source = Path(__file__).resolve().parents[1]
out = root / 'out/android-expdb-review'
out.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(root / '.tmp')
os.environ['ASAN_OPTIONS'] = 'detect_leaks=1:abort_on_error=1'
wrappers = ['open', 'close', 'read', 'fstat', 'lstat', 'opendir', 'readdir', 'closedir',
            'clock_gettime', 'nanosleep', 'realpath', 'getrandom', 'ioctl', 'syscall']
command = ['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-g',
           '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(source),
           str(source/'Expdb.cpp'), str(source/'tests/loader.cpp'),
           '-Wl,' + ','.join('--wrap=' + name for name in wrappers), '-o', str(out/'loader-tests')]
subprocess.run(command, check=True)
subprocess.run([str(out/'loader-tests')], check=True)
