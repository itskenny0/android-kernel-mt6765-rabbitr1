#!/usr/bin/env python3
"""Execute the patched LK display guard with controlled MMIO and failure injection."""
import importlib.util
import json
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, CS_GRP_CALL, CS_GRP_JUMP
from capstone.arm import ARM_OP_IMM
import lk_handoff as guard

from unicorn import arm_const as arm

ROOT = Path('/rabbitr1')
spec = importlib.util.spec_from_file_location('stock_stop', ROOT/'scripts/test-lk-display-stop.py')
stock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stock)
BIAS, STACK, PATH = stock.BIAS, stock.STACK, stock.PATH


class Checked(stock.LK):
    CODE = stock.LK.CODE + ((0x287c8, 0x28848), (0xc874, 0xc8c8), (0x1d4d0, 0x1d4d4))

    def __init__(self, raw, delays=(0, 0, 0), idle=(1, 1), rdma=1,
                 invalid=None, stuck_enable=False):
        self.delays, self.idle = delays, idle
        self.stuck_enable = stuck_enable
        self.reset_reads = [0, 0, 0]
        self.reset_seen = [False, False, False]
        self.gic, self.halted, self.active = False, False, False
        super().__init__(raw, states=idle)
        self.uc.reg_write(arm.UC_ARM_REG_CPSR, 0x33)  # SVC, Thumb, IRQ/FIQ unmasked
        self.w32(0x1400d010, rdma)
        self.powered = invalid != 'power'
        if invalid == 'context': self.w32(BIAS+0xbb200, 0)
        if invalid == 'display': self.w32(BIAS+0xbb210, 0)
        if invalid == 'handle': self.w32(BIAS+0xbb230, 0)
        if invalid == 'power': self.w32(PATH+8, 0)
        if isinstance(invalid, int): self.w32(PATH+0xd8, invalid)

    def code(self, uc, address, size, user):
        at = address-BIAS
        if at == 0x2d84:
            self.gic = True
            self.events.append(['gic'])
            uc.reg_write(arm.UC_ARM_REG_PC, uc.reg_read(arm.UC_ARM_REG_LR))
        elif at == 0x28838:  # observer stops before WFI, after the failure DSB
            self.halted = True
            uc.emu_stop()
        else:
            super().code(uc, address, size, user)

    def write(self, uc, access, address, size, value, user):
        if self.active and not stock.within(self.MMIO, address, size):
            assert STACK-0x1000 <= address and address+size <= STACK
        super().write(uc, access, address, size, value, user)
        if stock.within(self.MMIO, address, size):
            allowed = {base+offset for base in stock.OVL for offset in (4, 8, 0xc, 0x14)}
            allowed |= {0x1400d000, 0x1400d004, 0x1400d010, 0x14014010, 0x14014014}
            assert address in allowed, f'Unexpected MMIO write {address:#x}'
            assert uc.reg_read(arm.UC_ARM_REG_CPSR) & 0xc0 == 0xc0
            for i, base in enumerate(stock.OVL):
                if address == base+0x14 and value == 1:
                    self.reset_seen[i] = True
                    self.w32(base+0x240, 0)
            if address == 0x14014010 and value & 1:
                self.reset_seen[2] = True
                self.w32(0x1401400c, 0x80000000)

    def read(self, uc, access, address, size, value, user):
        super().read(uc, access, address, size, value, user)
        if address == 0x1400d010 and self.stuck_enable:
            self.w32(address, self.word(address) | 1)
        for i, status in enumerate((0x1400b240, 0x1400c240, 0x1401400c)):
            if address == status and self.reset_seen[i]:
                count = self.reset_reads[i]
                self.reset_reads[i] += 1
                complete = self.delays[i] is not None and count >= self.delays[i]
                self.w32(status, (self.idle[i] if i < 2 else 0) if complete else
                         (0 if i < 2 else 0x80000000))

    def handoff(self, success, early=False):
        uc = self.uc
        self.active = True
        uc.reg_write(arm.UC_ARM_REG_SP, STACK)
        uc.reg_write(arm.UC_ARM_REG_LR, stock.STOP | 1)
        for i, reg in enumerate(stock.SAVED): uc.reg_write(reg, 0xa000+i)
        uc.emu_start((BIAS+0x1d4d0)|1, BIAS+0x1d4d4, count=400000)
        assert self.gic is success and self.halted is not success
        assert uc.reg_read(arm.UC_ARM_REG_CPSR) & 0xc0 == 0xc0
        if success:
            assert uc.reg_read(arm.UC_ARM_REG_PC) == BIAS+0x1d4d4
            assert uc.reg_read(arm.UC_ARM_REG_SP) == STACK
            assert all(uc.reg_read(reg) == 0xa000+i for i, reg in enumerate(stock.SAVED))
            assert self.reset_seen == [True]*3
            assert all(self.reset_reads)
            assert self.word(0x1400d010) & 3 == 0
            assert self.word(0x1401400c) & 0x80000000 == 0
            assert self.events[-1] == ['gic']
            assert self.calls == [[0x8c28, PATH], [0xa56c, 0], [0xa56c, 1],
                                  [0xb128, 6], [0xc500, 17], [0xa5e8, 0], [0xa5e8, 1]]
            for base in stock.OVL:
                assert self.word(base+4) == self.word(base+8) == self.word(base+0xc) == 0
            assert self.word(0x1400d000) == self.word(0x1400d004) == 0
            assert self.word(0x14014014) & 3 == 0
            assert self.word(0x14014010) & 1 == 0
        else:
            assert uc.reg_read(arm.UC_ARM_REG_PC) == BIAS+0x28838
            assert uc.reg_read(arm.UC_ARM_REG_SP) == STACK-16
            assert self.logs[-1] == 'R1: DMA handoff failed\n'
            if early:
                assert not self.reads and not self.calls
                assert not any(event[0] == 'write' for event in self.events)
        assert self.word(PATH+8) == int(self.powered)
        return {'success': success, 'delays': self.delays, 'idle': self.idle,
                'reset_reads': self.reset_reads, 'logs': self.logs,
                'writes': [event for event in self.events if event[0] == 'write']}


