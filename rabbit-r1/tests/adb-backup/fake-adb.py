#!/usr/bin/python3
"""Fixture executable. No real adb/device access; executes generated guards with mocks."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

base = Path(__file__).absolute().parent
state = json.loads((base / 'fixture.json').read_text())
args = sys.argv[1:]
assert args[:6] == ['-H', '127.0.0.1', '-P', '5037', '-s', 'TESTSERIAL'], args
with (base / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(args) + '\n')
mode = state['mode']
if args[6:] == ['features']:
    if mode == 'no-shell-v2':
        print('cmd\nstat_v2')
    elif mode == 'comma-features':
        print('cmd,shell_v2,stat_v2')
    else:
        print('shell_v2\ncmd\nstat_v2')
    sys.exit(0)
assert args[6:8] == ['shell', '-T'] and len(args) == 9, args
split = shlex.split(args[8])
assert len(split) == 3 and split[:2] == ['sh', '-c']
script = split[2]
assert script.startswith('set -eu\n')
assert script.splitlines()[-1].startswith('exec dd if=/dev/block/mmcblk0')
for forbidden in (' of=', ' seek=', ' conv=', ' oflag=', 'rpmb', 'force_ro', 'reboot', 'su '):
    assert forbidden not in script, forbidden
plan = state['plan']
disk = plan['disk']
values = {'/sys/class/block/mmcblk0/device/cid': plan['cid'],
          '/sys/class/block/mmcblk0/size': str(disk['bytes'] // 512),
          '/sys/class/block/mmcblk0/queue/logical_block_size': str(disk['logical_sector_bytes']),
          '/sys/class/block/mmcblk0/dev': f"{disk['major']}:{disk['minor']}"}
stats = {}
for r in plan['reads']:
    node = Path(r['node']).name
    values['/sys/class/block/' + node + '/size'] = str(r['bytes'] // 512)
    values['/sys/class/block/' + node + '/dev'] = f"{r['major']}:{r['minor']}"
    values['/sys/class/block/' + node + '/start'] = str(r['start_sector'])
    stats[r['node']] = f"{r['major']:x}:{r['minor']:x}"
if mode == 'cid-mismatch':
    values['/sys/class/block/mmcblk0/device/cid'] = 'f' * 32
if mode == 'geometry-mismatch':
    values['/sys/class/block/mmcblk0/size'] = '1'
if mode == 'partition-start-mismatch':
    values['/sys/class/block/mmcblk0p7/start'] = '1'
prefix = 'id() { printf "%s\\n" ' + shlex.quote('1' if mode == 'not-root' else '0') + '; }\n'
prefix += 'getprop() { printf "%s\\n" ' + shlex.quote('OTHER' if mode == 'serial-mismatch' else 'TESTSERIAL') + '; }\n'
prefix += 'cat() { case "$1" in\n'
for k, v in values.items():
    prefix += shlex.quote(k) + ') printf "%s\\n" ' + shlex.quote(v) + ';;\n'
prefix += '*) return 96;; esac; }\n'
prefix += 'stat() { case "$3" in\n'
for k, v in stats.items():
    prefix += shlex.quote(k) + ') printf "%s\\n" ' + shlex.quote(v) + ';;\n'
prefix += '*) return 96;; esac; }\n'
prefix += 'test() { if [ "$1" = "-b" ]; then return 0; elif [ "$1" = "-L" ]; then return 1; else builtin test "$@"; fi; }\n'
env = dict(os.environ, PATH=str(base))
result = subprocess.run(['/bin/bash', '-c', prefix + script], env=env)
sys.exit(result.returncode)
