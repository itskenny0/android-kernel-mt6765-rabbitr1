#!/usr/bin/env python3
"""Validate packaged images and the flash preparer without accessing a device."""
import gzip
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

ROOT = Path('/rabbitr1')
PACKAGE = ROOT/'dist/mtkclient'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flash = load('flash', 'prepare-flash.py')
validate = load('validate', 'validate-kernel.py')


def table(blob):
    header = struct.unpack_from('>8I', blob)
    assert header[0] == 0xd7b7ab1e
    assert header[2:] == (32, 32, 1, 32, 2048, 0)
    size, offset, *ids = struct.unpack_from('>8I', blob, 32)
    assert offset == 64 and not any(ids)
    assert size + offset == header[1] <= len(blob)
    assert not any(blob[header[1]:])
    return blob[offset:offset+size]


def cpio(blob):
    pos, entries = 0, {}
    while True:
        assert blob[pos:pos+6] == b'070701'
        fields = [int(blob[pos+6+i*8:pos+14+i*8], 16) for i in range(13)]
        length, namelen = fields[6], fields[11]
        name = blob[pos+110:pos+110+namelen-1].decode()
        pos = (pos+110+namelen+3) & ~3
        data = blob[pos:pos+length]
        assert len(data) == length
        pos = (pos+length+3) & ~3
        if name == 'TRAILER!!!':
            assert not any(blob[pos:])
            return entries
        assert name not in entries
        entries[name] = data


manifest = json.loads((PACKAGE/'manifest.json').read_text())
assert manifest['lk_build']['relock_protection']['enabled'] is True
for profile in ['ram', 'expdb']:
    blob = (PACKAGE/f'boot-{profile}.img').read_bytes()
    assert len(blob) == 32*1024*1024 and blob[:8] == b'ANDROID!'
    klen, kaddr, rlen, raddr, slen, saddr, tags, page, version, oslevel = struct.unpack_from('<10I', blob, 8)
    assert (kaddr, raddr, slen, saddr, tags, page, version) == (0x40080000, 0x51b00000, 0, 0, 0x47880000, 2048, 2)
    recsize, recoffset, hsize, dtlen, dtaddr = struct.unpack_from('<IQIIQ', blob, 1632)
    assert (recsize, recoffset, hsize, dtaddr) == (0, 0, 1660, 0x47880000)
    stock = (ROOT/'firmware/stock-v0.8.293/boot.img').read_bytes()
    assert oslevel == struct.unpack_from('<I', stock, 44)[0]
    offset = page
    kernel = blob[offset:offset+klen]
    offset += (klen+page-1)//page*page
    ramdisk = blob[offset:offset+rlen]
    offset += (rlen+page-1)//page*page
    dt = blob[offset:offset+dtlen]
    assert not any(blob[offset+dtlen:])
    assert kernel == (ROOT/'dist/mainline/Image.gz').read_bytes()
    assert ramdisk == (ROOT/'dist/bringup/initramfs.cpio.gz').read_bytes()
    digest = hashlib.sha1()
    for payload in [kernel, ramdisk, b'', b'', dt]:
        digest.update(payload)
        digest.update(struct.pack('<I', len(payload)))
    assert blob[576:608] == digest.digest() + bytes(12)
    cmd = (blob[64:576].split(b'\0')[0]+blob[608:1632].split(b'\0')[0]).decode()
    assert cmd == manifest['profiles'][profile]['cmdline']
    nodes = validate.fdt_nodes(table(dt))
    assert nodes['/soc/mmc@11230000']['status'] == (b'okay\0' if profile == 'expdb' else b'disabled\0')
    assert nodes['/']['compatible'].startswith(b'rabbit,r1\0')
    entries = cpio(gzip.decompress(ramdisk))
    for module in ['pstore_zone', 'pstore_blk']:
        assert entries[f'lib/modules/{module}.ko'] == (ROOT/f'out/mainline/fs/pstore/{module}.ko').read_bytes()
        assert ('vermagic='+manifest['kernel_release']+' ').encode() in entries[f'lib/modules/{module}.ko']
    for source in ['init', 'r1-report', 'r1-log-start']:
        name = source if source == 'init' else 'bin/'+source
        assert entries[name] == (ROOT/'initramfs'/source).read_bytes()
        subprocess.run(['qemu-aarch64', str(ROOT/'out/busybox/busybox'), 'sh', '-n',
                        str(ROOT/'initramfs'/source)], check=True)
    assert entries['bin/expdb-map'] == (ROOT/'out/busybox/expdb-map').read_bytes()
    assert entries['bin/expdb-map'][:6] == b'\x7fELF\x02\x01'
    with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='unpack-test-') as tmp:
        result = subprocess.run(['python3', str(ROOT/'src/mkbootimg/unpack_bootimg.py'),
            '--boot_img', str(PACKAGE/f'boot-{profile}.img'), '--out', tmp],
            capture_output=True, text=True, check=True)
        assert (Path(tmp)/'kernel').read_bytes() == kernel
        assert (Path(tmp)/'ramdisk').read_bytes() == ramdisk
        assert (Path(tmp)/'dtb').read_bytes() == dt
    print('PASS:', profile, 'v2 header, SHA1 ID, load addresses, padding, DT table, initramfs, AOSP unpack')

