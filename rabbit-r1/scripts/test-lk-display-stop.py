#!/usr/bin/env python3
"""Reproduce LK teardown failures; this is not a DMA-quiescence test."""
import hashlib
import json
from pathlib import Path
import struct

from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE)
from unicorn import arm_const as arm

ROOT = Path('/rabbitr1')
SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
BIAS, STACK, STOP, PATH = 0x47fffe00, 0x60008000, 0x70000000, 0x60012000
OVL = (0x1400b000, 0x1400c000)
SAVED = [getattr(arm, f'UC_ARM_REG_R{i}') for i in range(4, 12)]


def within(ranges, address, size):
    return any(first <= address and address+size <= end for first, end in ranges)


class LK:
    CODE = ((0x8668, 0x86c4), (0x8c28, 0x8c7e), (0x8e24, 0x8e82),
            (0x9e3c, 0x9e60), (0xa038, 0xa048), (0xa2e4, 0xa2f6),
            (0xa358, 0xa38e), (0xa56c, 0xa5d2), (0xa5e8, 0xa63e),
            (0xa648, 0xa6c0), (0xb098, 0xb0ae), (0xb128, 0xb196),
            (0xb82c, 0xb836), (0xbbb0, 0xbd26), (0xbd6c, 0xbd8c),
            (0xbde8, 0xbdf2), (0xc500, 0xc550), (0xc8cc, 0xc98c),
            (0xc9b4, 0xca58), (0xce58, 0xcecc), (0xd2e0, 0xd41c),
            (0xe5f8, 0xe73e), (0xe8f8, 0xe9e6), (0x107a4, 0x10824))
    MMIO = ((0x14000000, 0x14001000), (0x1400b000, 0x1400e000),
            (0x14014000, 0x14015000), (0x11c80000, 0x11c81000))
    CALLS = (0x8c28, 0x8e24, 0xa56c, 0xa648, 0xa5e8, 0xb128,
             0xb098, 0xc500, 0xe8f8, 0xbbb0, 0x8668)

    def __init__(self, raw, states=(1, 1), initialized=True):
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        uc.mem_map(BIAS & ~4095, 0x200000)
        uc.mem_write(BIAS, raw)
        first, end = struct.unpack_from('<II', raw, 0x32c)
        assert (first, end) == (0x480b7874, 0x4816a594)
        uc.mem_write(first, bytes(end-first))
        uc.mem_map(0x60000000, 0x20000)
        uc.mem_map(STOP, 0x1000)
        for first, end in self.MMIO:
            uc.mem_map(first, end-first)
        # Controlled, already initialized primary path. LCM transport itself
        # is modeled; it must not silently clear the injected busy registers.
        for offset, value in ((0xbb200, 1), (0xbb210, int(initialized)),
                              (0xbb224, 0x60010000), (0xbb230, PATH),
                              (0xba8ec, 1), (0xba7f0, 1), (0xba8dc, PATH)):
            self.w32(BIAS+offset, value)
        self.w32(PATH+0xd8, 0)  # primary scenario
        self.w32(PATH+8, 1)  # powered
        for base, state in zip(OVL, states):
            self.w32(base+0x240, state)
            self.w32(base+0xc, 1)
        self.w32(0x1400d010, 1)  # running, reset state never completes
        self.w32(0x1401400c, 0x80000000)  # permanently busy DSI
        self.w32(0x14014014, 1)  # video mode
        self.events, self.reads, self.calls, self.logs = [], [], [], []
        uc.hook_add(UC_HOOK_CODE, self.code)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.write)
        uc.hook_add(UC_HOOK_MEM_READ, self.read)
        self.run(0xbd6c)
        # The real initializer copies driver pointers from the module table.
        assert struct.unpack_from('<I', raw, 0xb7500)[0] == BIAS+0x98350
        for module in range(29):
            assert self.word(BIAS+0xbb010+4*module) == struct.unpack_from(
                '<I', raw, 0x98350+16*module+12)[0]
        self.events.clear()
        self.calls.clear()
        self.reads.clear()

    def w32(self, address, value):
        self.uc.mem_write(address, struct.pack('<I', value))

    def word(self, address):
        return struct.unpack('<I', self.uc.mem_read(address, 4))[0]

    def code(self, uc, address, size, _):
        at = address-BIAS
        if at in (0x4a518, 0x4a514, 0x2a3bc, 0x11ee4):
            value = uc.reg_read(arm.UC_ARM_REG_R0)
            if at == 0x2a3bc:
                assert BIAS <= value < BIAS+0xb7874
                message = bytes(uc.mem_read(value, 150)).split(b'\0')[0].decode()
                self.logs.append(message)
                self.events.append(['log', message])
            elif at == 0x11ee4:
                assert value == 0x60010000
                self.events.append(['modeled_lcm_suspend'])
            else:
                self.events.append(['delay_us' if at == 0x4a518 else 'delay_ms', value])
            uc.reg_write(arm.UC_ARM_REG_R0, 0)
            uc.reg_write(arm.UC_ARM_REG_PC, uc.reg_read(arm.UC_ARM_REG_LR))
            return
        assert within(self.CODE, at, size), f'Unexpected LK instruction {at:#x}'
        if at in self.CALLS:
            self.calls.append([at, uc.reg_read(arm.UC_ARM_REG_R0)])

    def write(self, uc, access, address, size, value, _):
        if within(self.MMIO, address, size):
            assert size == 4
            self.events.append(['write', address, value, uc.reg_read(arm.UC_ARM_REG_PC)-BIAS])
        else:
            ranges = ((STACK-0x1000, STACK), (PATH+8, PATH+12),
                      (BIAS+0xba7f0, BIAS+0xba7f4),
                      (BIAS+0xbb010, BIAS+0xbb084),
                      (BIAS+0xbb088, BIAS+0xbb08c),
                      (BIAS+0xbb214, BIAS+0xbb218),
                      (0x48159948, 0x48159a84))  # DSI register backup through +0x138
            assert within(ranges, address, size), f'Unexpected write {address:#x}'

    def read(self, uc, access, address, size, value, _):
        if within(self.MMIO, address, size):
            self.reads.append([address, size, uc.reg_read(arm.UC_ARM_REG_PC)-BIAS])

    def run(self, at, argument=0):
        uc = self.uc
        for reg, value in ((arm.UC_ARM_REG_SP, STACK), (arm.UC_ARM_REG_LR, STOP | 1),
                           (arm.UC_ARM_REG_R0, argument), (arm.UC_ARM_REG_R1, 0)):
            uc.reg_write(reg, value)
        for i, reg in enumerate(SAVED):
            uc.reg_write(reg, 0xa000+i)
        uc.emu_start((BIAS+at) | 1, STOP, count=400000)
        assert uc.reg_read(arm.UC_ARM_REG_PC) == STOP
        assert uc.reg_read(arm.UC_ARM_REG_SP) == STACK
        assert all(uc.reg_read(reg) == 0xa000+i for i, reg in enumerate(SAVED))
        return uc.reg_read(arm.UC_ARM_REG_R0)


