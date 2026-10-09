#!/usr/bin/env python3
"""Extract actual charging provider, supply, probe and stop paths; model services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
ROOT = Path('/rabbitr1')
PROJECT = ROOT/'src/mainline/rabbit-r1'
p.add_argument('--source', type=Path, default=PROJECT.parent/'drivers/power/supply/rabbit-r1-charging.c')
p.add_argument('--output', type=Path, default=ROOT/'out/r1-charging-suppliers')
a = p.parse_args()
a.source, a.output = a.source.resolve(), a.output.resolve()
if not a.source.is_relative_to(ROOT) or not a.output.is_relative_to(ROOT):
    p.error("Paths must stay under /rabbitr1")
a.output.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(a.output)
s = a.source.read_text()
def function(name):
    start = s.index('static ', s.index(name) - 40)
    # Find the declaration line rather than a preceding function.
    start = s.rfind('\n', 0, s.index(name)) + 1
    brace = s.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (s[end] == '{') - (s[end] == '}')
        end += 1
    return s[start:end] + '\n'
names = ['r1_charge_provider', 'r1_charge_supply', 'r1_charge_stop', 'r1_charge_probe']
parts = '\n'.join(function(n) for n in names)
(a.output / 'under-test.h').write_text(parts)
fixture = PROJECT/'tests/r1-charging-suppliers.c'
results = []
for i2c in (0, 1):
    cmd = ['cc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
           '-Wno-unused-parameter', '-fsanitize=address,undefined', '-fno-pie',
           '-no-pie', '-pthread', '-DI2C_REACHABLE=' + str(i2c),
           '-I' + str(a.output), str(fixture), '-o', str(a.output / ('test-' + str(i2c)))]
    r = subprocess.run(cmd, text=True, capture_output=True)
    (a.output / ('compile-' + str(i2c) + '.log')).write_text(r.stdout + r.stderr)
    r.check_returncode()
    r = subprocess.run([cmd[-1]], text=True, capture_output=True, timeout=30)
    (a.output / ('run-' + str(i2c) + '.log')).write_text(r.stdout + r.stderr)
    r.check_returncode()
    results.append({'i2c_reachable': i2c, 'command': cmd, 'output': r.stdout})
record = {'passed': True, 'source': str(a.source),
          'source_sha256': hashlib.sha256(a.source.read_bytes()).hexdigest(),
          'functions': {n: hashlib.sha256(function(n).encode()).hexdigest() for n in names},
          'results': results,
          'scope': 'Actual helper/probe/stop bodies; OF, device links, devres, power-supply and scheduling services are explicit models. Busy-lock case uses real pthread mutex/thread. No kernel race or physical charging claim.'}
(a.output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
print('\n'.join(x['output'].strip() for x in results))
