#!/usr/bin/env python3
"""Trace selected stock LK/ATF handoff routines without executing a host SMC."""
import hashlib
import json
from pathlib import Path
import struct

from unicorn import (Uc, UC_ARCH_ARM, UC_ARCH_ARM64, UC_MODE_ARM,
                     UC_MODE_THUMB, UC_HOOK_CODE, UC_HOOK_MEM_WRITE)
from unicorn import arm_const as arm, arm64_const as arm64

ROOT = Path('/rabbitr1')
STOCK = ROOT/'firmware/stock-v0.8.293'
TEE_SHA = 'e6de1331346ea1df4a0b78106de5ec5886f6eda99270de5e23ac9e7ba7101b62'
LK_SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
LK_BIAS = 0x47fffe00
# This fixture mapping resolves both ADRP-relative data and the image's
# absolute GOT pointers. It is not a measurement of a running device.
ATF_BIAS = 0x47d803c0
STACK, STOP, CONTEXT, CPU = 0x60008000, 0x6000f000, 0x60010000, 0x60011000
KERNEL, FDT = 0x40080000, 0x47880000


def contains(ranges, address, size):
    return any(first <= address and address+size <= end for first, end in ranges)


def trace_lk(image, disabled, crypto_result):
    uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    uc.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_A15)
    uc.mem_map(LK_BIAS & ~4095, 0x200000)
    uc.mem_map(0x60000000, 0x10000)
    uc.mem_write(LK_BIAS, image)
    first, end = struct.unpack_from('<II', image, 0x32c)
    assert (first, end) == (0x480b7874, 0x4816a594)
    uc.mem_write(first, bytes(end-first))
    flag = struct.unpack_from('<I', image, 0xb76b8)[0]
    assert flag == LK_BIAS+0xbb718
    uc.mem_write(flag, struct.pack('<I', disabled))
    calls = []
    ranges = ((0x3760, 0x37a2), (0x14344, 0x14362),
              (0x1ba20, 0x1ba48), (0x2889c, 0x288e6), (0x28960, 0x28984))

    def code(machine, address, size, _):
        at = address-LK_BIAS
        if at in (0x2a3bc, 0x18aa4):  # logging and verbosity only
            machine.reg_write(arm.UC_ARM_REG_R0, 0)
            machine.reg_write(arm.UC_ARM_REG_PC, machine.reg_read(arm.UC_ARM_REG_LR))
            return
        assert contains(ranges, at, size), f'Unexpected LK instruction {at:#x}'
        if at == 0x288a6:
            values = [machine.reg_read(getattr(arm, f'UC_ARM_REG_R{i}')) for i in range(8)]
            calls.append(values)
            if values[0] == 0x82000115:
                machine.emu_stop()  # the successful kernel transition never returns
                return
            assert values[0] in (0x82000101, 0x8200010c)
            machine.reg_write(arm.UC_ARM_REG_R0,
                              crypto_result if values[0] == 0x8200010c else 0)
            machine.reg_write(arm.UC_ARM_REG_PC, (address+size) | 1)

    def write(machine, access, address, size, value, _):
        assert contains(((STACK-0x1000, STACK), (flag, flag+4)), address, size), hex(address)

    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.reg_write(arm.UC_ARM_REG_SP, STACK)
    uc.reg_write(arm.UC_ARM_REG_LR, STOP | 1)
    uc.emu_start((LK_BIAS+0x3760) | 1, STOP, count=1000)
    assert uc.reg_read(arm.UC_ARM_REG_PC) == STOP
    assert uc.reg_read(arm.UC_ARM_REG_SP) == STACK
    expected = [] if disabled else [[0x8200010c]+[0]*7]
    expected += [[0x82000101]+[0]*7]
    assert calls == expected
    assert struct.unpack('<I', uc.mem_read(flag, 4))[0] == (disabled or crypto_result == 0)
    for i, value in enumerate((KERNEL, FDT, 0, 1)):
        uc.reg_write(getattr(arm, f'UC_ARM_REG_R{i}'), value)
    uc.reg_write(arm.UC_ARM_REG_LR, STOP | 1)
    uc.emu_start((LK_BIAS+0x1ba20) | 1, STOP, count=1000)
    assert uc.reg_read(arm.UC_ARM_REG_PC) == LK_BIAS+0x288a6
    assert calls[-1] == [0x82000115, KERNEL, FDT, 0, 1, 0, 0, 0]
    return calls


