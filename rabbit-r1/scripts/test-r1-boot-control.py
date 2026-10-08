#!/usr/bin/env python3
"""Build and run the Android boot-control core and storage boundary checks."""
from pathlib import Path
import os
import resource
import subprocess

ROOT = Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
boot = ROOT / 'android/device/boot'
tests = ROOT / 'tests/boot-control'
out = ROOT / 'out/boot-control'
out.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT / '.tmp')
os.environ['ASAN_OPTIONS'] = 'detect_leaks=1:abort_on_error=1'
flags = ['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
         '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-g', '-I', str(boot)]
subprocess.run(flags + [str(tests/'core.cpp'), str(boot/'Control.cpp'),
                       '-o', str(out/'core')], check=True)
subprocess.run([str(out/'core'), str(tests/'stock-vectors.txt')], check=True)
subprocess.run(flags + [str(tests/'storage.cpp'), str(boot/'Control.cpp'), str(boot/'LinuxStorage.cpp'),
                       '-Wl,--wrap=pread,--wrap=pwrite,--wrap=fsync,--wrap=ioctl',
                       '-o', str(out/'storage')], check=True)
subprocess.run([str(out/'storage')], check=True)
