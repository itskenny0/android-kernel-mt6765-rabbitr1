#!/usr/bin/env python3
"""Execute the patched Thumb handoff with Unicorn; no hardware or USB access."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import *
from PIL import Image

ROOT = Path('/rabbitr1')

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/filename)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

build = load('build', 'build-lk.py')
validate = load('validate', 'validate-kernel.py')
stock = (build.STOCK/'lk.img').read_bytes()
patched = (build.DIST/'lk.bin').read_bytes()
warnings = (build.OUT/'warnings.bin').read_bytes()
assert patched == build.patch_handoff(stock, warnings)
for bad in [stock[:-1], bytes(len(stock)), stock[:100]+bytes([stock[100]^1])+stock[101:]]:
    try:
        build.patch_handoff(bad, warnings)
    except ValueError:
        pass
    else:
        raise AssertionError('Unknown LK accepted')
assert patched[:512] == stock[:512] and len(patched) == len(stock)
# Upstream tests trace the stock Fastboot registration and execute the refusal.
subprocess.run([sys.executable, str(build.ZAP/'tests/test_relock.py'), '-v'], check=True,
               env=dict(os.environ, MTKLKZAP_TEST_LK=str(build.STOCK/'lk.img')))
relock = (build.OUT/'relock.bin').read_bytes()
profile = json.loads((build.ZAP/'profiles/lk/rabbit-r1-v0.8.293.json').read_text())
assert patched[profile['handler']:profile['handler_end']] == relock[profile['handler']:profile['handler_end']]
assert patched[profile['fastboot_fail']:profile['fastboot_fail']+16] == stock[profile['fastboot_fail']:profile['fastboot_fail']+16]
print('PASS: relock guard survives warning and mainline handoff patches')
assert patched[0x211bc:0x212e8] == stock[0x211bc:0x212e8]  # shared overlay function
assert patched[0x3e064:0x3e3d0] == stock[0x3e064:0x3e3d0]  # early LK caller
for offset, before, after in build.HANDOFF_PATCHES:
    code = list(Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(after, offset))
    print('PATCH:', '; '.join(f'{i.address:#x} {i.mnemonic} {i.op_str}' for i in code))
assert [(i.mnemonic, i.op_str) for i in Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(patched[0x2138e:0x21396],0x2138e)] == [
    ('str.w', 'r8, [sp, #8]'), ('movs', 'r4, #0'), ('nop', '')]

# Real stock DT merge preserves all board data, including GPIO/charger settings.
base = (build.STOCK/'boot-unpacked/base.dtb').read_bytes()
assert stock.count(base) == 1 and patched.count(base) == 1
with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='lk-test-') as tmp:
    output = Path(tmp)/'merged.dtb'
    subprocess.run([str(ROOT/'out/mainline/scripts/dtc/fdtoverlay'), '-i',
        str(build.STOCK/'boot-unpacked/base.dtb'), '-o', str(output),
        str(build.STOCK/'overlay-0.dtbo')], check=True)
    merged = output.read_bytes()
    nodes = validate.fdt_nodes(merged)
    assert nodes == validate.fdt_nodes((build.STOCK/'merged.dtb').read_bytes())
    assert len(nodes['/gpio@10005000']['gpio_init_default']) == 5040
print('PASS: LK private DT, GPIO table and charger/PMIC overlay unchanged')


def emulate(image, dtb, shift=0, overlay_shift=0, original=False, fail_alloc=0):
    # Only external allocation/accessor/memcpy/logging calls are modeled. Every
    # instruction of prepare_kernel_dtb(), including cleanup and bounds, runs.
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    codebase = 0x100000
    uc.mem_map(codebase, 0x100000)
    uc.mem_write(codebase, image)
    for address, size in [(0x400000,0x100000), (0x500000,0x100000),
                          (0x600000,0x100000), (0x800000,0x200000), (0xb00000,0x10000), (0xc00000,0x1000)]:
        uc.mem_map(address,size)
    source, ov, dest, stack, stop = 0x400000+shift,0x500000+overlay_shift,0x600000,0xb08000,0xc00000
    overlay = (build.STOCK/'overlay-0.dtbo').read_bytes()
    uc.mem_write(source,dtb)
    uc.mem_write(ov,overlay)
    uc.mem_write(dest, b'\xa5'*0x80000)
    uc.reg_write(UC_ARM_REG_SP,stack)
    uc.reg_write(UC_ARM_REG_LR,stop|1)
    regs = [UC_ARM_REG_R4,UC_ARM_REG_R5,UC_ARM_REG_R6,UC_ARM_REG_R7,UC_ARM_REG_R8,
            UC_ARM_REG_R9,UC_ARM_REG_R10,UC_ARM_REG_R11]
    for i,reg in enumerate(regs):
        uc.reg_write(reg,0xa000+i)
    live = set()
    allocations, next_alloc, overlays = 0,0x800000,0
    def malloc(size):
        nonlocal allocations,next_alloc
        allocations += 1
        if fail_alloc == allocations:
            return 0
        ptr = next_alloc
        next_alloc += (size+15)&~15
        assert next_alloc <= 0xa00000
        live.add(ptr)
        return ptr
    def hook(uc, address, size, _):
        nonlocal overlays
        offset = address-codebase
        if offset not in [0x3d360,0x3d2e4,0x3d354,0x3d31c,0x21110,0x211b8,
                          0x2b034,0x2b068,0x2b1e8,0x18aa4,0x2a3bc,0x211bc]:
            return
        r0,r1,r2 = [uc.reg_read(reg) for reg in [UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2]]
        if offset == 0x3d360: result = len(overlay)
        elif offset == 0x3d2e4: result = len(dtb)
        elif offset == 0x3d354: result = ov
        elif offset == 0x3d31c: result = source
        elif offset == 0x21110: result = dest
        elif offset == 0x211b8: result = 1
        elif offset == 0x2b034: result = malloc(r0)
        elif offset == 0x2b068:
            assert r0 in live, f'Invalid/double free {r0:#x}'
            live.remove(r0)
            result = 0
        elif offset == 0x2b1e8:
            uc.mem_write(r0,bytes(uc.mem_read(r1,r2)))
            result = r0
        elif offset == 0x211bc:
            assert original, 'Patched Linux handoff still applies vendor overlay'
            overlays += 1
            result_ptr = malloc(len(merged))
            uc.mem_write(result_ptr, merged)
            output_ptr = struct.unpack('<I', uc.mem_read(uc.reg_read(UC_ARM_REG_SP),4))[0]
            uc.mem_write(output_ptr, struct.pack('<I',result_ptr))
            result = 0
        else: result = 0
        uc.reg_write(UC_ARM_REG_R0,result)
        uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
    uc.hook_add(UC_HOOK_CODE,hook)
    uc.emu_start((codebase+0x212e8)|1,stop,count=5000)
    assert uc.reg_read(UC_ARM_REG_PC) == stop, 'Handoff did not return'
    assert uc.reg_read(UC_ARM_REG_SP) == stack
    assert all(uc.reg_read(reg)==0xa000+i for i,reg in enumerate(regs))
    assert not live, f'Leaked allocations {live}'
    result = uc.reg_read(UC_ARM_REG_R0)
    if fail_alloc or struct.unpack_from('>I',dtb,4)[0] > 0x80000:
        assert result != 0 and bytes(uc.mem_read(dest,0x80000)) == b'\xa5'*0x80000
    else:
        assert result == 0
        expected = bytearray(merged if original else dtb)
        struct.pack_into('>I',expected,4,0x80000)
        assert bytes(uc.mem_read(dest,len(expected))) == expected
        assert bytes(uc.mem_read(source,len(dtb))) == dtb
    assert overlays == int(original)

# Establish that the unmodified caller actually invokes the shared overlay.
emulate(stock,base,original=True)
dtb = (ROOT/'dist/mainline/mt6765-rabbit-r1.dtb').read_bytes()
for shift in range(4):
    for overlay_shift in range(4):
        emulate(patched,dtb,shift,overlay_shift)
for allocation in [1,2]:
    emulate(patched,dtb,1,1,fail_alloc=allocation)
oversize = bytearray(dtb)
struct.pack_into('>I',oversize,4,0x80001)
emulate(patched,bytes(oversize),1,1)
print('PASS: Thumb handoff, 16 alignment cases, allocation failures, 512 KiB bound, registers and ownership')

# Independent logo decoding proves exact pixel order and preservation of other slots.
a = (build.STOCK/'logo.bin').read_bytes()
b = (build.DIST/'logo.bin').read_bytes()
assert a[:4] == b[:4] and a[8:512] == b[8:512]
before, after = build.logo_slots(a), build.logo_slots(b)
assert [i for i in range(60) if before[i] != after[i]] == [0,38]
rgba = Image.open(build.DIST/'splash.png').convert('RGBA')
for slot in [0,38]:
    assert zlib.decompress(after[slot]) == rgba.tobytes('raw','BGRA')
print('PASS: LineageOS splash pixels, MTK header, and 58 unchanged logo slots')
print('No hardware was accessed; this does not emulate the complete bootloader or board.')
