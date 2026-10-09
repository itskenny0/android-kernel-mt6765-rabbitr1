#!/usr/bin/env python3
"""Prepare complete LK A/B files offline; no device or flash operations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

import lk_slots

ROOT = Path('/rabbitr1')


def local(value):
    path = Path(value).absolute()
    if not path.is_relative_to(ROOT) or path.resolve() != path:
        raise ValueError('Require a direct path under /rabbitr1: '+str(path))
    return path


def fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def read_input(path, size):
    path = local(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        raise ValueError('Input must be a regular file of exact length: '+str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or fingerprint(opened) != fingerprint(before):
            raise ValueError('Input changed while opening: '+str(path))
        chunks = []
        count = 0
        while count <= size:
            data = os.read(fd, min(65536, size + 1 - count))
            if not data:
                break
            chunks.append(data)
            count += len(data)
        if count != size or fingerprint(os.fstat(fd)) != fingerprint(opened):
            raise ValueError('Input changed while reading: '+str(path))
        if fingerprint(path.lstat()) != fingerprint(opened):
            raise ValueError('Input pathname changed while reading: '+str(path))
        return b''.join(chunks), fingerprint(opened)
    finally:
        os.close(fd)


def write_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def prepare_files(original_a, original_b, patched, output):
    inputs = {'lk_a': (local(original_a), lk_slots.PARTITION_BYTES),
              'lk_b': (local(original_b), lk_slots.PARTITION_BYTES),
              'patched_lk': (local(patched), lk_slots.PAYLOAD_BYTES)}
    output = local(output)
    if os.path.lexists(output):
        raise ValueError('Refusing preexisting output: '+str(output))
    if not output.parent.is_dir():
        raise ValueError('Output parent must already exist')
    blobs, identities = {}, {}
    for name, (path, size) in inputs.items():
        blobs[name], identities[name] = read_input(path, size)
    prepared = lk_slots.prepare(blobs['lk_a'], blobs['lk_b'], blobs['patched_lk'])
    # Check supplied files again before publishing derived files. This is an
    # offline file observation, not a cold-device acquisition or freshness proof.
    for name, (path, size) in inputs.items():
        current, identity = read_input(path, size)
        if current != blobs[name] or identity != identities[name]:
            raise ValueError('Input changed during preparation: '+str(path))
    output.mkdir(mode=0o700)
    profiles = dict(prepared.profiles)
    originals = dict(prepared.originals)
    images = {}
    for name, data in prepared.images:
        path = output/(name+'.img')
        write_file(path, data)
        actual, _ = read_input(path, lk_slots.PARTITION_BYTES)
        if actual != data:
            raise ValueError('Written output bytes differ: '+str(path))
        images[name] = {'file': path.name, 'bytes': len(data),
                        'sha256': hashlib.sha256(data).hexdigest(),
                        'original_sha256': originals[name]}
    for name, (path, size) in inputs.items():
        current, identity = read_input(path, size)
        if current != blobs[name] or identity != identities[name]:
            raise ValueError('Input changed before preparation record: '+str(path))
    for name, data in prepared.images:
        actual, _ = read_input(output/(name+'.img'), lk_slots.PARTITION_BYTES)
        if actual != data:
            raise ValueError('Output changed before preparation record: '+name)
    record = {
        'format': 1,
        'scope': 'offline LK byte preparation only; not an installable package or device transaction',
        'policy': 'r1-v0.8.293-current-prefix-per-slot-tails-v1',
        'partition_bytes': lk_slots.PARTITION_BYTES,
        'prefix_bytes': lk_slots.PAYLOAD_BYTES,
        'reviewed_prefixes': {'stock_sha256': lk_slots.STOCK_SHA256,
                              'patched_sha256': lk_slots.PATCHED_SHA256},
        'originals': {name: {'path': str(inputs[name][0]),
                            'bytes': lk_slots.PARTITION_BYTES,
                            'sha256': originals[name], 'profile': profiles[name]}
                      for name in lk_slots.SLOTS},
        'patched_prefix': {'path': str(inputs['patched_lk'][0]),
                           'bytes': lk_slots.PAYLOAD_BYTES,
                           'sha256': lk_slots.PATCHED_SHA256},
        'images': images,
        'required_before_any_future_write':
            'Bind both complete named original hashes to verified cold backup containers in the same device/GPT/package transaction; this record does not establish that binding.'
    }
    # A failure leaves a fresh, incomplete directory for inspection. The record
    # is written last; no multi-file atomicity or physical durability is claimed.
    write_file(output/'preparation.json', (json.dumps(record, indent=2)+'\n').encode())
    fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lk-a', type=Path, required=True, help='Complete original lk_a regular file')
    parser.add_argument('--lk-b', type=Path, required=True, help='Complete original lk_b regular file')
    parser.add_argument('--patched-lk', type=Path, required=True, help='Exact reviewed 864,000-byte patched prefix')
    parser.add_argument('--out', type=Path, required=True, help='New directory under /rabbitr1; existing parent required')
    args = parser.parse_args(argv)
    try:
        record = prepare_files(args.lk_a, args.lk_b, args.patched_lk, args.out)
    except (ValueError, OSError) as error:
        parser.exit(1, 'LK preparation failed: '+str(error)+'\n')
    print(json.dumps({'output': str(local(args.out)), 'scope': record['scope'],
                      'images': record['images']}, indent=2))


if __name__ == '__main__':
    main()
