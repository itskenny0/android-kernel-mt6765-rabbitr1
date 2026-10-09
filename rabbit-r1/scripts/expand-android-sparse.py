#!/usr/bin/env python3
"""Validate Android sparse v1 and atomically expand it for raw mtkclient writes.

Only regular files under /rabbitr1 are accepted. DONT_CARE blocks become zero
bytes (host filesystem holes), so the expanded hash defines every flashed byte.
No device data is read or written, and an existing output is never replaced.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import zlib

ROOT = Path('/rabbitr1')
QUANTUM = 1024 * 1024
ZERO = bytes(QUANTUM)
HEADER = struct.Struct('<IHHHHIIII')
CHUNK = struct.Struct('<HHII')
RAW, FILL, SKIP, CRC = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4


def confined(value):
    path = Path(os.path.abspath(value))
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError('Path must be inside /rabbitr1')
    return path


class ParentDirectory:
    """Pin every path component beneath the workspace without following links."""

    def __init__(self, path):
        self.fds = []
        self.names = path.relative_to(ROOT).parts[:-1]
        try:
            self.fds.append(os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY |
                                    os.O_NOFOLLOW | os.O_CLOEXEC))
            for name in self.names:
                parent = self.fds[-1]
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY |
                                    os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                except OSError:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise ValueError('Symlink directory components are not accepted')
                    raise
                self.fds.append(child)
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        return self.fds[-1]

    def verify(self):
        root = os.stat(ROOT, follow_symlinks=False)
        pinned = os.fstat(self.fds[0])
        if not stat.S_ISDIR(root.st_mode) or (root.st_dev, root.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError('Workspace directory changed while converting')
        for parent, name, child in zip(self.fds, self.names, self.fds[1:]):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(child)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise ValueError('Parent directory changed while converting')

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds = []


def create_temporary(parent_fd):
    for _ in range(100):
        name = '.r1-raw-' + secrets.token_hex(16)
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
            return fd, name
        except FileExistsError:
            pass
    raise FileExistsError('Could not create a unique raw-image temporary file')


def identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def expand(source, *, expected_size, expected_sha256, output=None):
    if not isinstance(expected_size, int) or not 0 < expected_size < 2**63:
        raise ValueError('Expected raw partition size must be positive signed64')
    if not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('Expected sparse SHA256 must be 64 lowercase hex digits')
    source = confined(source)
    output = confined(output) if output is not None else None
    input_hash, raw_hash = hashlib.sha256(), hashlib.sha256()
    crc = raw_bytes = read_bytes = 0
    counts = {name: 0 for name in ('raw', 'fill', 'dont_care', 'crc32')}
    temporary = None
    sink = None
    source_parent = output_parent = None
    fd = None
    try:
        source_parent = ParentDirectory(source)
        source_info = os.stat(source.name, dir_fd=source_parent.fd, follow_symlinks=False)
        if stat.S_ISLNK(source_info.st_mode):
            raise ValueError('Symlink inputs are not accepted')
        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError('Sparse input must be a regular file')
        if output is not None:
            output_parent = ParentDirectory(output)
            try:
                output_info = os.stat(output.name, dir_fd=output_parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(output_info.st_mode):
                    raise ValueError('Symlink outputs are not accepted')
                raise FileExistsError(output)
        fd = os.open(source.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW |
                     os.O_NONBLOCK, dir_fd=source_parent.fd)
        stream = os.fdopen(fd, 'rb')
        fd = None
        with stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise ValueError('Sparse input must be a regular file')

            def read(size):
                nonlocal read_bytes
                if not 0 <= size <= initial.st_size - read_bytes:
                    raise ValueError('Truncated sparse image')
                data = stream.read(size)
                if len(data) != size:
                    raise ValueError('Short sparse-image read')
                input_hash.update(data)
                read_bytes += size
                return data

            def emit(data, hole=False):
                nonlocal crc, raw_bytes
                if len(data) > expected_size - raw_bytes:
                    raise ValueError('Expanded image exceeds partition size')
                crc = zlib.crc32(data, crc)
                raw_hash.update(data)
                raw_bytes += len(data)
                if sink is not None:
                    if hole:
                        sink.seek(len(data), os.SEEK_CUR)
                    elif sink.write(data) != len(data):
                        raise OSError('Short raw-image write')

            fields = HEADER.unpack(read(HEADER.size))
            magic, major, minor, header_size, chunk_size, block_size, blocks, chunks, checksum = fields
            if magic != 0xED26FF3A or major != 1:
                raise ValueError('Expected Android sparse major version 1')
            if header_size < HEADER.size or chunk_size < CHUNK.size:
                raise ValueError('Sparse headers are too short')
            if not block_size or block_size % 4 or blocks * block_size != expected_size:
                raise ValueError('Sparse geometry differs from the expected partition size')
            read(header_size - HEADER.size)
            if chunks > (initial.st_size - read_bytes) // chunk_size:
                raise ValueError('Chunk count exceeds available headers')
            if output is not None:
                handle, temporary = create_temporary(output_parent.fd)
                try:
                    sink = os.fdopen(handle, 'w+b')
                except BaseException:
                    os.close(handle)
                    raise
            for _ in range(chunks):
                kind, reserved, count, stored = CHUNK.unpack(read(CHUNK.size))
                read(chunk_size - CHUNK.size)
                if reserved or stored < chunk_size:
                    raise ValueError('Invalid sparse chunk header')
                length = count * block_size
                payload = stored - chunk_size
                if kind != CRC and length > expected_size - raw_bytes:
                    raise ValueError('Chunk exceeds remaining partition bytes')
                if kind == RAW:
                    counts['raw'] += 1
                    if payload != length:
                        raise ValueError('RAW chunk length mismatch')
                    while length:
                        data = read(min(length, QUANTUM))
                        emit(data, data.count(0) == len(data))
                        length -= len(data)
                elif kind in (FILL, SKIP):
                    counts['fill' if kind == FILL else 'dont_care'] += 1
                    if payload != (4 if kind == FILL else 0):
                        raise ValueError('FILL/DONT_CARE payload length mismatch')
                    pattern = read(4) if kind == FILL else b'\0' * 4
                    hole = pattern == b'\0' * 4
                    repeated = ZERO if hole else pattern * (QUANTUM // 4)
                    while length:
                        data = repeated[:min(length, QUANTUM)]
                        emit(data, hole)
                        length -= len(data)
                elif kind == CRC:
                    counts['crc32'] += 1
                    if count or payload != 4:
                        raise ValueError('Invalid CRC32 chunk shape')
                    if struct.unpack('<I', read(4))[0] != crc:
                        raise ValueError('Sparse chunk CRC32 mismatch')
                else:
                    raise ValueError('Unknown sparse chunk type')
            if raw_bytes != expected_size or read_bytes != initial.st_size:
                raise ValueError('Incomplete raw image or trailing sparse data')
            if checksum and checksum != crc:
                raise ValueError('Sparse image header CRC32 mismatch')
            if input_hash.hexdigest() != expected_sha256:
                raise ValueError('Sparse input SHA256 mismatch')
            if identity(initial) != identity(os.fstat(stream.fileno())):
                raise ValueError('Sparse input changed while reading')
            source_parent.verify()
            current_source = os.stat(source.name, dir_fd=source_parent.fd, follow_symlinks=False)
            if identity(initial) != identity(current_source) or not stat.S_ISREG(current_source.st_mode):
                raise ValueError('Sparse input path changed while reading')
            if sink is not None:
                sink.truncate(expected_size)
                sink.flush()
                os.fsync(sink.fileno())
                sink.close()
                sink = None
                output_parent.verify()
                # Same-filesystem hard link publishes a complete file atomically,
                # with EEXIST protecting any output created after preflight.
                os.link(temporary, output.name, src_dir_fd=output_parent.fd,
                        dst_dir_fd=output_parent.fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=output_parent.fd)
                temporary = None
            return {'input': str(source), 'sparse_bytes': read_bytes,
                    'sparse_sha256': input_hash.hexdigest(), 'raw_bytes': raw_bytes,
                    'raw_sha256': raw_hash.hexdigest(), 'raw_crc32': f'{crc:08x}',
                    'block_size': block_size, 'minor_version': minor,
                    'chunks': counts, 'output': str(output) if output else None,
                    'hardware_accessed': False}
    finally:
        try:
            if sink is not None:
                sink.close()
        finally:
            try:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=output_parent.fd)
                    except FileNotFoundError:
                        pass
            finally:
                if fd is not None:
                    os.close(fd)
                if output_parent is not None:
                    output_parent.close()
                if source_parent is not None:
                    source_parent.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--size', type=int, required=True, help='Expected complete raw partition size')
    parser.add_argument('--sha256', required=True, help='Pinned hash of the sparse input')
    parser.add_argument('--output', type=Path, help='New raw output; omit for validation only')
    args = parser.parse_args()
    try:
        result = expand(args.input, expected_size=args.size,
                        expected_sha256=args.sha256, output=args.output)
    except (OSError, ValueError, struct.error) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
