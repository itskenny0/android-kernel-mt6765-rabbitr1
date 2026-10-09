#!/usr/bin/env python3
"""Test production battery STATUS, supplier lifetime, notifications and measurement concurrency."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6357-status'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/power/supply/mt6357-gauge.c')
args = parser.parse_args()
source = args.source.resolve()
if not source.is_relative_to(ROOT):
    raise SystemExit('Source must stay under /rabbitr1')
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
# Reuse the independently checked PMIC transaction model, and run its full suite.
# It produces host.c with the exact current source's structs and measurement code.
subprocess.run([sys.executable, str(Path(__file__).with_name('test-mt6357-charge-counter.py')),
                '--source', str(source)], check=True)
s = source.read_text()
host = (ROOT/'out/mt6357-charge-counter/host.c').read_text()
host = host[:host.index('int main(void)')]
# STATUS tests also exercise the supplier's device-core state under its lock.
old_device = 'struct device { struct device *parent; void *driver_data; void *of_node; };'
assert host.count(old_device) == 1
host = host.replace(old_device, 'struct device { struct device *parent; void *driver_data; void *of_node; struct { int status; } links; };')
host = host.replace('static int mt6357_gauge_read_status(struct mt6357_gauge *g, int *out) { return -ENODATA; }',
                    'static int mt6357_gauge_read_status(struct mt6357_gauge *g, int *out);')
fixture = Path(__file__).resolve().parent.parent/'tests/battery/mt6357-status.c'
model, checks = fixture.read_text().split('/* INSERT PRODUCTION STATUS FUNCTIONS HERE */')

def function(name, text=s):
    match = re.search(r'(?:static )?[\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+2]+'\n'

names = ('mt6357_gauge_read_status', 'mt6357_gauge_status_work',
         'mt6357_gauge_status_notify', 'mt6357_gauge_stop_status',
         'mt6357_gauge_init_status', 'mt6357_gauge_probe')
charger_source = SRC/'drivers/power/supply/mt6370-charger.c'
charger_text = charger_source.read_text()
charger_enum = re.search(r'enum \{\s*MT6370_CHG_STAT_READY.*?\n};', charger_text, re.S)
assert charger_enum
charger_functions = charger_enum[0]+'\n'+''.join(function(name, charger_text)
    for name in ('mt6370_chg_get_online', 'mt6370_chg_get_status'))
# Validate that the shipping board uses this one actual charger relationship.
dts = (SRC/'arch/arm64/boot/dts/mediatek/mt6765-rabbit-r1.dts').read_text()
gauge_node = re.search(r'r1_battery: gauge \{(.*?)\n\t};', dts, re.S)[1]
assert re.findall(r'power-supplies\s*=\s*<([^>]+)>', gauge_node) == ['&r1_charger']
assert re.search(r'r1_charger: charger \{\s*compatible = "mediatek,mt6370-charger";', dts)
host = host.replace('static void mutex_lock(struct mutex *m)\n{',
    'static void before_mutex_lock(struct mutex *m);\nstatic void mutex_lock(struct mutex *m)\n{ before_mutex_lock(m);')
code = host+model+charger_functions+''.join(function(name) for name in names)+checks
path = OUT/'host.c'
path.write_text(code)
command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
           '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-const-variable',
           '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
           '-fno-pie', '-no-pie', str(path), '-o', str(OUT/'host')]
subprocess.run(command, check=True)
result = subprocess.run([str(OUT/'host')], check=True, timeout=60, text=True, capture_output=True)
print(result.stdout, end='')
(OUT/'audit.json').write_text(json.dumps(dict(
    hardware_tested=False, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),
    charger_source_sha256=hashlib.sha256(charger_source.read_bytes()).hexdigest(),
    host_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    compiler_command=command, test_command=[str(OUT/'host')],
    captured_stdout=result.stdout, captured_stderr=result.stderr, exit_code=result.returncode,
    scope='Actual production STATUS read/worker/callback/stop/init/probe and shared current/counter acquisition; mocked Linux workqueue, devres, device links, supplier reads, OF references and PMIC bus. Existing counter suite runs first. No electrical, capacity or health validation.'
), indent=2)+'\n')