def check(raw):
    cases = []
    for idle in ((1, 1), (2, 2), (1, 2), (3, 3)):
        for delays in ((0, 0, 0), (3, 7, 11), (2000, 2000, 1999), (2001, 2001, 0)):
            cases.append(Checked(raw, delays, idle).handoff(True))
    for delays in ((None, 0, 0), (0, None, 0), (0, 0, None), (0, 0, 2000)):
        cases.append(Checked(raw, delays).handoff(False))
    for invalid in ('context', 'display', 'handle', 'power', 1, 2, 3, 0xffffffff):
        cases.append(Checked(raw, invalid=invalid).handoff(False, early=True))
    for mode in (2, 3):
        cases.append(Checked(raw, rdma=mode).handoff(False))
    cases.append(Checked(raw, stuck_enable=True).handoff(False))
    for idle in ((4, 1), (1, 4)):
        cases.append(Checked(raw, idle=idle).handoff(False))
    return cases


def check_slot(stock_raw):
    # Conservative direct-reference scan. It does not resolve arbitrary
    # computed jumps; the exact firmware hash and relock guard remain required.
    references = []
    for mode, stride in ((CS_MODE_THUMB, 2), (CS_MODE_ARM, 4)):
        decoder = Cs(CS_ARCH_ARM, mode)
        decoder.detail = True
        for at in range(0x200, 0x61000, stride):
            for ins in decoder.disasm(stock_raw[at:at+4], at, count=1):
                if not (ins.group(CS_GRP_CALL) or ins.group(CS_GRP_JUMP)):
                    continue
                if not ins.operands or ins.operands[-1].type != ARM_OP_IMM:
                    continue
                target = ins.operands[-1].imm & ~1
                if guard.START <= target < guard.END:
                    assert guard.RELOCK_START <= at < guard.END, hex(at)
                    references.append([mode, at, target])
    for at in range(0, len(stock_raw)-3, 4):
        target = int.from_bytes(stock_raw[at:at+4], 'little') & ~1
        assert not BIAS+guard.START <= target < BIAS+guard.END, hex(at)
    return references


def mutations(raw):
    # Break the actual instructions, then demand rejection by the execution
    # oracle (independently of the payload checksum gate).
    changes = [
        ('original_hook', guard.HOOK, guard.AFTER, guard.BEFORE, {}),
        ('ignore_overlay_timeout', 0x28806, bytes.fromhex('12d0'), bytes.fromhex('00bf'),
         {'delays': (None, 0, 0)}),
        ('ignore_dsi_busy', 0x28822, bytes.fromhex('0bd5'), bytes.fromhex('0be0'),
         {'delays': (0, 0, None)}),
        ('allow_memory_rdma', 0x287f0, bytes.fromhex('13f0030f'), bytes.fromhex('13f0010f'),
         {'rdma': 3}),
        ('leave_fiq_enabled', 0x287ca, bytes.fromhex('73b6'), bytes.fromhex('72b6'), {}),
    ]
    rejected = []
    for name, at, before, after, options in changes:
        assert raw[at:at+len(before)] == before, name
        altered = bytearray(raw)
        altered[at:at+len(before)] = after
        assert not guard.has_display_guard(altered)
        try:
            Checked(bytes(altered), **options).handoff(not options)
        except AssertionError:
            rejected.append(name)
        else:
            raise AssertionError('Broken guard passed: '+name)
    return rejected


def main():
    stock_raw = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    assert guard.digest(stock_raw) == stock.SHA
    raw = (ROOT/'dist/lk/lk.bin').read_bytes()
    assert guard.has_display_guard(raw)
    references = check_slot(stock_raw)
    cases = check(raw)
    rejected = mutations(raw)
    report = {
        'stock_lk_sha256': stock.SHA, 'patched_lk_sha256': guard.digest(raw),
        'cases': cases, 'rejected_mutations': rejected,
        'old_slot_direct_references': references, 'hardware_tested': False,
        'scope': 'actual hook, stop dispatcher and reset instructions; controlled initialized path, MMIO, delays, logging and GIC tail-call boundary; no physical DMA or complete boot proof',
    }
    (ROOT/'out/lk-display-handoff-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'PASS: {len(cases)} checked LK display handoff fixtures; {len(rejected)} broken variants rejected')
    print('Clock/power release is forbidden in every fixture. Physical DMA and boot remain untested.')


if __name__ == '__main__':
    main()
