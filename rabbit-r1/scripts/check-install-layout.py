#!/usr/bin/env python3
"""Inspect pinned GPT/observation files for offline r1 layout consistency."""
import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SECTOR = 512
PARSER_SHA256 = 'b8a5db471033f9c91c3bb5d01e841210f925b5e4b7917d2498455962c52695e4'

ROOT = Path('/rabbitr1')
AREAS = ('boot1', 'boot2', 'rpmb', 'gp1', 'gp2', 'gp3', 'gp4', 'user')
CHIP_FIELDS = ('hw_code', 'hw_sub_code', 'hw_version', 'sw_version', 'chip_evolution')


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def workspace_path(value, *, absent=False):
    path = Path(value)
    need(path.is_absolute() and path.is_relative_to(ROOT) and '..' not in path.parts,
         'path must be under /rabbitr1 without traversal')
    current = ROOT
    parts = path.relative_to(ROOT).parts
    need(parts, 'workspace root is not a file')
    for index, part in enumerate(parts):
        current /= part
        if absent and index == len(parts) - 1:
            need(not os.path.lexists(current), 'output already exists')
        else:
            mode = current.lstat().st_mode
            need(not stat.S_ISLNK(mode), 'symlink path refused')
            if index < len(parts) - 1:
                need(stat.S_ISDIR(mode), 'parent is not a directory')
    return path


def stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def read_input(value, limit, expected=None):
    path = workspace_path(value)
    if expected is not None:
        need(re.fullmatch('[0-9a-f]{64}', expected) is not None, 'invalid SHA256 pin')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= limit,
             'input is not a bounded regular file')
        data = stream.read(limit + 1)
        need(len(data) == before.st_size and stamp(os.fstat(stream.fileno())) == stamp(before),
             'input changed while reading')
    need(stamp(path.lstat()) == stamp(before), 'input path changed while reading')
    sha = digest(data)
    need(expected is None or sha == expected, 'input SHA256 differs from supplied pin')
    return data, {'bytes': len(data), 'sha256': sha}


def unique(items):
    result = {}
    for key, value in items:
        need(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def observation(data):
    value = json.loads(data, object_pairs_hook=unique)
    need(type(value) is dict and set(value) == {
        'schema', 'chip', 'cid', 'emmc_type', 'sector_bytes', 'sizes', 'fwver'},
        'unsupported observation fields')
    need(type(value['schema']) is int and value['schema'] == 1, 'observation schema')
    need(type(value['chip']) is dict and set(value['chip']) == set(CHIP_FIELDS), 'chip fields')
    need(all(type(n) is int and 0 <= n < 2**32 for n in value['chip'].values())
         and value['chip']['hw_code'] != 0, 'chip values')
    cid = value['cid']
    need(type(cid) is str and re.fullmatch('[0-9a-f]{32}', cid) is not None
         and cid not in ('0' * 32, 'f' * 32), 'nonblank lowercase CID required')
    need(type(value['emmc_type']) is int and value['emmc_type'] == 1, 'eMMC type')
    need(type(value['sector_bytes']) is int and value['sector_bytes'] == SECTOR,
         '512-byte logical sectors required')
    need(type(value['fwver']) is int and 0 <= value['fwver'] < 2**64, 'firmware version')
    sizes = value['sizes']
    need(type(sizes) is dict and set(sizes) == set(AREAS), 'storage area fields')
    need(all(type(n) is int and 0 <= n < 2**64 and n % SECTOR == 0
             for n in sizes.values()), 'storage geometry')
    need(sizes['user'] >= 68 * SECTOR, 'user capacity')
    return value


def inspect(args):
    parser_path = Path(__file__).with_name('r1_gpt.py')
    parser_bytes, parser_pin = read_input(parser_path, 128 * 1024, expected=PARSER_SHA256)
    spec = importlib.util.spec_from_file_location('r1_install_layout_gpt', parser_path)
    g = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = g
    exec(compile(parser_bytes, str(parser_path), 'exec'), g.__dict__)
    _, helper_pin = read_input(Path(__file__).resolve(), 128 * 1024)
    observed_raw, observed_pin = read_input(args.observation, 16384, args.observation_sha256)
    expected_raw, expected_pin = read_input(args.expected_observation, 16384,
                                          args.expected_observation_sha256)
    observed, expected = observation(observed_raw), observation(expected_raw)
    need(observed == expected, 'observed and expected storage identity differ')
    need(type(args.user_bytes) is int and args.user_bytes == expected['sizes']['user'],
         'independently supplied capacity differs from observations')
    inputs, blobs = {}, []
    for name in ('primary_header', 'primary_array', 'backup_header', 'backup_array'):
        limit = g.SECTOR if name.endswith('header') else g.MAX_ENTRIES * g.ENTRY_SIZE
        blob, inputs[name] = read_input(getattr(args, name), limit)
        blobs.append(blob)
    pair = g.parse_pair(*blobs, user_bytes=args.user_bytes,
                        disk_guid_policy=args.disk_guid_policy)
    regions = g.r1_regions(pair)
    binding = {'schema': 1, 'disk_guid_policy': args.disk_guid_policy,
               'parser': parser_pin, 'inspector': helper_pin, 'user_bytes': args.user_bytes,
               'inputs': inputs, 'observation': observed_pin, 'expected_observation': expected_pin,
               'pair_fingerprint': pair.fingerprint()}
    return {'schema': 1, 'status': 'consistent', 'scope': 'offline layout consistency',
            'binding': binding,
            'binding_sha256': digest(json.dumps(binding, sort_keys=True,
                                               separators=(',', ':')).encode()),
            'partition_count': len(pair.partitions), 'pair': asdict(pair),
            'install_regions': [asdict(part) for part in regions],
            'limits': ['Supplied files and observation pins are caller inputs, not live hardware evidence.',
                       'This report does not establish cold state, freshness, unlock state or authorization to write.',
                       'Complete payload, backup, LK-original and physical transport checks remain separate.']}


def publish(path, report):
    path = workspace_path(path, absent=True)
    data = (json.dumps(report, indent=2) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('primary-header', 'primary-array', 'backup-header', 'backup-array',
                 'observation', 'expected-observation'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--observation-sha256', required=True)
    parser.add_argument('--expected-observation-sha256', required=True)
    parser.add_argument('--user-bytes', type=int, required=True)
    parser.add_argument('--disk-guid-policy', choices=('nonzero', 'zero'), default='nonzero')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        workspace_path(args.out, absent=True)
        report = inspect(args)
        publish(args.out, report)
    except (ValueError, OSError) as exc:
        parser.exit(1, 'Layout inspection failed: ' + str(exc) + '\n')
    print(json.dumps({'status': report['status'], 'scope': report['scope'],
                      'partition_count': report['partition_count'],
                      'binding_sha256': report['binding_sha256'], 'output': str(args.out)}))


if __name__ == '__main__':
    main()
