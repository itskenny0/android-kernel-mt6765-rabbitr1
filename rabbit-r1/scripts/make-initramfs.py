#!/usr/bin/env python3
"""Create a deterministic newc archive without host root privileges or mknod."""
import gzip
import hashlib
import os
from pathlib import Path
import stat

ROOT = Path('/rabbitr1')
epoch = int(os.environ['SOURCE_DATE_EPOCH'])
payload = bytearray()
inode = 0

def entry(name, mode, data=b'', major=0, minor=0):
    global inode
    inode += 1
    name = name.encode() + b'\0'
    fields = [inode, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, epoch,
              len(data), 0, 0, major, minor, len(name), 0]
    payload.extend(b'070701' + ''.join(f'{v:08x}' for v in fields).encode())
    payload.extend(name)
    payload.extend(b'\0' * (-len(payload) % 4))
    payload.extend(data)
    payload.extend(b'\0' * (-len(payload) % 4))

for name in ['bin', 'dev', 'dev/pts', 'proc', 'sys', 'run', 'lib', 'lib/modules']:
    entry(name, stat.S_IFDIR | 0o755)
entry('dev/console', stat.S_IFCHR | 0o600, major=5, minor=1)
entry('dev/null', stat.S_IFCHR | 0o666, major=1, minor=3)
entry('bin/busybox', stat.S_IFREG | 0o755, (ROOT/'out/busybox/busybox').read_bytes())
for name in ['sh','mount','mkdir','uname','cat','echo','sleep','getty','ln','setsid',
             'cttyhack','tr','ls','dmesg','printf','grep','insmod','sync','dd','tail',
             'poweroff','rmmod']:
    entry('bin/'+name, stat.S_IFLNK | 0o777, b'busybox')
entry('init', stat.S_IFREG | 0o755, (ROOT/'initramfs/init').read_bytes())
entry('bin/r1-report', stat.S_IFREG | 0o755, (ROOT/'initramfs/r1-report').read_bytes())
entry('bin/r1-log-start', stat.S_IFREG | 0o755, (ROOT/'initramfs/r1-log-start').read_bytes())
entry('bin/r1-log-shutdown', stat.S_IFREG | 0o755, (ROOT/'initramfs/r1-log-shutdown').read_bytes())
entry('bin/expdb-map', stat.S_IFREG | 0o755, (ROOT/'out/busybox/expdb-map').read_bytes())
entry('bin/expdb-checkpoint', stat.S_IFREG | 0o755, (ROOT/'out/busybox/expdb-checkpoint').read_bytes())
for name in ['pstore_zone', 'pstore_blk']:
    entry('lib/modules/'+name+'.ko', stat.S_IFREG | 0o644,
          (ROOT/'out/mainline/fs/pstore'/(name+'.ko')).read_bytes())
entry('TRAILER!!!', 0)
payload.extend(b'\0' * (-len(payload) % 512))
output = ROOT/'dist/bringup/initramfs.cpio.gz'
output.write_bytes(gzip.compress(payload, mtime=0))
(output.parent/'SHA256SUMS').write_text(hashlib.sha256(output.read_bytes()).hexdigest()+'  '+output.name+'\n')
print(output, output.stat().st_size, 'bytes; contains an unauthenticated development console')
