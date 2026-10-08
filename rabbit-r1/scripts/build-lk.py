#!/usr/bin/env python3
"""Build the r1 LK and LineageOS splash from pinned stock firmware and mtklkzap."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

ROOT = Path('/rabbitr1')
STOCK = ROOT/'firmware/stock-v0.8.293'
ZAP = ROOT/'src/mtklkzap'
OUT = ROOT/'out/lk'
DIST = ROOT/'dist/lk'
STOCK_SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
# File offsets, valid ONLY for STOCK_SHA. The shared dtb_overlay() and its
# early LK caller remain untouched. Skip the later vendor MMC pin-name swap
# too: it assumes six states and corrupts the mainline node's single state.
# See docs/LK.md for both calling conventions.
HANDOFF_PATCHES = [
    (0x1c290, bytes.fromhex('eaf7b4fc'), bytes.fromhex('002000bf')),
    (0x2138e, bytes.fromhex('fff715ff044650bb'), bytes.fromhex('cdf80880002400bf')),
    (0x213da, bytes.fromhex('0298'), bytes.fromhex('0020')),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run(*args):
    subprocess.run([str(x) for x in args], check=True)


def check_stock(stock):
    if sha(stock) != STOCK_SHA:
        raise ValueError('Unsupported LK; expected the exact RabbitOS v0.8.293 image')


def patch_handoff(stock, warnings):
    check_stock(stock)
    if len(stock) != len(warnings):
        raise ValueError('LK length changed')
    output = bytearray(warnings)
    for offset, before, after in HANDOFF_PATCHES:
        if output[offset:offset+len(before)] != before:
            raise ValueError(f'Unexpected handoff instructions at {offset:#x}')
        output[offset:offset+len(after)] = after
    return bytes(output)


def logo_slots(blob):
    if blob[:4] != bytes.fromhex('88168858') or len(blob) < 528:
        raise ValueError('Invalid MTK logo header')
    payload = blob[512:]
    count, size = struct.unpack_from('<II', payload)
    if not 1 <= count <= 256 or not 8+4*count <= size <= len(payload):
        raise ValueError('Invalid logo table')
    offsets = struct.unpack_from('<'+str(count)+'I', payload, 8)+(size,)
    if offsets[0] != 8+4*count or any(a >= b for a,b in zip(offsets, offsets[1:])):
        raise ValueError('Invalid logo offsets')
    return [payload[a:b] for a,b in zip(offsets, offsets[1:])]


def main():
    for path in [OUT, DIST]:
        path.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT/'sources.lock.json').read_text())
    revision = subprocess.check_output(['git','-C',str(ZAP),'rev-parse','HEAD'], text=True).strip()
    if revision != lock['repositories']['mtklkzap']['commit'] or subprocess.check_output(
            ['git','-C',str(ZAP),'status','--porcelain'], text=True).strip():
        raise ValueError('mtklkzap must match its clean pinned revision')
    stock = (STOCK/'lk.img').read_bytes()
    check_stock(stock)
    # Relock protection identifies pristine firmware; it must precede other patches.
    run(sys.executable, ZAP/'patch_lk_relock.py', STOCK/'lk.img', '--force', '-o', OUT/'relock.bin')
    run(sys.executable, ZAP/'verify_lk_relock.py', STOCK/'lk.img', OUT/'relock.bin')
    relock = (OUT/'relock.bin').read_bytes()
    run(sys.executable, ZAP/'patch_lk_orangestate.py', OUT/'relock.bin', '--mode', 'both',
        '--force', '-o', OUT/'orange.bin')
    run(sys.executable, ZAP/'patch_lk_dmverity.py', OUT/'orange.bin', '--force', '-o', OUT/'warnings.bin')
    # Upstream checks the exact warning diff, before our separate DT handoff edit.
    run(sys.executable, ZAP/'verify-lk.py', OUT/'relock.bin', OUT/'warnings.bin')
    warnings = (OUT/'warnings.bin').read_bytes()
    patched = patch_handoff(stock, warnings)
    (DIST/'lk.bin').write_bytes(patched)
    run(sys.executable, ZAP/'make-splash.py', '--width', '480', '--height', '640',
        '--fraction', '0.45', '--src', ZAP/'examples/lineage-logo.png', '-o', DIST/'splash.png')
    run(sys.executable, ZAP/'build-logo.py', '--logo', STOCK/'logo.bin',
        '--profile', ZAP/'profiles/mt6765-480x640.yaml', '--name', 'mt6765-480x640',
        '--image', DIST/'splash.png', '--slots', '0,38', '--partition-size', str(11*1024*1024),
        '--mtklogo', ROOT/'toolchains/mtklogo/linux-amd64/mtklogo', '-o', DIST/'logo.bin')
    before, after = logo_slots((STOCK/'logo.bin').read_bytes()), logo_slots((DIST/'logo.bin').read_bytes())
    if len(before) != 60 or len(after) != 60 or [i for i in range(60) if before[i] != after[i]] != [0,38]:
        raise ValueError('Unexpected logo slot changes')
    report = {
        'format': 3, 'stock_lk_sha256': STOCK_SHA, 'stock_lk_bytes': len(stock),
        'mtklkzap_commit': revision, 'warning_bytes_changed': sum(a != b for a,b in zip(relock,warnings)),
        'handoff_patches': [{'file_offset': offset, 'before': before.hex(), 'after': after.hex()}
                            for offset,before,after in HANDOFF_PATCHES],
        'relock_protection': {
            'enabled': True, 'profile': 'rabbit-r1-v0.8.293',
            'bytes_changed': sum(a != b for a,b in zip(stock,relock)),
            'command': 'flashing lock',
            'response': 'FAILRelock blocked: restore complete stock firmware first',
            'scope': 'patched LK handler; other loaders and direct seccfg writes unaffected',
        },
        'kernel_overlay': 'bypassed; mainline base copied with original bounds and later fixups',
        'kernel_mmc_pinctrl_preserved': True,
        'lk_overlay': 'stock DTBO; shared overlay function and early caller unchanged',
        'logo': {'width': 480, 'height': 640, 'color_model': 'bgrabe', 'changed_slots': [0,38]},
        'verified_boot': 'warnings suppressed; unlocking and vbmeta policy remain separate',
        'hardware_tested': False,
        'files': {name: {'bytes': (DIST/name).stat().st_size, 'sha256': sha((DIST/name).read_bytes())}
                  for name in ['lk.bin','logo.bin','splash.png']},
    }
    (DIST/'build.json').write_text(json.dumps(report, indent=2)+'\n')
    (DIST/'SHA256SUMS').write_text(''.join(f'{sha(p.read_bytes())}  {p.name}\n'
        for p in sorted(DIST.iterdir()) if p.name != 'SHA256SUMS'))
    print('Built LK and logo; hardware boot remains untested.')


if __name__ == '__main__':
    main()