assert len((PACKAGE/'dtbo.img').read_bytes()) == 8*1024*1024
stock_dtbo = (ROOT/'firmware/stock-v0.8.293/dtbo.img').read_bytes()
assert (PACKAGE/'dtbo.img').read_bytes() == stock_dtbo.ljust(8*1024*1024, b'\0')
assert (PACKAGE/'lk.bin').read_bytes() == (ROOT/'dist/lk/lk.bin').read_bytes()
assert (PACKAGE/'logo.bin').read_bytes() == (ROOT/'dist/lk/logo.bin').read_bytes()


def fixture_gpt():
    entries = bytearray(128*128)
    sector = 34
    for i, (name, size) in enumerate(flash.SIZES.items()):
        name = flash.partition_name(name, 'a')
        offset = i*128
        entries[offset:offset+16] = bytes([i+1])*16
        entries[offset+16:offset+32] = bytes([i+6])*16
        struct.pack_into('<QQ', entries, offset+32, sector, sector+size//512-1)
        encoded = name.encode('utf-16-le')
        entries[offset+56:offset+56+len(encoded)] = encoded
        sector += size//512
    header = bytearray(512)
    header[:8] = b'EFI PART'
    struct.pack_into('<4I4Q', header, 8, 0x10000, 92, 0, 0, 1, sector+100, 34, sector+50)
    header[56:72] = bytes(range(16))
    struct.pack_into('<QIII', header, 72, 2, 128, 128, zlib.crc32(entries))
    struct.pack_into('<I', header, 16, zlib.crc32(header[:92]))
    return bytes(512)+header+entries


def rejects(fn):
    try:
        fn()
    except (ValueError, SystemExit):
        return
    raise AssertionError('Invalid input accepted')


gpt = fixture_gpt()
assert flash.parse_gpt(gpt)['partitions']['expdb']['bytes'] == flash.SIZES['expdb']
rejects(lambda: flash.parse_gpt(gpt[:1024]))
for pos in [528, 540, 1100]:
    damaged = bytearray(gpt)
    damaged[pos] ^= 1
    rejects(lambda: flash.parse_gpt(damaged))

def repair_crcs(blob):
    struct.pack_into('<I', blob, 600, zlib.crc32(blob[1024:]))
    struct.pack_into('<I', blob, 528, 0)
    struct.pack_into('<I', blob, 528, zlib.crc32(blob[512:604]))

# Reject actual invalid layouts even when both CRCs are correct.
damaged = bytearray(gpt)
struct.pack_into('<Q', damaged, 1024+128+32, 34)
repair_crcs(damaged)
rejects(lambda: flash.parse_gpt(damaged))
damaged = bytearray(gpt)
damaged[1024+128+56:1024+128+128] = damaged[1024+56:1024+128]
repair_crcs(damaged)
rejects(lambda: flash.parse_gpt(damaged))
vbmeta = (ROOT/'firmware/stock-v0.8.293/vbmeta.img').read_bytes().ljust(flash.SIZES['vbmeta'], b'\0')
patched = flash.patch_vbmeta(vbmeta)
assert patched[:120] == vbmeta[:120] and patched[124:] == vbmeta[124:]
assert struct.unpack_from('>I', patched, 120)[0] == 3
rejects(lambda: flash.patch_vbmeta(vbmeta[:4096]))
rejects(lambda: flash.patch_vbmeta(bytes(flash.SIZES['vbmeta'])))
rejects(lambda: flash.local('/outside-rabbitr1'))

with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='flash-test-') as tmp:
    tmp = Path(tmp)
    backup = tmp/'backup'
    backup.mkdir()
    (backup/'gpt.bin').write_bytes(gpt)
    for name, size in flash.SIZES.items():
        filename = flash.partition_name(name, 'a')
        content = vbmeta if name == 'vbmeta' else b''
        if name == 'lk':
            content = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
        elif name == 'logo':
            content = (ROOT/'firmware/stock-v0.8.293/logo.bin').read_bytes()
        elif name in ['boot', 'dtbo']:
            content = (ROOT/'firmware/stock-v0.8.293'/(name+'.img')).read_bytes()
        (backup/(filename+'.img')).write_bytes(content.ljust(size, b'\0'))
    cmd = ['python3', str(PACKAGE/'prepare-flash.py'), 'prepare', '--package', str(PACKAGE),
           '--backup', str(backup), '--out', str(tmp/'prepared'), '--slot', 'a']
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode and 'already unlocked' in result.stderr
    assert not (tmp/'prepared').exists()
    subprocess.run(cmd+['--bootloader-unlocked'], check=True)
    prepared = tmp/'prepared'
    assert (prepared/'lk-new.img').read_bytes() == (PACKAGE/'lk.bin').read_bytes().ljust(flash.SIZES['lk'], b'\0')
    assert (prepared/'logo-new.img').read_bytes() == (PACKAGE/'logo.bin').read_bytes().ljust(flash.SIZES['logo'], b'\0')
    for part in flash.WRITE_PARTS:
        assert (prepared/(part+'-restore.img')).read_bytes() == (backup/(flash.partition_name(part,'a')+'.img')).read_bytes()
    for script in ['flash.sh', 'restore.sh']:
        subprocess.run(['bash', '-n', str(tmp/'prepared'/script)], check=True)
        preview = subprocess.check_output(['bash', str(tmp/'prepared'/script)], text=True)
        assert 'Preview only' in preview
        assert 'Do not relock until the complete stock firmware package' in preview
        text = (tmp/'prepared'/script).read_text()
        assert 'boot_a,dtbo_a,vbmeta_a,logo,lk_a' in text
        assert 'boot_b' not in text and 'seccfg' not in text
    subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=tmp/'prepared', check=True,
                   stdout=subprocess.DEVNULL)
    # Exercise generated --write scripts against files, never a USB backend.
    device = tmp/'device'
    shutil.copytree(backup, device)
    simulator = tmp/'mtk-simulator.py'
    simulator.write_text("""import json, shutil, sys
from pathlib import Path
root = Path(__file__).parent/'device'
args = sys.argv[1:]
if args[0] == 'gpt':
    shutil.copyfile(root/'gpt.bin', Path(args[1])/'gpt.bin')
    with (root/'runs.txt').open('a') as stream:
        stream.write(args[1]+'\\n')
elif args[0] in ['r','w']:
    parts, files = args[1].split(','), args[2].split(',')
    assert len(parts) == len(files) == 5
    assert parts == ['boot_a','dtbo_a','vbmeta_a','logo','lk_a']
    for part, filename in zip(parts,files):
        a,b = (root/(part+'.img')), Path(filename)
        shutil.copyfile(a,b) if args[0]=='r' else shutil.copyfile(b,a)
    if args[0] == 'w':
        with (root/'writes.txt').open('a') as stream:
            stream.write(json.dumps(parts)+'\\n')
else:
    raise RuntimeError('Unexpected command: '+repr(args))
""")
    def simulate(script, success):
        text = (prepared/script).read_text()
        old = 'mtk=(/rabbitr1/toolchains/mtkclient/bin/python /rabbitr1/src/mtkclient/mtk.py)'
        assert text.count(old) == 1
        text = text.replace(old, f'mtk=(python3 {simulator})')
        result = subprocess.run(['bash','-c',text,'test-flash','--write'], capture_output=True, text=True)
        assert (result.returncode == 0) == success, result.stdout+result.stderr
    try:
        simulate('flash.sh', True)
        for part in flash.WRITE_PARTS:
            assert (device/(flash.partition_name(part,'a')+'.img')).read_bytes() == (prepared/(part+'-new.img')).read_bytes()
        # A second write must stop at preflight: the device no longer matches its backup.
        simulate('flash.sh', False)
        assert len((device/'writes.txt').read_text().splitlines()) == 1
        # Restore must work even if an interrupted earlier write left a broken LK.
        (device/'lk_a.img').write_bytes(bytes(flash.SIZES['lk']))
        simulate('restore.sh', True)
        for part in flash.WRITE_PARTS:
            name = flash.partition_name(part,'a')+'.img'
            assert (device/name).read_bytes() == (backup/name).read_bytes()
        assert len((device/'writes.txt').read_text().splitlines()) == 2
        wrong_gpt = bytearray(gpt)
        wrong_gpt[568] ^= 1
        repair_crcs(wrong_gpt)
        (device/'gpt.bin').write_bytes(wrong_gpt)
        simulate('restore.sh', False)
        assert len((device/'writes.txt').read_text().splitlines()) == 2
    finally:
        for name in (device/'runs.txt').read_text().splitlines():
            run = Path(name).resolve()
            assert run.parent == ROOT/'.tmp' and run.name.startswith('r1-')
            shutil.rmtree(run)
    print('PASS: simulated writes/readbacks, changed-device rejection, partial LK restore and device identity checks')
print('PASS: GPT CRC/bounds rejection, AVB flags only, wrong-path rejection, backup preparation, dry run and restore scripts')
print('No hardware was accessed.')
