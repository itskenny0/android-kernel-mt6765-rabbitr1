#!/usr/bin/env python3
"""Validate packaged images and the flash preparer without accessing a device."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import shlex
import shutil
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import zlib

ROOT = Path('/rabbitr1')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, default=ROOT/'dist/mtkclient')
parser.add_argument('--scripts', type=Path, default=Path(__file__).resolve().parent)
args = parser.parse_args()
PACKAGE, SCRIPTS = args.package.resolve(), args.scripts.resolve()
assert PACKAGE.is_relative_to(ROOT) and SCRIPTS.is_relative_to(ROOT)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS/filename)
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
assert manifest['lk_build']['kernel_mmc_pinctrl_preserved'] is True
assert manifest['lk_build']['kernel_scp_fixup_bypassed'] is True
assert manifest['lk_build']['kernel_console_preserved'] is True
assert manifest['lk_build']['kernel_display_guard'] is True
assert flash.has_display_guard((PACKAGE/'lk.bin').read_bytes())
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


# Reject stale LK even if its build record claims the new fix. These failures
# must occur before requesting any device backup or creating a flash script.
with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='lk-fixup-gate-') as tmp:
    directory = Path(tmp)
    packager = load('packager', 'package-mtkclient.py')
    packager.ROOT = directory
    packager.OUT = directory/'output'
    packager.DIST = directory/'package'
    (directory/'out').mkdir()
    (directory/'out/mainline').symlink_to(ROOT/'out/mainline', target_is_directory=True)
    (directory/'scripts').symlink_to(ROOT/'scripts', target_is_directory=True)
    (directory/'dist/lk').mkdir(parents=True)
    (directory/'dist/bringup').symlink_to(ROOT/'dist/bringup', target_is_directory=True)
    features = [
        ('kernel_mmc_pinctrl_preserved', 0x1c290, 'eaf7b4fc',
         'MMC pin-state fix', 'preserve the mainline MMC pin states'),
        ('kernel_scp_fixup_bypassed', 0x4a44, '10f0d0f8',
         'SCP node fix', 'skip the vendor SCP node requirement'),
        ('kernel_console_preserved', 0x1cb70, '2df0defd',
         'console fix', 'preserve the mainline console'),
        ('kernel_display_guard', 0x1d4d0, 'e5f758fc',
         'checked LK display handoff', 'checked display handoff'),
        ('kernel_display_guard', 0x287c8, '00000000',
         'checked LK display handoff', 'checked display handoff'),
        ('kernel_display_guard', 0x28788, '00000000',
         'checked LK display handoff', 'checked display handoff'),
    ]
    for feature, offset, old, prepare_error, package_error in features:
        stale = bytearray((PACKAGE/'lk.bin').read_bytes())
        stale[offset:offset+4] = bytes.fromhex(old)
        (directory/'lk.bin').write_bytes(stale)
        (directory/'dist/lk/lk.bin').write_bytes(stale)
        for value in (None, False, True):
            fixture = json.loads(json.dumps(manifest))
            if value is None:
                del fixture['lk_build'][feature]
            else:
                fixture['lk_build'][feature] = value
            (directory/'manifest.json').write_text(json.dumps(fixture))
            try:
                flash.prepare(SimpleNamespace(package=directory))
            except ValueError as error:
                assert prepare_error in str(error), str(error)
            else:
                raise AssertionError('Stale LK accepted')
            (directory/'dist/lk/build.json').write_text(json.dumps(fixture['lk_build']))
            try:
                packager.main()
            except ValueError as error:
                assert package_error in str(error), str(error)
            else:
                raise AssertionError('Stale LK packaged')
            assert not list(packager.DIST.iterdir())
print('PASS: thirty-six stale-LK packaging/preparation cases rejected before producing flash files')

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
    cmd = ['python3', str(SCRIPTS/'prepare-flash.py'), 'prepare', '--package', str(PACKAGE),
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
    # Exercise real generated scripts with a file-backed, recording-only DA.
    # No USB backend is imported. Every case removes its private readback tree.
    device = tmp/'device'
    shutil.copytree(backup, device)
    simulator = tmp/'mtk-simulator.py'
    simulator.write_text("""import json, shutil, sys
