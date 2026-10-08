#!/usr/bin/env python3
"""Package experimental r1 boot images with the stock v2/table layout."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile
from lk_handoff import has_display_guard

ROOT = Path('/rabbitr1')
OUT = ROOT/'out/package'
DIST = ROOT/'dist/mtkclient'
KERNEL = ROOT/'dist/mainline'
STOCK = ROOT/'firmware/stock-v0.8.293'
BOOT_SIZE = 32 * 1024 * 1024
DTBO_SIZE = 8 * 1024 * 1024


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def dt_table(blob):
    # Same single-entry v0 layout and IDs as the supplied stock firmware.
    return struct.pack('>16I', 0xd7b7ab1e, 64 + len(blob), 32, 32, 1, 32, 2048, 0,
                       len(blob), 64, 0, 0, 0, 0, 0, 0) + blob


def pad(path, size):
    if path.stat().st_size > size:
        raise ValueError('Partition overflow: ' + str(path))
    with path.open('ab') as stream:
        stream.write(b'\0' * (size - stream.tell()))


def main():
    os.environ.update(TMPDIR=str(ROOT/'.tmp'), PYTHONDONTWRITEBYTECODE='1')
    OUT.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    record = json.loads((KERNEL/'build.json').read_text())
    for name in ['Image', 'Image.gz', 'mt6765-rabbit-r1.dtb', 'config']:
        if sha(KERNEL/name) != record['artifacts'][name]['sha256']:
            raise ValueError('Kernel build hash mismatch: ' + name)
    for name in ['pstore_zone', 'pstore_blk']:
        path = f'fs/pstore/{name}.ko'
        if sha(ROOT/'out/mainline'/path) != record['modules'][path]:
            raise ValueError('Rebuild matching pstore modules')
    initramfs = ROOT/'dist/bringup/initramfs.cpio.gz'
    expected = (initramfs.parent/'SHA256SUMS').read_text().split()[0]
    if sha(initramfs) != expected:
        raise ValueError('Initramfs checksum mismatch')
    kernel = gzip.decompress((KERNEL/'Image.gz').read_bytes())
    if kernel[56:60] != b'ARM\x64':
        raise ValueError('Expected AArch64 Image')
    # Decompressed kernel, FDT and ramdisk load areas must not overlap.
    if 0x40080000 + len(kernel) >= 0x47880000:
        raise ValueError('Kernel reaches the DTB load area')

    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    lk_record = json.loads((ROOT/'dist/lk/build.json').read_text())
    if lk_record.get('relock_protection', {}).get('enabled') is not True:
        raise ValueError('Rebuild LK with relock protection before packaging')
    if lk_record.get('kernel_mmc_pinctrl_preserved') is not True or \
            (ROOT/'dist/lk/lk.bin').read_bytes()[0x1c290:0x1c294] != bytes.fromhex('002000bf'):
        raise ValueError('Rebuild LK to preserve the mainline MMC pin states')
    if lk_record.get('kernel_scp_fixup_bypassed') is not True or \
            (ROOT/'dist/lk/lk.bin').read_bytes()[0x4a44:0x4a48] != bytes.fromhex('002000bf'):
        raise ValueError('Rebuild LK to skip the vendor SCP node requirement')
    if lk_record.get('kernel_console_preserved') is not True or \
            (ROOT/'dist/lk/lk.bin').read_bytes()[0x1cb70:0x1cb74] != bytes.fromhex('002000bf'):
        raise ValueError('Rebuild LK to preserve the mainline console')
    if lk_record.get('kernel_display_guard') is not True or not has_display_guard(
            (ROOT/'dist/lk/lk.bin').read_bytes()):
        raise ValueError('Rebuild LK with the checked display handoff')
    if lk_record['stock_lk_sha256'] != sha(STOCK/'lk.img'):
        raise ValueError('LK build uses a different stock image')
    for name, facts in lk_record['files'].items():
        path = ROOT/'dist/lk'/name
        if path.stat().st_size != facts['bytes'] or sha(path) != facts['sha256']:
            raise ValueError('LK build hash mismatch: ' + name)
    # LK still needs the complete stock overlay for its private board DT.
    shutil.copyfile(STOCK/'dtbo.img', DIST/'dtbo.img')
    pad(DIST/'dtbo.img', DTBO_SIZE)
    for name in ['lk.bin', 'logo.bin', 'splash.png']:
        shutil.copyfile(ROOT/'dist/lk'/name, DIST/name)
    profiles = {}
    for profile in ['ram', 'expdb']:
        dtb = OUT/f'{profile}.dtb'
        shutil.copyfile(KERNEL/'mt6765-rabbit-r1.dtb', dtb)
        if profile == 'expdb':
            run('fdtput', '-t', 's', dtb, '/soc/mmc@11230000', 'status', 'okay')
        nodes = validate.fdt_nodes(dtb.read_bytes())
        expected_status = b'okay\0' if profile == 'expdb' else b'disabled\0'
        if nodes['/soc/mmc@11230000']['status'] != expected_status:
            raise ValueError('Wrong eMMC state')
        if profile == 'expdb':
            ram_nodes = validate.fdt_nodes((OUT/'ram.dtb').read_bytes())
            ram_nodes['/soc/mmc@11230000']['status'] = b'okay\0'
            if nodes != ram_nodes:
                raise ValueError('Unexpected difference between boot profiles')
        table = OUT/f'{profile}-dtb.img'
        table.write_bytes(dt_table(dtb.read_bytes()))
        cmdline = ('bootopt=64S3,32N2,64N2 buildvariant=userdebug '
                   'earlycon=uart8250,mmio32,0x11002000 console=ttyS0,921600n8 '
                   'maxcpus=1 clk_ignore_unused regulator_ignore_unused '
                   'rdinit=/init loglevel=8 ignore_loglevel r1.usb=1 '
                   f'r1.expdb={int(profile == "expdb")}')
        image = DIST/f'boot-{profile}.img'
        run(sys.executable, ROOT/'src/mkbootimg/mkbootimg.py',
            '--header_version', '2', '--pagesize', '2048', '--base', '0',
            '--kernel_offset', '0x40080000', '--ramdisk_offset', '0x51b00000',
            '--tags_offset', '0x47880000', '--dtb_offset', '0x47880000',
            '--os_version', '12.0.0', '--os_patch_level', '2023-07',
            '--kernel', KERNEL/'Image.gz', '--ramdisk', initramfs,
            '--dtb', table, '--cmdline', cmdline, '--output', image)
        pad(image, BOOT_SIZE)
        profiles[profile] = {'image': image.name, 'emmc': profile == 'expdb',
                             'expdb_logging': profile == 'expdb', 'cmdline': cmdline,
                             'dtb_sha256': sha(dtb)}
    for name in ['prepare-flash.py', 'decode-expdb.py', 'lk_handoff.py']:
        shutil.copyfile(ROOT/'scripts'/name, DIST/name)
    shutil.copyfile(ROOT/'docs/FLASHING.md', DIST/'README.md')
    manifest = {
        'format': 2, 'device': 'rabbit r1', 'status': 'experimental; not boot-tested',
        'source_commit': record['source_commit'], 'kernel_release': record['kernel_release'],
        'kernel_build': record, 'profiles': profiles, 'lk_build': lk_record,
        'initramfs_sha256': sha(initramfs),
        'stock_reference': 'RabbitOS v0.8.293',
        'stock_lk_sha256': sha(STOCK/'lk.img'),
        'stock_lk_bytes': (STOCK/'lk.img').stat().st_size,
        'stock_logo_sha256': sha(STOCK/'logo.bin'),
        'stock_logo_bytes': (STOCK/'logo.bin').stat().st_size,
        'stock_dtbo_sha256': sha(STOCK/'dtbo.img'),
        'stock_dtbo_bytes': (STOCK/'dtbo.img').stat().st_size,
        'partitions': {'boot': BOOT_SIZE, 'dtbo': DTBO_SIZE, 'vbmeta': 8*1024*1024,
                       'lk': 1024*1024, 'logo': 11*1024*1024, 'expdb': 20*1024*1024},
        'logging': {'backend': 'pstore_blk', 'mapped_bytes': 18*1024*1024,
                    'preserved_tail_bytes': 2*1024*1024, 'panic_safe': False},
        'files': {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p)}
                  for p in sorted(DIST.iterdir()) if p.name not in ['manifest.json', 'SHA256SUMS']},
    }
    (DIST/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (DIST/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in sorted(DIST.iterdir())
                                        if p.name != 'SHA256SUMS'))
    output = ROOT/'dist/rabbit-r1-mainline-mtkclient.zip'
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(DIST.iterdir()):
            entry = zipfile.ZipInfo('rabbit-r1-mainline/'+path.name, (2026, 10, 7, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes())
    output.with_suffix('.zip.sha256').write_text(f'{sha(output)}  {output.name}\n')
    print(output, output.stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
