#!/usr/bin/env python3
"""Bounded fake-process and host-fault controls; no real ADB/SSH/device access."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest import mock

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / 'receive-adb-backup.py'
FIXTURES = SCRIPTS.parent / 'tests/adb-backup'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--out', type=Path, required=True, help='new private result directory under /rabbitr1')
args = parser.parse_args()
OUT = args.out.absolute()
if not OUT.is_relative_to(Path('/rabbitr1')) or OUT.resolve() != OUT:
    parser.error('Require a direct output path under /rabbitr1')
if os.path.lexists(OUT):
    parser.error('Refusing preexisting output')
OUT.mkdir()
spec = importlib.util.spec_from_file_location('receiver', SCRIPT)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
BASE = {'schema': 1, 'serial': 'TESTSERIAL', 'cid': '0' * 32,
        'disk': {'node': '/dev/block/mmcblk0', 'bytes': 8 * r.MIB,
                 'logical_sector_bytes': 512, 'major': 179, 'minor': 0},
        'excluded_nodes': ['/dev/block/mmcblk0p45'], 'reserve_bytes': r.GIB,
        'timeout_seconds': 8, 'idle_seconds': 3,
        'reads': [{'name': 'test_partition', 'kind': 'partition', 'node': '/dev/block/mmcblk0p7',
                   'bytes': 3 * r.MIB, 'start_sector': 2048, 'major': 179, 'minor': 7,
                   'offset': 0, 'length': 3 * r.MIB}]}
results = []


def setup(root, mode='valid', plan=None):
    case = root / ('case-' + str(len(results)))
    case.mkdir()
    p = copy.deepcopy(plan or BASE)
    (case / 'fixture.json').write_text(json.dumps({'plan': p, 'mode': mode}))
    (case / 'plan.json').write_text(json.dumps(p))
    for source, target in [('fake-adb.py', 'adb'), ('fake-dd.py', 'dd')]:
        shutil.copyfile(FIXTURES / source, case / target)
        (case / target).chmod(0o700)
    return case, p


def received(case, plan, fault=None):
    output = case / 'backup'
    r.validate_plan(plan)
    r.check_destination(case, output)
    return r.receive(plan, hashlib.sha256((case / 'plan.json').read_bytes()).hexdigest(), output, case / 'adb')


def failure(case, p, expect):
    try:
        received(case, p)
    except (r.Failure, OSError) as exc:
        assert expect in str(exc), (expect, str(exc))
    else:
        raise AssertionError('failure expected')
    assert not (case / 'backup').exists()


with tempfile.TemporaryDirectory(prefix='fake-receiver-', dir=OUT) as temp:
    root = Path(temp)
    for mode in ['valid', 'warning', 'comma-features']:
        case, p = setup(root, mode)
        m = received(case, p)
        got = (case / 'backup/test_partition.img').read_bytes()
        expected = bytes(range(256)) * (len(got) // 256)
        assert got == expected and len(got) == p['reads'][0]['length']
        entry = m['entries'][0]
        assert hashlib.sha256(got).hexdigest() == entry['host_reread_sha256'] == entry['process']['sha256']
        assert entry['process']['eof'] == {'stdout': True, 'stderr': True}
        assert entry['process']['exit_code'] == 0
        assert (case / 'backup/manifest.json').is_file() and not (case / 'backup.partial').exists()
        assert stat.S_IMODE((case / 'backup').stat().st_mode) == 0o700
        assert stat.S_IMODE((case / 'backup/test_partition.img').stat().st_mode) == 0o600
        if mode == 'warning':
            assert bytes.fromhex(entry['process']['stderr_hex']) == b'synthetic warning\n'
        results.append({'case': mode, 'passed': True})
    for mode, message in [('short', 'short stdout'), ('extra', 'extra stdout'),
                          ('nonzero', 'exited 17'), ('stderr-overflow', 'stderr exceeded'),
                          ('cid-mismatch', 'CID'), ('serial-mismatch', 'serial'),
                          ('not-root', 'root'), ('geometry-mismatch', 'disk size'),
                          ('partition-start-mismatch', 'partition start'),
                          ('no-shell-v2', 'shell_v2 required')]:
        case, p = setup(root, mode)
        failure(case, p, message)
        if mode in ('cid-mismatch', 'serial-mismatch', 'not-root', 'geometry-mismatch',
                    'partition-start-mismatch', 'no-shell-v2'):
            assert not (case / 'dd-calls.jsonl').exists(), mode
        results.append({'case': mode, 'passed': True})
    for mode, timeout, idle, message in [('idle', 4, 1, 'stdout progress timeout'),
                                       ('total-timeout', 1, 1, 'total process timeout')]:
        case, p = setup(root, mode)
        p.update(timeout_seconds=timeout, idle_seconds=idle)
        with mock.patch.object(os, 'killpg', wraps=os.killpg) as killed:
            failure(case, p, message)
        assert [call.args[1] for call in killed.call_args_list] == [r.signal.SIGTERM, r.signal.SIGKILL]
        results.append({'case': mode, 'passed': True})
    for kind in ['existing', 'symlink', 'partial', 'parent-symlink']:
        case, p = setup(root)
        if kind == 'existing':
            (case / 'backup').mkdir()
        elif kind == 'symlink':
            (case / 'backup').symlink_to(case / 'absent')
        elif kind == 'partial':
            (case / 'backup.partial').mkdir()
        else:
            (case / 'link').symlink_to(case, target_is_directory=True)
        output = case / 'link/backup' if kind == 'parent-symlink' else case / 'backup'
        try:
            r.check_destination(case, output)
        except r.Failure:
            pass
        else:
            raise AssertionError(kind)
        assert not (case / 'calls.jsonl').exists()
        results.append({'case': kind, 'passed': True})
    for kind in ['space-preflight', 'host-write', 'host-fsync', 'host-reread', 'publication-failure']:
        case, p = setup(root)
        if kind == 'space-preflight':
            target, replacement, expect = 'free_bytes', lambda path: r.GIB, 'reserve reached'
        elif kind == 'host-write':
            def badwrite(fd, data):
                raise OSError('fixture write failure')
            target, replacement, expect = 'write_all', badwrite, 'fixture write failure'
        elif kind == 'host-reread':
            original = r.file_hash
            target, replacement, expect = 'file_hash', lambda path: '0' * 64 if str(path).endswith('.img.partial') else original(path), 'reread hash mismatch'
        elif kind == 'publication-failure':
            def badrename(*args):
                raise OSError('fixture publication failure')
            target, replacement, expect = 'rename_noreplace', badrename, 'fixture publication failure'
        else:
            original = os.fsync
            def badsync(fd):
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError('fixture fsync failure')
                return original(fd)
            target, replacement, expect = None, badsync, 'fixture fsync failure'
        context = mock.patch.object(r, target, replacement) if target else mock.patch.object(os, 'fsync', replacement)
        with context:
            failure(case, p, expect)
        if kind == 'space-preflight':
            assert not (case / 'calls.jsonl').exists()
        results.append({'case': kind, 'passed': True})
    p = copy.deepcopy(BASE)
    d = p['disk']
    p['reads'] = [{'name': name, 'kind': 'gpt', 'node': d['node'], 'bytes': d['bytes'],
                   'start_sector': None, 'major': 179, 'minor': 0, 'offset': offset, 'length': r.MIB}
                  for name, offset in [('first_mib', 0), ('last_mib', d['bytes'] - r.MIB)]]
    p['reads'].append({'name': 'preloader_a', 'kind': 'boot', 'node': d['node'] + 'boot0',
                       'bytes': 4096, 'start_sector': None, 'major': 179, 'minor': 32, 'offset': 0, 'length': 4096})
    case, p = setup(root, plan=p)
    m = received(case, p)
    assert len(m['entries']) == 3
    assert len((case / 'dd-calls.jsonl').read_text().splitlines()) == 3
    results.append({'case': 'multi-read-two-GPT-windows-and-boot-area', 'passed': True})
    for kind in ['rpmb', 'userdata', 'traversal', 'injection', 'partial-partition', 'unbounded-disk', 'overlap']:
        p = copy.deepcopy(BASE)
        read = p['reads'][0]
        if kind == 'rpmb':
            read.update(kind='boot', node='/dev/block/mmcblk0rpmb', start_sector=None)
        elif kind == 'userdata':
            read['node'] = '/dev/block/mmcblk0p45'
        elif kind == 'traversal':
            read['name'] = '../escape'
        elif kind == 'injection':
            p['serial'] = 'x;echo BAD'
        elif kind == 'partial-partition':
            read['length'] -= 512
        elif kind == 'overlap':
            p['reads'].append(dict(read, name='duplicate'))
        else:
            read.update(kind='gpt', node=p['disk']['node'], bytes=p['disk']['bytes'],
                        length=p['disk']['bytes'], start_sector=None, major=179, minor=0)
        try:
            r.validate_plan(p)
        except r.Failure:
            pass
        else:
            raise AssertionError(kind)
        results.append({'case': kind, 'passed': True})
    case, p = setup(root)
    cmd = [sys.executable, str(SCRIPT), '--plan', str(case / 'plan.json'),
           '--out', str(case / 'backup'), '--workspace', str(case), '--adb', str(case / 'adb')]
    checked = subprocess.run(cmd, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    assert not (case / 'calls.jsonl').exists(), 'check-only executed fake ADB'
    results.append({'case': 'CLI-default-check-only-no-process', 'passed': True})
    (case / 'fixture.json').write_text(json.dumps({'plan': p, 'mode': 'nonzero'}))
    failed = subprocess.run(cmd + ['--execute'], capture_output=True)
    assert failed.returncode == 1 and b'exited 17' in failed.stderr
    assert not (case / 'backup').exists()
    results.append({'case': 'CLI-nonzero-propagation', 'passed': True})

result = {'status': 'passed', 'cases': len(results), 'results': results,
          'receiver_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
          'real_adb_or_ssh_or_device_access': False,
          'scope': 'Actual receiver with fake ADB executable and generated shell guards executed using mocked read-only utilities; host fault injections.'}
with (OUT / 'test-result.json').open('x') as f:
    json.dump(result, f, indent=2)
    f.write('\n')
print(json.dumps({'status': 'passed', 'cases': len(results)}))