from pathlib import Path
root = Path(__file__).parent/'device'
args = sys.argv[1:]
control = json.loads((root/'control.json').read_text())
with (root/'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args)+'\\n')
if args[0] == 'gpt':
    output = Path(args[1])
    with (root/'runs.txt').open('a') as stream:
        stream.write(str(output)+'\\n')
    if control.get('kind') != 'gpt-no-output':
        shutil.copyfile(root/'gpt.bin', output/'gpt.bin')
    if control.get('kind') in ['existing-output','symlink-output']:
        part = control['part']
        path = output/(part+'-'+control['phase']+'.img')
        source = Path.cwd()/(part+'-'+control.get('suffix','new')+'.img')
        path.symlink_to(source) if control['kind']=='symlink-output' else shutil.copyfile(source,path)
elif args[0] in ['r','w']:
    part, filename = args[1:3]
    assert ',' not in part and ',' not in filename
    base = part if part=='logo' else part[:-2]
    sizes = {'boot':33554432,'dtbo':8388608,'vbmeta':8388608,'logo':11534336,'lk':1048576}
    assert part == (base if base=='logo' else base+'_a')
    assert args[3:] == (['--parttype','user','--offset','0x0','--length',hex(sizes[base])]
                       if args[0]=='r' else ['--parttype','user'])
    a,b = root/(part+'.img'), Path(filename)
    phase = 'write' if args[0]=='w' else 'before' if b.name.endswith('-before.img') else 'readback'
    kind = control.get('kind') if control.get('part')==base and control.get('phase')==phase else None
    if kind == 'exit-failure': sys.exit(7)
    if kind in ['no-output','skip-write']: sys.exit(0) # Real CLI can fail and return0.
    source,target = (a,b) if args[0]=='r' else (b,a)
    shutil.copyfile(source,target)
    if kind == 'truncate':
        with target.open('r+b') as stream: stream.truncate(sizes[base]-1)
    elif kind == 'corrupt':
        with target.open('r+b') as stream:
            first=stream.read(1); stream.seek(0); stream.write(bytes([first[0]^1]))
else:
    raise RuntimeError('Unexpected command: '+repr(args))
""")
    transfer_cases = 0

    def reset_device(restoring=False):
        (device/'gpt.bin').write_bytes(gpt)
        for part in flash.WRITE_PARTS:
            source = prepared/(part+'-new.img') if restoring else backup/(flash.partition_name(part,'a')+'.img')
            shutil.copyfile(source, device/(flash.partition_name(part,'a')+'.img'))
        (device/'calls.jsonl').write_text('')
        (device/'runs.txt').write_text('')

    def simulate(script, success, control=None):
        (device/'control.json').write_text(json.dumps(control or {}))
        (device/'calls.jsonl').write_text('')
        (device/'runs.txt').write_text('')
        text = (prepared/script).read_text()
        old = 'mtk=(/rabbitr1/toolchains/mtkclient/bin/python /rabbitr1/src/mtkclient/mtk.py)'
        assert text.count(old) == 1
        text = text.replace(old, f'mtk=(python3 {shlex.quote(str(simulator))})')
        try:
            result = subprocess.run(['bash','-c',text,'test-flash','--write'], capture_output=True, text=True)
            assert (result.returncode == 0) == success, result.stdout+result.stderr
            return [json.loads(line) for line in (device/'calls.jsonl').read_text().splitlines()]
        finally:
            for name in (device/'runs.txt').read_text().splitlines():
                run = Path(name).resolve()
                assert run.parent == ROOT/'.tmp' and run.name.startswith('r1-')
                shutil.rmtree(run)

    def written(calls):
        return [call[1] for call in calls if call[0]=='w']

    expected = [flash.partition_name(part,'a') for part in flash.WRITE_PARTS]
    reset_device()
    calls = simulate('flash.sh', True)
    assert [call[0] for call in calls] == ['gpt']+['r']*5+['w','r']*5
    assert [call[1] for call in calls[1:6]] == expected
    assert written(calls) == expected
    for part in flash.WRITE_PARTS:
        assert (device/(flash.partition_name(part,'a')+'.img')).read_bytes() == (prepared/(part+'-new.img')).read_bytes()
    transfer_cases += 1
    # A second flash must stop before the first write: source no longer matches.
    assert not written(simulate('flash.sh', False)); transfer_cases += 1
    # Restore accepts broken LK contents while still checking full dump size.
    (device/'lk_a.img').write_bytes(bytes(flash.SIZES['lk']))
    calls = simulate('restore.sh', True)
    assert [call[0] for call in calls] == ['gpt']+['r']*5+['w','r']*5
    assert written(calls) == expected
    for part in flash.WRITE_PARTS:
        name = flash.partition_name(part,'a')+'.img'
        assert (device/name).read_bytes() == (backup/name).read_bytes()
    transfer_cases += 1
    for script in ['flash.sh','restore.sh']:
        reset_device(script=='restore.sh')
        wrong_gpt = bytearray(gpt); wrong_gpt[568] ^= 1; repair_crcs(wrong_gpt)
        (device/'gpt.bin').write_bytes(wrong_gpt)
        assert not written(simulate(script, False)); transfer_cases += 1
        reset_device(script=='restore.sh')
        assert not written(simulate(script, False, {'kind':'gpt-no-output'})); transfer_cases += 1

    # All source partitions, particularly the last LK, are checked before flash.
    for part in flash.WRITE_PARTS:
        reset_device()
        with (device/(flash.partition_name(part,'a')+'.img')).open('r+b') as stream:
            first=stream.read(1); stream.seek(0); stream.write(bytes([first[0]^1]))
        assert not written(simulate('flash.sh', False)); transfer_cases += 1
    # Silent failures/partial/stale reads cannot become successful preflight.
    for script in ['flash.sh','restore.sh']:
        for part in flash.WRITE_PARTS:
            for kind in ['no-output','truncate','existing-output','symlink-output']:
                reset_device(script=='restore.sh')
                calls = simulate(script, False, {'phase':'before','part':part,'kind':kind})
                assert not written(calls); transfer_cases += 1
        # Every write must be verified before the next partition is attempted.
        for index,part in enumerate(flash.WRITE_PARTS):
            for phase,kinds in [('write',['corrupt','truncate','exit-failure']),
                                ('readback',['no-output','truncate','corrupt','exit-failure',
                                             'existing-output','symlink-output'])]:
                for kind in kinds:
                    reset_device(script=='restore.sh')
                    calls = simulate(script, False, {'phase':phase,'part':part,'kind':kind,
                                                     'suffix':'restore' if script=='restore.sh' else 'new'})
                    assert written(calls) == expected[:index+1], (script,part,phase,kind,calls)
                    transfer_cases += 1
        # A dropped write to differing boot content returns0 but must halt.
        reset_device(script=='restore.sh')
        calls = simulate(script, False, {'phase':'write','part':'boot','kind':'skip-write'})
        assert written(calls) == ['boot_a']; transfer_cases += 1
    # Even an internally checksummed prepared image must have full GPT size.
    original = (prepared/'lk-new.img').read_bytes()
    original_sums = (prepared/'SHA256SUMS').read_text()
    try:
        (prepared/'lk-new.img').write_bytes(original[:-1])
        lines = original_sums.splitlines()
        lines = [flash.sha(prepared/'lk-new.img')+'  lk-new.img' if line.endswith('  lk-new.img') else line for line in lines]
        (prepared/'SHA256SUMS').write_text('\n'.join(lines)+'\n')
        reset_device()
        assert not simulate('flash.sh', False); transfer_cases += 1
    finally:
        (prepared/'lk-new.img').write_bytes(original)
        (prepared/'SHA256SUMS').write_text(original_sums)
    print(f'PASS: {transfer_cases} ordered transfer/preflight/readback failure cases, including silent CLI errors and stale outputs')
print('PASS: GPT CRC/bounds rejection, AVB flags only, wrong-path rejection, backup preparation, dry run and restore scripts')
print('No hardware was accessed.')
