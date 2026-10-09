#!/usr/bin/env python3
"""Receive explicitly planned, bounded, read-only ADB backups. Linux host only."""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

GIB = 1024 ** 3
MIB = 1024 ** 2
MAX_STDERR = 64 * 1024
MAX_PLAN = 128 * 1024
MIN_RESERVE = GIB


class Failure(Exception):
    pass


def require(value, reason):
    if not value:
        raise Failure(reason)


def exact_keys(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys.split()),
            'unexpected/missing keys: ' + label)


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, 'invalid ' + label)
    return value


def no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, 'duplicate JSON key: ' + key)
        obj[key] = value
    return obj


def load_plan(path):
    with open_regular(path) as source:
        data = source.read(MAX_PLAN + 1)
    require(len(data) <= MAX_PLAN, 'plan too large')
    plan = json.loads(data, object_pairs_hook=no_duplicates)
    validate_plan(plan)
    return plan, hashlib.sha256(data).hexdigest()


def validate_plan(p):
    exact_keys(p, 'schema serial cid disk excluded_nodes reserve_bytes timeout_seconds idle_seconds reads', 'plan')
    require(p['schema'] == 1 and type(p['schema']) is int, 'unsupported schema')
    require(isinstance(p['serial'], str) and re.fullmatch(r'[A-Za-z0-9]{1,64}', p['serial']), 'invalid serial')
    require(isinstance(p['cid'], str) and re.fullmatch(r'[0-9a-f]{32}', p['cid']), 'invalid lowercase CID')
    d = p['disk']
    exact_keys(d, 'node bytes logical_sector_bytes major minor', 'disk')
    require(isinstance(d['node'], str) and re.fullmatch(r'/dev/block/mmcblk[0-9]+', d['node']), 'invalid disk node')
    integer(d['bytes'], 4 * MIB, 2 ** 50, 'disk bytes')
    require(d['logical_sector_bytes'] in (512, 1024, 2048, 4096)
            and type(d['logical_sector_bytes']) is int, 'invalid logical sector size')
    require(d['bytes'] % d['logical_sector_bytes'] == 0, 'unaligned disk size')
    for k in ('major', 'minor'):
        integer(d[k], 0, 2 ** 20 - 1, 'disk ' + k)
    integer(p['reserve_bytes'], MIN_RESERVE, 2 ** 50, 'reserve bytes')
    integer(p['timeout_seconds'], 1, 24 * 3600, 'timeout')
    integer(p['idle_seconds'], 1, 3600, 'idle timeout')
    require(p['idle_seconds'] <= p['timeout_seconds'], 'idle exceeds total timeout')
    part = re.escape(d['node']) + r'p[1-9][0-9]*'
    require(isinstance(p['excluded_nodes'], list) and p['excluded_nodes']
            and all(isinstance(x, str) and re.fullmatch(part, x) for x in p['excluded_nodes'])
            and len(set(p['excluded_nodes'])) == len(p['excluded_nodes']), 'invalid excluded partition nodes')
    require(isinstance(p['reads'], list) and 1 <= len(p['reads']) <= 128, 'invalid read list')
    names, intervals = set(), {}
    for r in p['reads']:
        exact_keys(r, 'name kind node bytes start_sector major minor offset length', 'read')
        require(isinstance(r['name'], str) and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', r['name'])
                and r['name'] not in names and 'userdata' not in r['name'], 'invalid/duplicate/excluded name')
        names.add(r['name'])
        require(isinstance(r['node'], str) and r['node'] not in p['excluded_nodes'], 'excluded node')
        integer(r['bytes'], 512, 2 ** 50, 'node bytes')
        integer(r['offset'], 0, r['bytes'] - 1, 'read offset')
        integer(r['length'], 1, r['bytes'], 'read length')
        require(r['offset'] + r['length'] <= r['bytes'], 'read exceeds node')
        require(all(r[k] % d['logical_sector_bytes'] == 0 for k in ('bytes', 'offset', 'length')), 'unaligned read')
        for k in ('major', 'minor'):
            integer(r[k], 0, 2 ** 20 - 1, 'node ' + k)
        if r['kind'] == 'gpt':
            require(r['node'] == d['node'] and r['bytes'] == d['bytes']
                    and r['major'] == d['major'] and r['minor'] == d['minor']
                    and r['start_sector'] is None, 'GPT disk identity mismatch')
            require(r['length'] <= MIB and (r['offset'] + r['length'] <= MIB
                    or r['offset'] >= r['bytes'] - MIB), 'GPT window exceeds first/last MiB')
        elif r['kind'] == 'partition':
            require(re.fullmatch(part, r['node']), 'invalid partition node')
            integer(r['start_sector'], 1, d['bytes'] // 512 - 1, 'partition start sector')
            require(r['start_sector'] * 512 + r['bytes'] <= d['bytes'], 'partition exceeds disk')
            require(r['offset'] == 0 and r['length'] == r['bytes'], 'partition read must be complete')
        elif r['kind'] == 'boot':
            require(r['node'] in (d['node'] + 'boot0', d['node'] + 'boot1')
                    and r['start_sector'] is None, 'invalid boot area')
            require(r['offset'] == 0 and r['length'] == r['bytes'], 'boot read must be complete')
        else:
            raise Failure('unknown read kind; RPMB never allowed')
        for a, b in intervals.get(r['node'], []):
            require(r['offset'] + r['length'] <= a or b <= r['offset'], 'overlapping duplicate read')
        intervals.setdefault(r['node'], []).append((r['offset'], r['offset'] + r['length']))


def guard_script(p, r):
    # Every interpolated value passed strict node/integer/hex/alphanumeric validation.
    d = p['disk']
    disk = '/sys/class/block/' + Path(d['node']).name
    node = '/sys/class/block/' + Path(r['node']).name
    checks = [('id -u', '0', 'root'), ('getprop ro.serialno', p['serial'], 'serial'),
              ('cat ' + disk + '/device/cid', p['cid'], 'CID'),
              ('cat ' + disk + '/size', str(d['bytes'] // 512), 'disk size'),
              ('cat ' + disk + '/queue/logical_block_size', str(d['logical_sector_bytes']), 'sector size'),
              ('cat ' + disk + '/dev', f"{d['major']}:{d['minor']}", 'disk device number'),
              ('cat ' + node + '/size', str(r['bytes'] // 512), 'node size'),
              ('cat ' + node + '/dev', f"{r['major']}:{r['minor']}", 'node device number'),
              ("stat -c '%t:%T' " + r['node'], f"{r['major']:x}:{r['minor']:x}", 'node stat')]
    if r['kind'] == 'partition':
        checks.append(('cat ' + node + '/start', str(r['start_sector']), 'partition start'))
    lines = ['set -eu']
    for command, expected, label in checks:
        lines.append('test "$(' + command + ')" = ' + shlex.quote(expected)
                     + ' || { echo ' + shlex.quote('backup identity mismatch: ' + label) + ' >&2; exit 80; }')
    lines.append('test -b ' + r['node'] + ' && ! test -L ' + r['node']
                 + " || { echo 'backup node is not a direct block device' >&2; exit 81; }")
    lines.append('exec dd if=' + r['node'] + ' bs=1048576 skip=' + str(r['offset'])
                 + ' count=' + str(r['length']) + ' iflag=count_bytes,skip_bytes status=none')
    return '\n'.join(lines)


def adb_base(adb, p):
    return [str(adb), '-H', '127.0.0.1', '-P', '5037', '-s', p['serial']]


def read_command(adb, p, r):
    # adb shell joins remaining argv with spaces (unlike exec-out). Quote sh -c's
    # script once for the remote shell; shell -T mandates shell-v2, without PTY.
    return adb_base(adb, p) + ['shell', '-T', 'sh -c ' + shlex.quote(guard_script(p, r))]


def absolute(path):
    p = Path(path)
    require(p.is_absolute() and '..' not in p.parts, 'absolute non-traversing path required')
    return p


def directory(path):
    p = absolute(path)
    for ancestor in reversed((p,) + tuple(p.parents)):
        mode = ancestor.lstat().st_mode
        require(stat.S_ISDIR(mode), 'non-directory/symlink component: ' + str(ancestor))
    return p


def open_regular(path):
    path = absolute(path)
    directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'non-regular file: ' + str(path))
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def file_hash(path):
    h = hashlib.sha256()
    with open_regular(path) as f:
        while data := f.read(MIB):
            h.update(data)
    return h.hexdigest()


def free_bytes(path):
    v = os.statvfs(path)
    return v.f_bavail * v.f_frsize


def check_space(path, reserve, additional=0):
    n = free_bytes(path)
    require(n >= reserve + additional, 'host free-space reserve reached')
    return n


def stop_group(child):
    # Also terminate descendants retaining pipe fds after the leader exits.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        n = os.write(fd, remaining)
        require(n > 0, 'host write made no progress')
        remaining = remaining[n:]


def transfer(command, limit, timeout, idle, directory_path, reserve, output_fd=None, exact=False):
    start = progress = time.monotonic()
    minimum = check_space(directory_path, reserve)
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    digest, stderr, stdout = hashlib.sha256(), bytearray(), bytearray()
    received = 0
    eof = {'stdout': False, 'stderr': False}
    try:
        with selectors.DefaultSelector() as selector:
            for name, pipe in [('stdout', child.stdout), ('stderr', child.stderr)]:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while selector.get_map() or child.poll() is None:
                now = time.monotonic()
                require(now - start < timeout, 'total process timeout')
                require(now - progress < idle, 'stdout progress timeout')
                minimum = min(minimum, check_space(directory_path, reserve))
                for key, _ in selector.select(0.2):
                    try:
                        data = os.read(key.fd, MIB if key.data == 'stdout' else 8192)
                    except BlockingIOError:
                        continue
                    if not data:
                        eof[key.data] = True
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == 'stderr':
                        require(len(stderr) + len(data) <= MAX_STDERR, 'stderr exceeded bounded capture')
                        stderr.extend(data)
                        continue
                    progress = time.monotonic()
                    require(received + len(data) <= limit, 'extra stdout bytes')
                    minimum = min(minimum, check_space(directory_path, reserve, len(data)))
                    if output_fd is not None:
                        write_all(output_fd, data)
                    else:
                        stdout.extend(data)
                    digest.update(data)
                    received += len(data)
            exit_code = child.wait(timeout=1)
        require(exit_code == 0, 'ADB/remote process exited ' + str(exit_code) + ': '
                + bytes(stderr).decode('utf-8', 'replace')[:4096])
        require(all(eof.values()), 'incomplete pipe EOF')
        require(not exact or received == limit, 'short stdout bytes')
        return {'bytes': received, 'sha256': digest.hexdigest(), 'stderr_hex': bytes(stderr).hex(),
                'exit_code': exit_code, 'eof': eof, 'elapsed_seconds': time.monotonic() - start,
                'minimum_free_bytes': minimum}, bytes(stdout)
    except BaseException:
        stop_group(child)
        raise
    finally:
        child.stdout.close()
        child.stderr.close()


def exclusive_json(dirfd, name, obj):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    try:
        write_all(fd, (json.dumps(obj, indent=2) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_noreplace(parentfd, source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(parentfd, os.fsencode(source), parentfd, os.fsencode(target), 1) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def check_destination(workspace, output):
    workspace = directory(workspace)
    output = absolute(output)
    require(output != workspace and output.is_relative_to(workspace), 'output outside workspace')
    directory(output.parent)
    require(not os.path.lexists(output) and not os.path.lexists(str(output) + '.partial'),
            'output or private partial already exists; retry/resume forbidden')
    return output


def receive(plan, plan_sha, output, adb):
    parentfd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    partial = output.with_name(output.name + '.partial')
    dirfd = None
    manifest = {'schema': 1, 'status': 'incomplete', 'plan': plan, 'plan_sha256': plan_sha,
                'receiver_sha256': file_hash(Path(__file__).absolute()), 'adb_sha256': file_hash(adb),
                'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'read_only_device_commands': True, 'userdata_excluded_by_reviewed_plan': True,
                'snapshot_consistency_proven': False, 'entries': []}
    try:
        check_space(output.parent, plan['reserve_bytes'], sum(r['length'] for r in plan['reads']) + 16 * MIB)
        os.mkdir(partial.name, 0o700, dir_fd=parentfd)
        dirfd = os.open(partial.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parentfd)
        os.fsync(parentfd)
        record, features = transfer(adb_base(adb, plan) + ['features'], 16384,
                                    min(30, plan['timeout_seconds']), min(15, plan['idle_seconds']),
                                    partial, plan['reserve_bytes'])
        require('shell_v2' in re.split(r'[,\s]+', features.decode('ascii').strip()), 'ADB shell_v2 required')
        manifest['features'] = {'text': features.decode('ascii'), 'process': record}
        for r in plan['reads']:
            name = r['name'] + '.img'
            temporary = name + '.partial'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=dirfd)
            try:
                cmd = read_command(adb, plan, r)
                info, _ = transfer(cmd, r['length'], plan['timeout_seconds'], plan['idle_seconds'],
                                   partial, plan['reserve_bytes'], fd, exact=True)
                os.fsync(fd)
                require(os.fstat(fd).st_size == r['length'], 'host size mismatch')
            finally:
                os.close(fd)
            # Independent host reread after close/fsync; no device reread/retry.
            reread = file_hash(partial / temporary)
            require(reread == info['sha256'], 'host reread hash mismatch')
            os.link(temporary, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=dirfd)
            os.fsync(dirfd)
            manifest['entries'].append({'read': r, 'file': name, 'command': cmd,
                                        'process': info, 'host_reread_sha256': reread})
            print(json.dumps({'status': 'entry-verified-private', 'name': r['name'],
                              'bytes': info['bytes'], 'sha256': reread}), flush=True)
        require(file_hash(adb) == manifest['adb_sha256'], 'ADB executable changed')
        require(file_hash(Path(__file__).absolute()) == manifest['receiver_sha256'], 'receiver changed')
        check_space(partial, plan['reserve_bytes'])
        # Last file published is the complete manifest, after all payload fsyncs.
        manifest['status'] = 'complete-exact-byte-read-only-backup'
        manifest['completed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        exclusive_json(dirfd, 'manifest.json', manifest)
        os.fsync(dirfd)
        require(os.stat(output.parent, follow_symlinks=False).st_ino == os.fstat(parentfd).st_ino
                and os.stat(output.parent, follow_symlinks=False).st_dev == os.fstat(parentfd).st_dev,
                'output parent changed')
        require(os.stat(partial.name, dir_fd=parentfd, follow_symlinks=False).st_ino == os.fstat(dirfd).st_ino,
                'private directory changed')
        rename_noreplace(parentfd, partial.name, output.name)
        os.fsync(parentfd)
        return manifest
    except BaseException as exc:
        # Retain partial evidence. Never overwrite, resume or silently retry it.
        if dirfd is not None:
            try:
                exclusive_json(dirfd, 'failure.json', {'status': 'failed', 'error': str(exc),
                                                      'completed_entries': manifest['entries']})
                os.fsync(dirfd)
            except BaseException:
                pass
        raise
    finally:
        if dirfd is not None:
            os.close(dirfd)
        os.close(parentfd)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', required=True, type=Path)
    ap.add_argument('--out', required=True, type=Path)
    ap.add_argument('--workspace', type=Path, default=Path('/opt/rabbit'))
    ap.add_argument('--adb', type=Path)
    ap.add_argument('--execute', action='store_true', help='perform the reviewed read-only plan; default only checks host plan')
    args = ap.parse_args()
    def terminate(signum, frame):
        raise Failure('receiver interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, terminate)
    plan, plan_sha = load_plan(args.plan)
    output = check_destination(args.workspace, args.out)
    adb = args.adb or Path(shutil.which('adb') or '/nonexistent-adb')
    adb = absolute(adb)
    file_hash(adb)
    require(os.access(adb, os.X_OK), 'ADB is not executable')
    check_space(output.parent, plan['reserve_bytes'], sum(r['length'] for r in plan['reads']) + 16 * MIB)
    if not args.execute:
        print(json.dumps({'status': 'host-plan-check-only', 'device_commands_run': False,
                          'plan_sha256': plan_sha, 'bytes': sum(r['length'] for r in plan['reads']),
                          'commands': [read_command(adb, plan, r) for r in plan['reads']]}, indent=2))
        return
    receive(plan, plan_sha, output, adb)
    print(json.dumps({'status': 'complete', 'manifest': str(output / 'manifest.json')}))


if __name__ == '__main__':
    try:
        main()
    except (Failure, OSError, ValueError, KeyboardInterrupt) as exc:
        print('backup failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
