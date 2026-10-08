#!/usr/bin/env python3
"""Compile and exercise the production r1 charging decision and transactions."""
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT / 'src/mainline'
OUT = ROOT / 'out/r1-charging-tests'
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT / '.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
s = (SRC / 'drivers/power/supply/rabbit-r1-charging.c').read_text()
def function(name):
    m = re.search(r'static [\w *]+\b' + name + r'\([^;]*?\)\n\{', s)
    assert m, name
    return s[m.start():s.index('\n}\n', m.end()) + 2] + '\n'
parts = s[s.index('#define R1_CHARGE_MIN_UA'):s.index('static bool r1_charge_limit_valid')]
for name in ('r1_charge_limit_valid', 'r1_charge_decide', 'r1_get', 'r1_set',
             'r1_charge_isolate', 'r1_charge_sample', 'r1_charge_fresh',
             'r1_charge_apply', 'r1_charge_work', 'r1_charge_notify',
             'charge_control_limit_store', 'r1_charge_suspend', 'r1_charge_resume'):
    parts += function(name)
(OUT / 'policy-under-test.h').write_text(parts)
command = ['cc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-parameter', '-Wno-unused-variable',
                '-fsanitize=address,undefined', '-fno-pie', '-no-pie', '-pthread',
                '-I' + str(OUT), str(SRC / 'rabbit-r1/tests/r1-charging-test.c'),
                '-o', str(OUT / 'test')]
subprocess.run(command, check=True)
subprocess.run([str(OUT / 'test')], check=True, timeout=60)

mutations = {
    'invalid-sensor': ('if (!s->valid)', 'if (false)'),
    'charger-fault': ('if (s->health != POWER_SUPPLY_HEALTH_GOOD)', 'if (false)'),
    'sdp-budget': ('budget = clamp(s->gadget_ua, 0, 500000);', 'budget = 500000;'),
    'late-budget-change': ('return r1_charge_fresh(p, start, generation) ? 0 : -EAGAIN;', 'return 0;'),
}
for name, (before, after) in mutations.items():
    assert parts.count(before) == 1, name
    (OUT / 'policy-under-test.h').write_text(parts.replace(before, after))
    subprocess.run(command, check=True)
    with (OUT / (name + '.log')).open('w') as log:
        result = subprocess.run([str(OUT / 'test')], stdout=log, stderr=log, timeout=60)
    assert result.returncode != 0, 'Faulty implementation passed: ' + name
(OUT / 'policy-under-test.h').write_text(parts)
subprocess.run(command, check=True)
print('PASS: four faulty policy variants rejected at runtime')
