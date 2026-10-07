#!/usr/bin/env python3
"""Extract this build's pstore console/pmsg rings from an mtkclient expdb dump."""
import argparse
from pathlib import Path
import struct

ROOT = Path('/rabbitr1')
EXPDB_SIZE = 20 * 1024 * 1024
SIGNATURE = 0x43474244


def decode_ring(blob, offset, size, kind):
    if offset < 0 or size < 12 or offset + size > len(blob):
        raise ValueError('Truncated pstore zone')
    sig, length, start = struct.unpack_from('<Iii', blob, offset)
    if sig != SIGNATURE ^ kind:
        return None
    capacity = size - 12
    if not 0 <= start <= length <= capacity:
        raise ValueError('Corrupt pstore ring bounds')
    if length < capacity and start != length:
        raise ValueError('Invalid partial pstore ring')
    data = blob[offset + 12:offset + 12 + length]
    return data[start:] + data[:start]


def local(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Paths must stay under /rabbitr1')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dump', type=local)
    parser.add_argument('--out', required=True, type=local)
    args = parser.parse_args()
    if args.dump.stat().st_size != EXPDB_SIZE:
        raise SystemExit('Expected the complete 20 MiB expdb partition')
    blob = args.dump.read_bytes()
    # This is the fixed layout used by initramfs/r1-log-start, Linux zone.c.
    rings = [('pmsg', 0, 64 * 1024, 7),
             ('console', 64 * 1024, 1024 * 1024, 2)]
    decoded = [(name, decode_ring(blob, offset, size, kind))
               for name, offset, size, kind in rings]
    if all(data is None for _, data in decoded):
        raise SystemExit('No r1 pstore rings found; not a stock MediaTek AEE decoder')
    args.out.mkdir(parents=True, exist_ok=False)
    for name, data in decoded:
        if data is not None:
            (args.out / (name + '.log')).write_bytes(data)
            print(name, len(data), 'bytes')


if __name__ == '__main__':
    main()
