#!/usr/bin/env python3
"""Run LK's MMC fixup and its caller on real FDTs, including shipped libfdt."""
import hashlib
import importlib.util
import json
from pathlib import Path
import resource
import struct
import subprocess
import sys
import tempfile

from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE)
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)

ROOT = Path('/rabbitr1')
STOCK_SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
SAVED = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
         UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]
MMC = '/soc/mmc@11230000'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def emulate(image, dtb, mux, stale):
    bias, fdt, stack = 0x47fffe00, 0x50000000, 0x60008000
    start, stop = bias+0x1c28e, bias+0x1c294
    mmio = 0x100056f0
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    uc.mem_map(bias & ~4095, 0x200000)
    uc.mem_write(bias, image)
    bss_start, bss_end = struct.unpack_from('<II', image, 0x32c)
    assert (bss_start, bss_end) == (0x480b7874, 0x4816a594)
    uc.mem_write(bss_start, bytes(bss_end-bss_start))
    uc.mem_map(fdt, 0x80000)
    uc.mem_map(0x60000000, 0x10000)
    uc.mem_map(mmio & ~4095, 0x1000)
    uc.mem_write(mmio, struct.pack('<I', mux))
    # prepare_kernel_dtb() expands totalsize to 512 KiB before later fixups.
    before = bytearray(dtb.ljust(0x80000, b'\0'))
    assert len(before) == 0x80000
    struct.pack_into('>I', before, 4, len(before))
    uc.mem_write(fdt, bytes(before))
    # The function's six 14-byte stack slots start at entry SP - 0x7c.
    # Zero is one valid prior stack state. Named leftovers make an accidental
    # swap observable without relying on an unmapped-memory exception.
    if stale:
        for i in range(6):
            uc.mem_write(stack-0x7c+14*i, f'stale{i}'.encode().ljust(14, b'\0'))
    for i, reg in enumerate(SAVED):
        uc.reg_write(reg, fdt if reg == UC_ARM_REG_R8 else 0xa000+i)
    uc.reg_write(UC_ARM_REG_SP, stack)
    uc.reg_write(UC_ARM_REG_LR, 0x70000001)
    calls, register_reads, dt_writes, visited_nodes = [], [], [], []
    def code(uc, address, size, _):
        at = address-bias
        if at == 0x2a3bc:
            # Only logging is stubbed. FDT traversal/mutation, string copies,
            # swaps, memmove and all other called instructions execute.
            uc.reg_write(UC_ARM_REG_R0, 0)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        assert (0x1c28e <= at < 0x1c294 or 0x6bd4 <= at < 0x6d80 or
                0x2b000 <= at < 0x2d000 or 0x60800 <= at < 0x60900), hex(at)
        if at in (0x6bfc, 0x6bd4, 0x2ca4c, 0x2cafc):
            calls.append(at)
        if at == 0x2c4b4:  # fdt_node_offset_by_compatible() visits a node
            visited_nodes.append(uc.reg_read(UC_ARM_REG_R4))
    def write(uc, access, address, size, value, _):
        if fdt <= address and address+size <= fdt+0x80000:
            dt_writes.append((address-fdt, size))
        else:
            assert stack-0x1000 <= address and address+size <= stack, hex(address)
    def read(uc, access, address, size, value, _):
        assert address == mmio and size == 4
        register_reads.append(mux)
    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.hook_add(UC_HOOK_MEM_READ, read, begin=mmio & ~4095, end=(mmio & ~4095)+4095)
    uc.emu_start(start | 1, stop, count=30000000)
    assert uc.reg_read(UC_ARM_REG_PC) == stop, (
        f'LK fixup did not return at {uc.reg_read(UC_ARM_REG_PC)-bias:#x}; '
        f'calls={calls}; nodes={len(visited_nodes)}; '
        f'result={uc.reg_read(UC_ARM_REG_R0):#x}')
    assert uc.reg_read(UC_ARM_REG_SP) == stack
    for i, reg in enumerate(SAVED):
        assert uc.reg_read(reg) == (fdt if reg == UC_ARM_REG_R8 else 0xa000+i)
    return bytes(before), bytes(uc.mem_read(fdt, 0x80000)), {
        'calls': calls, 'register_reads': register_reads, 'fdt_writes': len(dt_writes),
        'result': uc.reg_read(UC_ARM_REG_R0), 'visited_nodes': len(visited_nodes),
    }


