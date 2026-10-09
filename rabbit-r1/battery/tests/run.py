#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run the C core against frozen stock instruction outputs, with sanitizers."""
from pathlib import Path
import hashlib
import json
import os
import resource
import subprocess

ROOT = Path('/rabbitr1')
SOURCE = Path(__file__).resolve().parents[1]
OUT = ROOT/'out/battery-profile'
OUT.mkdir(parents=True, exist_ok=True)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
os.environ['ASAN_OPTIONS'] = 'detect_leaks=1:abort_on_error=1'
provenance = json.loads((SOURCE/'provenance.json').read_text())
for name, expected in provenance['generated_sha256'].items():
    if hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() != expected:
        raise SystemExit('Verified stock input changed: ' + name)
fixture = json.loads((SOURCE/'tests/stock-oracles.json').read_text())
lines = []
for case in fixture['temperature_profiles']:
    values = [case['temp_c'], case['qmax_01mah'], case['qmax_high_01mah']]
    values += [value for row in case['profile'] for value in row]
    lines.append('T ' + ' '.join(map(str, values)))
for temp, operation, value, expected in fixture['ocv_vectors']:
    kind = {'soc_to_ocv': 'D', 'ocv_to_soc': 'O'}[operation]
    lines.append(f'{kind} {temp} {value} {expected}')
for temp, capacity, percentages in fixture['normalization_vectors']:
    lines.append('N ' + ' '.join(map(str, [temp, capacity, *percentages])))
for case in fixture['repeated_dod_vectors']:
    lines.append('B ' + ' '.join(map(str, case)))
for case in fixture['live_counter_vectors']:
    lines.append('L ' + ' '.join(map(str, case)))
# Only serialization happens here: no temperature, interpolation or counter math.
(OUT/'stock-oracles.txt').write_text('\n'.join(lines) + '\n')
command = ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wconversion', '-Wshadow',
           '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-g',
           '-I', str(SOURCE), str(SOURCE/'profile.c'), str(SOURCE/'stock_profiles.c'),
           str(SOURCE/'tests/profile.c'), '-o', str(OUT/'profile-tests')]
subprocess.run(command, check=True)
subprocess.run([str(OUT/'profile-tests'), str(OUT/'stock-oracles.txt')], check=True)
