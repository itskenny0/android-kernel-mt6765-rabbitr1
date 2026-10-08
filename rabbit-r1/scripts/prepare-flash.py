#!/usr/bin/env python3
"""Prepare a single-slot mtkclient write from verified device backups. No USB I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import struct
import zlib

ROOT = Path('/rabbitr1')
SIZES = {'boot': 32*1024*1024, 'dtbo': 8*1024*1024, 'vbmeta': 8*1024*1024,
         'lk': 1024*1024, 'logo': 11*1024*1024, 'expdb': 20*1024*1024}


def local(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Paths must stay under /rabbitr1')
    return path


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def parse_gpt(blob):
    # mtkclient gpt.bin includes the protective MBR and primary GPT at LBA 1.
    if len(blob) < 1024 or blob[512:520] != b'EFI PART':
        raise ValueError('Expected a 512-byte-sector primary GPT dump')
    revision, size, checksum, reserved = struct.unpack_from('<4I', blob, 520)
    if revision != 0x10000 or not 92 <= size <= 512 or reserved:
        raise ValueError('Invalid GPT header')
    header = bytearray(blob[512:512+size])
    struct.pack_into('<I', header, 16, 0)
    if zlib.crc32(header) != checksum:
        raise ValueError('GPT header CRC mismatch')
    current, backup, first, last = struct.unpack_from('<4Q', blob, 536)
    entries_lba, count, stride, table_crc = struct.unpack_from('<QIII', blob, 584)
    if current != 1 or not 2 <= entries_lba < first <= last < backup:
        raise ValueError('Invalid GPT bounds')
    if not 1 <= count <= 4096 or stride < 128 or stride % 128:
        raise ValueError('Invalid GPT entry layout')
    start, length = entries_lba*512, count*stride
    if start + length > len(blob) or start + length > first*512:
        raise ValueError('Truncated or overlapping GPT array')
    table = blob[start:start+length]
    if zlib.crc32(table) != table_crc:
        raise ValueError('GPT entry CRC mismatch')
    parts = {}
    intervals = []
    for offset in range(0, length, stride):
        entry = table[offset:offset+stride]
        if entry[:16] == bytes(16):
            continue
        lo, hi = struct.unpack_from('<QQ', entry, 32)
        name = entry[56:128].decode('utf-16-le').split('\0', 1)[0]
        if not name or name in parts or not first <= lo <= hi <= last:
            raise ValueError('Invalid or duplicate GPT partition: ' + name)
        parts[name] = {'start': lo*512, 'bytes': (hi-lo+1)*512}
        intervals.append((lo, hi))
    intervals.sort()
    if any(a[1] >= b[0] for a, b in zip(intervals, intervals[1:])):
        raise ValueError('Overlapping GPT partitions')
    return {'disk_guid': blob[568:584].hex(), 'partitions': parts}


def patch_vbmeta(blob):
    if len(blob) != SIZES['vbmeta'] or blob[:4] != b'AVB0':
        raise ValueError('Expected a complete 8 MiB vbmeta partition with AVB0 header')
    major, minor = struct.unpack_from('>II', blob, 4)
    auth, aux = struct.unpack_from('>QQ', blob, 12)
    if major != 1 or minor > 3 or 256+auth+aux > len(blob):
        raise ValueError('Unsupported or truncated AVB header')
    patched = bytearray(blob)
    flags = struct.unpack_from('>I', blob, 120)[0]
    struct.pack_into('>I', patched, 120, flags | 3)
    return bytes(patched)


WRITE_PARTS = ['boot', 'dtbo', 'vbmeta', 'logo', 'lk']


def partition_name(part, slot):
    return part if part in ['expdb', 'logo'] else f'{part}_{slot}'


def shell_script(out, slot, restore=False):
    """Write the selected slot and shared logo; check GPT and every readback."""
    names = ','.join(partition_name(part, slot) for part in WRITE_PARTS)
    inputs = ','.join(f'{part}-{ "restore" if restore else "new"}.img'
                      for part in WRITE_PARTS)
    mode = 'restore' if restore else 'flash'
    # Everything is prepared locally. Running --write is a separate device action.
    lines = [
        '#!/usr/bin/env bash', 'set -euo pipefail',
        f'cd {shlex.quote(str(out))}',
        f'echo {shlex.quote(mode + " slot " + slot + ": " + names)}',
        'echo "Do not relock until the complete stock firmware package, including every LK slot, is restored."',
        'if [[ ${1:-} != --write ]]; then',
        f'    echo "Preview only. Run bash {mode}.sh --write to perform these writes."',
        f'    echo {shlex.quote("mtk.py w " + names + " " + inputs)}',
        '    exit 0', 'fi',
        'export TMPDIR=/rabbitr1/.tmp XDG_CACHE_HOME=/rabbitr1/.cache',
        'export XDG_CONFIG_HOME=/rabbitr1/.cache/config XDG_DATA_HOME=/rabbitr1/.cache/data',
        'export PYTHONDONTWRITEBYTECODE=1',
        'mtk=(/rabbitr1/toolchains/mtkclient/bin/python /rabbitr1/src/mtkclient/mtk.py)',
        'sha256sum -c SHA256SUMS',
        f'run=$(mktemp -d /rabbitr1/.tmp/r1-{mode}.XXXXXXXX)',
        '"${mtk[@]}" gpt "$run"',
        'python3 prepare-flash.py check-gpt gpt.bin "$run/gpt.bin"',
    ]
    if not restore:
        lines += [
            f'"${{mtk[@]}}" r {names} "$run/boot-before.img,$run/dtbo-before.img,$run/vbmeta-before.img,$run/logo-before.img,$run/lk-before.img"',
            'for part in boot dtbo vbmeta logo lk; do cmp "$part-restore.img" "$run/$part-before.img"; done',
        ]
    lines += [
        f'"${{mtk[@]}}" w {names} {inputs}',
        f'"${{mtk[@]}}" r {names} "$run/boot-readback.img,$run/dtbo-readback.img,$run/vbmeta-readback.img,$run/logo-readback.img,$run/lk-readback.img"',
        'for part in boot dtbo vbmeta logo lk; do',
        f'    cmp "$part-{ "restore" if restore else "new"}.img" "$run/$part-readback.img"',
        'done',
        'echo "Readback matches. The tool has not changed the active slot or rebooted the device."',
        'echo "Device readback files: $run"',
    ]
    return '\n'.join(lines)+'\n'


def prepare(args):
    manifest = json.loads((args.package/'manifest.json').read_text())
    if manifest.get('format') != 2:
        raise ValueError('Requires the LK-aware package format 2')
    if manifest.get('lk_build', {}).get('relock_protection', {}).get('enabled') is not True:
        raise ValueError('Package lacks LK relock protection; rebuild it')
    if manifest.get('lk_build', {}).get('kernel_mmc_pinctrl_preserved') is not True or \
            local(args.package/'lk.bin').read_bytes()[0x1c290:0x1c294] != bytes.fromhex('002000bf'):
        raise ValueError('Package lacks the LK MMC pin-state fix; rebuild it')
    for name, facts in manifest['files'].items():
        if Path(name).name != name:
            raise ValueError('Invalid package filename')
        path = local(args.package/name)
        if path.stat().st_size != facts['bytes'] or sha(path) != facts['sha256']:
            raise ValueError('Package checksum mismatch: ' + name)
    gpt_path = local(args.backup/'gpt.bin')
    gpt = parse_gpt(gpt_path.read_bytes())
    backups = {}
    for part, size in SIZES.items():
        name = partition_name(part, args.slot)
        if gpt['partitions'].get(name, {}).get('bytes') != size:
            raise ValueError('Unexpected live partition layout: ' + name)
        path = local(args.backup/(name+'.img'))
        if path.stat().st_size != size:
            raise ValueError('Missing or truncated device backup: ' + name)
        backups[part] = path
    with backups['boot'].open('rb') as stream:
        if stream.read(8) != b'ANDROID!':
            raise ValueError('Selected slot has no Android boot header; inspect the backup')
    with backups['dtbo'].open('rb') as stream:
        if stream.read(4) != b'\xd7\xb7\xab\x1e':
            raise ValueError('Selected slot has no Android DT table; inspect the backup')
    for part in ['lk', 'logo', 'dtbo']:
        reference = backups[part].read_bytes()[:manifest[f'stock_{part}_bytes']]
        if hashlib.sha256(reference).hexdigest() != manifest[f'stock_{part}_sha256']:
            raise ValueError(part + ' differs from v0.8.293; inspect before packaging')
    lk_payload = (args.package/'lk.bin').read_bytes()
    logo_payload = (args.package/'logo.bin').read_bytes()
    if len(lk_payload) != manifest['stock_lk_bytes'] or len(lk_payload) > SIZES['lk']:
        raise ValueError('Unexpected patched LK size')
    if len(logo_payload) > SIZES['logo']:
        raise ValueError('Logo exceeds partition')
    patched = patch_vbmeta(backups['vbmeta'].read_bytes())
    if not args.bootloader_unlocked:
        raise ValueError('Requires an already unlocked bootloader; vbmeta flags do not unlock it')
    args.out.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(gpt_path, args.out/'gpt.bin')
    shutil.copyfile(Path(__file__), args.out/'prepare-flash.py')
    for part, path in backups.items():
        shutil.copyfile(path, args.out/(part+'-restore.img'))
    shutil.copyfile(args.package/f'boot-{args.profile}.img', args.out/'boot-new.img')
    shutil.copyfile(args.package/'dtbo.img', args.out/'dtbo-new.img')
    (args.out/'vbmeta-new.img').write_bytes(patched)
    # Keep the device's trailing LK partition bytes; payload edits are same-size.
    (args.out/'lk-new.img').write_bytes(lk_payload + backups['lk'].read_bytes()[len(lk_payload):])
    (args.out/'logo-new.img').write_bytes(logo_payload.ljust(SIZES['logo'], b'\0'))
    (args.out/'flash.sh').write_text(shell_script(args.out, args.slot))
    (args.out/'restore.sh').write_text(shell_script(args.out, args.slot, restore=True))
    (args.out/'plan.json').write_text(json.dumps({
        'slot': args.slot, 'profile': args.profile, 'gpt': gpt,
        'write_partitions': [partition_name(p, args.slot) for p in WRITE_PARTS],
        'relock': 'Restore complete stock firmware, including every LK slot, before relocking; this restore script only replays saved backups',
        'shared_logo': 'changes both slots; original retained for restore',
        'lk': 'mainline handoff and warning patches; restore together with stock boot',
        'backup_directory': str(args.backup), 'package_directory': str(args.package),
        'bootloader_unlocked': 'operator assertion; not verified offline',
        'expdb': 'backup retained; only kernel pstore writes it after boot',
        'expdb_restore': 'manual only, after retrieving logs; not done by restore.sh',
        'slot_activation': 'unchanged; choose and verify explicitly before booting',
    }, indent=2)+'\n')
    (args.out/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.name}\n'
        for p in sorted(args.out.iterdir()) if p.name != 'SHA256SUMS'))
    print('Prepared:', args.out)
    print('No device was accessed. Preview: bash', args.out/'flash.sh')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--package', type=local, default=Path(__file__).resolve().parent)
    p.add_argument('--backup', type=local, required=True)
    p.add_argument('--out', type=local, required=True)
    p.add_argument('--slot', choices=['a', 'b'], required=True)
    p.add_argument('--profile', choices=['ram', 'expdb'], default='expdb')
    p.add_argument('--bootloader-unlocked', action='store_true')
    p = commands.add_parser('check-gpt')
    p.add_argument('original', type=local)
    p.add_argument('current', type=local)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args)
    elif parse_gpt(args.original.read_bytes()) != parse_gpt(args.current.read_bytes()):
        raise ValueError('Live GPT/device identity differs from backup')
    else:
        print('Live GPT identity and partition layout match the backup')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