def main():
    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    stock = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    assert hashlib.sha256(stock).hexdigest() == STOCK_SHA, 'Unknown stock LK'
    candidate = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else ROOT/'dist/lk/lk.bin'
    assert candidate.is_relative_to(ROOT)
    patched = candidate.read_bytes()
    assert len(patched) == len(stock)
    base = (ROOT/'dist/mainline/mt6765-rabbit-r1.dtb').read_bytes()
    cases = []
    with tempfile.TemporaryDirectory(dir=ROOT/'.tmp', prefix='lk-mmc-fixup-') as tmp:
        dtb = Path(tmp)/'board.dtb'
        for profile, status in (('ram', 'disabled'), ('expdb', 'okay')):
            dtb.write_bytes(base)
            subprocess.run(['fdtput', '-t', 's', str(dtb), MMC, 'status', status], check=True)
            data = dtb.read_bytes()
            expected = validate.fdt_nodes(data)
            assert expected[MMC]['pinctrl-names'] == b'default\0'
            for mux in (0, 0x2000, 0x4000, 0xffffe000):
                for stale in (False, True):
                    before, after, trace = emulate(stock, data, mux, stale)
                    nodes = validate.fdt_nodes(after)
                    assert trace['calls'].count(0x6bfc) == 1
                    assert trace['calls'].count(0x6bd4) == 3
                    assert trace['register_reads'] == [mux] and trace['result'] == 0
                    if mux & 0x6000:
                        assert nodes == expected and before == after
                        assert not trace['fdt_writes']
                    else:
                        broken = {name: dict(props) for name, props in expected.items()}
                        broken[MMC]['pinctrl-names'] = b'stale3\0' if stale else b'\0'
                        assert nodes == broken, 'Unexpected stock fixup result'
                        assert trace['fdt_writes'] and before != after
                    cases.append({'image': 'stock', 'profile': profile, 'mux': mux,
                                  'stale_stack': stale, 'pinctrl_names': nodes[MMC]['pinctrl-names'].hex(),
                                  **trace})
                    before, after, trace = emulate(patched, data, mux, stale)
                    assert before == after, 'Patched LK changed the mainline MMC pin states'
                    assert not trace['calls'] and not trace['register_reads'] and not trace['fdt_writes']
                    cases.append({'image': 'patched', 'profile': profile, 'mux': mux,
                                  'stale_stack': stale, **trace})
        # Stock Rabbit DT uses mediatek,msdc, so this lookup fails there.
        original_dt = (ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes()
        before, after, trace = emulate(stock, original_dt, 0, True)
        assert before == after and trace['result'] == 0xffffffff
        assert trace['calls'] == [0x6bfc] and not trace['register_reads']
        cases.append({'image': 'stock', 'profile': 'vendor', **trace})
        # With all six assumed states supplied, the original routine rotates
        # the names by three. This is an ABI assumption, not a useful fixup for
        # the mainline node or a reason to add dummy pin states.
        names = ['default', 'state_uhs', 'sleep', 'emmc_default', 'emmc_uhs', 'emmc_sleep']
        subprocess.run(['fdtput', '-t', 's', str(dtb), MMC, 'pinctrl-names', *names], check=True)
        data = dtb.read_bytes()
        before, after, trace = emulate(stock, data, 0, False)
        expected = validate.fdt_nodes(data)
        expected[MMC]['pinctrl-names'] = b'\0'.join(n.encode() for n in names[3:]+names[:3])+b'\0'
        assert validate.fdt_nodes(after) == expected and trace['calls'].count(0x6bd4) == 3
        cases.append({'image': 'stock', 'profile': 'six-state-fixture', **trace})
    report = {'stock_lk_sha256': STOCK_SHA, 'patched_lk_sha256': hashlib.sha256(patched).hexdigest(),
              'caller_offset': 0x1c290, 'fixup_offset': 0x6bfc, 'cases': cases,
              'scope': 'caller and complete fixup with shipped libfdt/libc; logging and MMIO read modeled',
              'limits': 'no whole-LK boot, physical mux state or eMMC validation'}
    (ROOT/'out/lk-mmc-fixup-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS: {len(cases)} LK/FDT fixtures; stock pin-name corruption reproduced')
    print('PASS: patched Linux caller preserves RAM/expdb DTBs byte-for-byte')
    print('Full boot and physical pin/clock/storage behavior remain untested.')


if __name__ == '__main__':
    main()
