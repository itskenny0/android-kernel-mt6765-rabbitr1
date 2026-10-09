#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run the C core against frozen stock instruction outputs, with sanitizers."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import resource
import subprocess

ROOT = Path('/rabbitr1')
SOURCE = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out', type=Path, default=ROOT/'out/battery-profile')
args = parser.parse_args()
OUT = args.out.resolve()
OUT.mkdir(parents=True, exist_ok=True)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(OUT)
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

# The cutoff capture is independent and separately pinned. No fixture expansion
# computes expected voltage, resistance, capacity, DOD or an error decision.
usable_provenance = json.loads((SOURCE/'tests/usable-provenance.json').read_text())
assert hashlib.sha256((SOURCE/'tests/replay-usable-capacity.py').read_bytes()).hexdigest() == usable_provenance['replay_script_sha256']
usable_path = SOURCE/'tests/usable-oracles.json'
assert hashlib.sha256(usable_path.read_bytes()).hexdigest() == usable_provenance['capture_sha256']
usable = json.loads(usable_path.read_text())
lines = []
for case in usable['cases'] + usable['zero_tail_boundaries']:
    if case['padding'] != 'repeat_last':
        continue  # Actual zero-tail evidence is retained, never a valid C row.
    errors = {'stock_negative_row_index': -4,
              'stock_search_fell_through_without_crossing': -4}
    reason = case.get('rejected')
    if reason is not None and reason not in errors:
        raise AssertionError('Capture needs explicit error mapping: ' + reason)
    values = [case[key] for key in ('temp_c', 'minimum_01mv', 'discharge_01ma',
              'rac_01mohm', 'shunt_01mohm', 'meter_01mohm', 'dc_ratio_percent')]
    values.append(errors[reason] if reason else 0)
    if not reason:
        values += [case[key] for key in ('initial_cutoff_01mv', 'initial_usable_01mah',
                                         'cutoff_01mv', 'usable_01mah')]
        values += case['dod_before_u16_store']
    lines.append(' '.join(map(str, values)))
(OUT/'usable-oracles.txt').write_text('\n'.join(lines) + '\n')
command[-3:] = [str(SOURCE/'tests/usable-capacity.c'), '-o', str(OUT/'usable-capacity-tests')]
subprocess.run(command, check=True)
subprocess.run([str(OUT/'usable-capacity-tests'), str(OUT/'usable-oracles.txt')], check=True)