class Atf:
    # Actual selected dispatch arms, their callees, memcpy and zeroing code.
    # Unexpected branches, assertions, EL3 entry and exception return fail.
    CODE = ((0x1274, 0x129c), (0x2ea8, 0x2fac), (0x3150, 0x316c),
            (0x4e7c, 0x4edc), (0x5068, 0x511c), (0x51a4, 0x52cc),
            (0x58d8, 0x58f0), (0x5a88, 0x5a98), (0x76b4, 0x7708),
            (0x8f64, 0x8fd0), (0x90f4, 0x9140), (0x9534, 0x9660),
            (0x9690, 0x96b8), (0xc61c, 0xc6dc), (0x1344c, 0x1347c),
            (0x13568, 0x136e0), (0x13ad8, 0x14120), (0x15678, 0x1567c),
            (0x15704, 0x1570c), (0x157b4, 0x157d8),
            (0x16734, 0x1673c), (0x167f4, 0x16850), (0x16960, 0x16978))
    DATA = ((0x47da0000, 0x47da0008), (0x47da1750, 0x47da1758),
            (0x47da5ad8, 0x47da5b01), (0x47da5c90, 0x47da5cc0),
            (0x47dabc60, 0x47dabc70))
    MMIO = ((0x10001000, 0x10002000), (0x10006000, 0x10007000),
            (0x1000e000, 0x1000f000), (0x10019000, 0x1001a000),
            (0x1001c000, 0x1001d000), (0x10200000, 0x10201000),
            (0x10207000, 0x10208000))

    def __init__(self, image, seed, el2, uart):
        self.uc = uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        uc.mem_map(0x47d80000, 0x50000)
        uc.mem_map(0x60000000, 0x20000)
        # Exclude the following cert1 container. The remaining mapped BSS is
        # zero, not populated with certificate bytes as executable state.
        uc.mem_write(ATF_BIAS, image[:0x21800])
        for first, end in self.MMIO:
            uc.mem_map(first, end-first)
            uc.mem_write(first, struct.pack('<I', seed)*((end-first)//4))
        uc.mem_write(CONTEXT, b'\xa5'*0x220)
        self.w64(CPU+8, CONTEXT)
        uc.mem_write(0x47dabe90+0x4c, struct.pack('<I', uart))
        self.writes, self.models, self.executed = [], [], set()
        self.el2 = el2
        uc.hook_add(UC_HOOK_CODE, self.code)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.write)

    def w64(self, address, value):
        self.uc.mem_write(address, struct.pack('<Q', value))

    def code(self, uc, address, size, _):
        at = address-ATF_BIAS
        self.executed.add(at)
        if at in (0xbe84, 0x96b8):
            if at == 0x96b8:
                assert uc.reg_read(arm64.UC_ARM64_REG_X0) == 1
            self.models.append('logging' if at == 0xbe84 else 'context_restore')
            uc.reg_write(arm64.UC_ARM64_REG_PC, uc.reg_read(arm64.UC_ARM64_REG_X30))
            return
        assert contains(self.CODE, at, size), f'Unexpected ATF instruction {at:#x}'
        # Only these three system-register reads are modeled. The context
        # builder, permission table loops, memory helpers and MMIO run intact.
        reads = {0x5078: (arm64.UC_ARM64_REG_X21, self.el2 << 8, 'ID_AA64PFR0_EL1'),
                 0x958c: (arm64.UC_ARM64_REG_X19, 0x501, 'SCR_EL3'),
                 0x9130: (arm64.UC_ARM64_REG_X1, CPU, 'TPIDR_EL3')}
        if at in reads:
            reg, value, name = reads[at]
            uc.reg_write(reg, value)
            uc.reg_write(arm64.UC_ARM64_REG_PC, address+size)
            self.models.append(name)

    def write(self, uc, access, address, size, value, _):
        ranges = self.DATA+self.MMIO+((STACK-0x1000, STACK+0x80),
                                     (CONTEXT, CONTEXT+0x220))
        assert contains(ranges, address, size), f'Unexpected ATF write {address:#x}'
        if contains(self.MMIO, address, size):
            assert size == 4
            self.writes.append([address, value & 0xffffffff,
                                uc.reg_read(arm64.UC_ARM64_REG_PC)-ATF_BIAS])

    def run(self, start, end):
        self.uc.reg_write(arm64.UC_ARM64_REG_SP, STACK)
        self.uc.reg_write(arm64.UC_ARM64_REG_X29, STACK)
        self.uc.emu_start(ATF_BIAS+start, ATF_BIAS+end, count=100000)
        assert self.uc.reg_read(arm64.UC_ARM64_REG_PC) == ATF_BIAS+end
        assert self.uc.reg_read(arm64.UC_ARM64_REG_SP) == STACK

    def handoff(self):
        self.uc.reg_write(arm64.UC_ARM64_REG_X19, 0x8200010c)
        self.run(0x58d8, 0x5a78)
        assert self.uc.reg_read(arm64.UC_ARM64_REG_X0) == 0
        assert not self.writes  # crypto-disable service sets a software flag
        assert self.uc.mem_read(0x47dabc60, 4) == b'\1\0\0\0'
        self.uc.reg_write(arm64.UC_ARM64_REG_X20, 0)
        self.run(0x5a88, 0x5a98)
        assert self.uc.reg_read(arm64.UC_ARM64_REG_X0) == 0
        assert len(self.writes) == 874
        postinit = list(self.writes)
        for offset, value in ((0x68, KERNEL), (0x60, FDT), (0x58, 0), (0x50, 1)):
            self.w64(STACK+offset, value)
        self.run(0x1274, 0x17ec)
        assert len(self.writes) == 899
        context = bytes(self.uc.mem_read(CONTEXT, 0x220))
        assert struct.unpack_from('<8Q', context) == (FDT, 0, 0, 0, 0, 0, 0, 0)
        assert struct.unpack_from('<Q', context, 0x120)[0] == KERNEL
        assert struct.unpack_from('<Q', context, 0x118)[0] == (0x3c9 if self.el2 else 0x3c5)
        assert struct.unpack_from('<Q', context, 0x100)[0] == (0x501 if self.el2 else 0x401)
        assert struct.unpack_from('<Q', context, 0x140)[0] == 0x30d00800
        assert self.models.count('context_restore') == 1
        assert {0x3150, 0x4e7c, 0x13568, 0x2ed8, 0x5068, 0x9534,
                0xc61c, 0x16734} <= self.executed
        # A second request must not rebuild or alter the prepared context.
        before = list(self.writes)
        self.run(0x1274, 0x17ec)
        assert self.writes == before
        assert bytes(self.uc.mem_read(CONTEXT, 0x220)) == context
        assert self.models.count('context_restore') == 1
        return postinit, self.writes[len(postinit):], context


def main():
    tee = (STOCK/'tee.img').read_bytes()
    stock_lk = (STOCK/'lk.img').read_bytes()
    patched_lk = (ROOT/'dist/lk/lk.bin').read_bytes()
    assert len(tee) == 140944 and hashlib.sha256(tee).hexdigest() == TEE_SHA
    assert len(stock_lk) == 864000 and hashlib.sha256(stock_lk).hexdigest() == LK_SHA
    assert tee[8:12] == b'atf\0' and tee[0x21808:0x2180e] == b'cert1\0'
    assert struct.unpack_from('<I', tee, 4)[0]+512 == 0x21800
    assert struct.unpack_from('<Q', tee, 0x215c8)[0] == 0x47dabe90
    for profile in ('ram', 'expdb'):
        with (ROOT/f'dist/mtkclient/boot-{profile}.img').open('rb') as stream:
            header = stream.read(1660)
        assert header[:8] == b'ANDROID!'
        assert struct.unpack_from('<I', header, 40)[0] == 2
        assert struct.unpack_from('<I', header, 12)[0] == KERNEL
        assert struct.unpack_from('<I', header, 32)[0] == FDT
        assert struct.unpack_from('<Q', header, 1652)[0] == FDT
    assert patched_lk == (ROOT/'dist/mtkclient/lk.bin').read_bytes()
    for first, end in ((0x3760, 0x37b0), (0x14344, 0x14368),
                       (0x1ba20, 0x1ba84), (0x2889c, 0x28984)):
        assert patched_lk[first:end] == stock_lk[first:end]
    lk_cases = []
    for name, image in (('stock', stock_lk), ('patched', patched_lk)):
        for disabled in (0, 1):
            for result in (0, 0xffffffff):
                lk_cases.append({'image': name, 'crypto_already_disabled': disabled,
                                 'crypto_result': result,
                                 'smc_arguments': trace_lk(image, disabled, result)})
    cases = []
    for seed in (0, 0xffffffff, 0xa5a5a5a5):
        for el2 in (0, 1):
            for uart in (0, 0x11002000):
                machine = Atf(tee, seed, el2, uart)
                postinit, kernel, context = machine.handoff()
                addresses = {address for address, _, _ in kernel}
                # SPM writes concern CPUs and their clusters. DIS_PWR_CON at
                # 0x1000630c is not written; neither are display/GCE/M4U blocks.
                expected = {0x10006000, 0x100062b4, 0x10200b58}
                expected.update(range(0x10006204, 0x1000622c, 4))
                assert addresses == expected
                assert 0x1000630c not in {address for address, _, _ in postinit+kernel}
                cases.append({'mmio_seed': seed, 'el2_present': bool(el2), 'uart': uart,
                              'postinit_writes': postinit, 'kernel_writes': kernel,
                              'context_sha256': hashlib.sha256(context).hexdigest(),
                              'models': sorted(set(machine.models))})
    report = {'stock_lk_sha256': LK_SHA, 'tee_sha256': TEE_SHA,
              'patched_lk_sha256': hashlib.sha256(patched_lk).hexdigest(),
              'atf_fixture_bias': ATF_BIAS, 'lk_cases': lk_cases, 'atf_cases': cases,
              'hardware_tested': False,
              'scope': 'selected LK callers and ATF dispatch arms/context construction; '
                       'no full boot, EL3 entry/restore/ERET, cache coherency or DMA validation'}
    (ROOT/'out/atf-handoff-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS: {len(lk_cases)} LK SMC argument cases and {len(cases)} ATF handoff fixtures')
    print('Stock routines prepare x0=FDT, x1-x3=0 and masked EL1h/EL2h entry; no display stop is established.')
    print('MMIO, system-register reads and secure-context restore are models; no device or host SMC used.')


if __name__ == '__main__':
    main()