def check(raw):
    cases = []
    for state in (0, 1, 2):
        for module in (0, 1):
            lk = LK(raw, (state, state))
            assert lk.run(0xa648, module) == 0
            writes = [event for event in lk.events if event[0] == 'write']
            base = OVL[module]
            assert [(a, v) for _, a, v, _ in writes] == [
                (base+0xc, 0), (base+0x14, 1), (base+0x14, 0),
                (base+4, 0), (base+8, 0), (0x14000104, 0x80 << module)]
            assert len(lk.logs) == int(state == 0)
            assert lk.events.count(['delay_us', 10]) == (2001 if state == 0 else 0)
            assert lk.word(base+0x240) == state
            cases.append({'kind': 'overlay_power_off', 'module': module, 'state': state,
                          'result': 0, 'writes': writes, 'logs': lk.logs})
    for states in ((0, 0), (0, 1), (1, 0), (1, 2)):
        lk = LK(raw, states)
        assert lk.run(0x107a4) == 0
        assert len(lk.logs) == states.count(0)
        assert lk.events.count(['delay_us', 10]) == 2001*states.count(0)
        assert lk.events[0] == ['modeled_lcm_suspend']
        assert lk.calls[:6] == [[0x8c28, PATH], [0xa56c, 0], [0xa56c, 1],
                               [0xb128, 6], [0xc500, 17], [0x8e24, PATH]]
        reads = [address for address, _, _ in lk.reads]
        assert reads.count(0x1400d010) == 1  # disable RMW, no reset/idle poll
        assert 0x1401400c not in reads  # no DSI busy read in this path
        assert lk.word(0x1400d010) == 0 and lk.word(0x1401400c) == 0x80000000
        assert lk.word(BIAS+0xbb214) == 0 and lk.word(PATH+8) == 0
        gates = [event for event in lk.events if event[0] == 'write' and event[1] == 0x14000104]
        assert gates and gates[-1][2] == 0x307bf580
        cases.append({'kind': 'primary_suspend', 'overlay_states': states, 'result': 0,
                      'writes': [event for event in lk.events if event[0] == 'write'],
                      'reads': lk.reads, 'calls': lk.calls, 'logs': lk.logs,
                      'modeled_lcm_suspend': True})
    lk = LK(raw, initialized=False)
    assert lk.run(0x107a4) == 0xffffffff
    assert not lk.reads and not any(event[0] == 'write' for event in lk.events)
    assert not lk.calls and len(lk.logs) == 1
    cases.append({'kind': 'uninitialized', 'result': -1, 'logs': lk.logs})
    return cases


def main():
    raw = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    assert len(raw) == 864000 and hashlib.sha256(raw).hexdigest() == SHA
    cases = check(raw)
    report = {'stock_lk_sha256': SHA, 'cases': cases, 'hardware_tested': False,
              'finding': 'Stock teardown can return success and request clock gating after overlay reset timeouts; no RDMA/DSI completion wait observed.',
              'scope': 'selected stock callbacks and primary suspend; modeled LCM, delays, logging and MMIO; no physical DMA or whole-boot validation'}
    (ROOT/'out/lk-display-stop-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print(f'REPRODUCED: {len(cases)} stock teardown fixtures, including false-success reset timeouts')
    print('Clock-gate writes follow failed overlay resets; RDMA/DSI completion is not checked here.')
    print('This known failure is NOT a passing DMA handoff or a beta-readiness result.')


if __name__ == '__main__':
    main()
