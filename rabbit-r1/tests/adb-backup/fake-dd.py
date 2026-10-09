#!/usr/bin/python3
"""Only synthetic stdout bytes; never opens a source device."""
import json
import os
from pathlib import Path
import sys
import time

base = Path(__file__).absolute().parent
state = json.loads((base / 'fixture.json').read_text())
with (base / 'dd-calls.jsonl').open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
args = dict(a.split('=', 1) for a in sys.argv[1:])
assert set(args) == {'if', 'bs', 'skip', 'count', 'iflag', 'status'}
assert args['if'].startswith('/dev/block/mmcblk0') and 'rpmb' not in args['if']
assert args['iflag'] == 'count_bytes,skip_bytes' and args['status'] == 'none'
n = int(args['count'])
mode = state['mode']
if mode == 'short':
    n -= 1
if mode == 'extra':
    n += 1
if mode == 'idle':
    (base / 'child.pid').write_text(str(os.getpid()))
    time.sleep(30)
if mode == 'warning':
    os.write(2, b'synthetic warning\n')
if mode == 'stderr-overflow':
    os.write(2, b'W' * 65537)
data = bytes(range(256))
offset = 0
while offset < n:
    chunk = min(65536, n - offset)
    os.write(1, (data * ((chunk + 255) // 256))[:chunk])
    offset += chunk
    if mode == 'total-timeout':
        time.sleep(0.4)
sys.exit(17 if mode == 'nonzero' else 0)
