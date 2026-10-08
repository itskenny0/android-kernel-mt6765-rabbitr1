#!/usr/bin/env python3
"""Recover selected display setup from the shipped r1 LK, without hardware."""
import hashlib
import json
from pathlib import Path
import struct

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)

ROOT = Path('/rabbitr1')
SHA256 = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
PRIMARY = [0, 1, 6, 4, 5, 11, 12, 13, 14, 15, 20, 17]
PRIMARY_WRITES = [(0xf3c, 2), (0xf40, 1), (0xf50, 1), (0xf48, 0),
                  (0xf4c, 1), (0xf54, 1), (0xf60, 0), (0xf64, 0), (0xf68, 1)]
ARG_REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3]
SAVED_REGS = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
              UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]


class StockLK:
    def __init__(self, raw, seed=0):
        assert hashlib.sha256(raw).hexdigest() == SHA256, 'Unknown LK'
        self.bias, self.stack, self.stop, self.mmio = 0x47fffe00, 0x60008000, 0x70000000, 0x14000000
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        uc.mem_map(self.bias & ~4095, 0x200000)
        uc.mem_write(self.bias, raw)
        # ARM startup 0x2f8..0x30c clears this linker-delimited BSS. The file's
        # appended FDT overlaps these runtime addresses; it is not BSS data.
        start, end = struct.unpack_from('<II', raw, 0x32c)
        assert (start, end) == (0x480b7874, 0x4816a594)
        uc.mem_write(start, bytes(end-start))
        uc.mem_map(0x60000000, 0x20000)
        uc.mem_map(self.stop, 0x1000)
        uc.mem_map(self.mmio, 0x1000)
        uc.mem_write(self.mmio, struct.pack('<I', seed)*1024)
        self.events, self.calls = [], []
        self.primary = False
        self.pointers = ({0x97f18+0x4c*i+0x44 for i in range(4)} |
                         {0x97db8+0x2c*i+0x24 for i in range(2)} |
                         {0x97e10+0x2c*i+0x24 for i in range(6)})
        self.cache = {0x97f18+0x4c*i+0x48 for i in range(4)}
        uc.hook_add(UC_HOOK_CODE, self.code)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.write)
        self.call(0x9ca8)
        assert not self.events

    def word(self, address):
        return struct.unpack('<I', self.uc.mem_read(address, 4))[0]

    def code(self, uc, address, size, _):
        at = address-self.bias
        if self.primary and at in (0x923c, 0x11c90, 0x8714, 0x89a0, 0x8f20, 0x2b2f4, 0x8a00):
            a, b, c = [uc.reg_read(r) for r in ARG_REGS[:3]]
            self.calls.append((at, a, b, c))
            result = 0
            if at == 0x11c90:  # LCM discovery is modeled, not run on a bus.
                assert (a, b) == (0, 0)
                result = 0x60010000
            elif at == 0x8714:  # Path allocation, preserving the chosen scenario.
                assert b == 0
                result = 0x60012000
                uc.mem_write(result+0xd8, struct.pack('<I', a))
            elif at == 0x89a0:
                assert a == 0x60012000 and b == 17  # DSI0
                assert self.path(0)[-1] == b  # The destination setter is a no-op here.
            elif at == 0x8f20:
                assert (a, b) == (0x60012000, 0x60013000)
            elif at == 0x2b2f4:
                assert (a, b, c) == (self.bias+0xbb214, 0, 0x24)
                uc.mem_write(a, bytes(c))
                result = a
            elif at == 0x8a00:
                assert (a, b) == (0x60012000, 0)
                uc.emu_stop()  # Intentional boundary before clocks/module init.
                return
            uc.reg_write(UC_ARM_REG_R0, result)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return
        allowed = [(0x964c, 0x97ee), (0x9808, 0x9c5e), (0x9ca8, 0x9d4a),
                   (0xa04c, 0xa0b4), (0xbd38, 0xbd62)]
        if self.primary:
            allowed += [(0x10a68, 0x10e18), (0x10720, 0x10738), (0x11e14, 0x11e22),
                        (0x11914, 0x11924), (0x10a34, 0x10a5a),
                        (0x86dc, 0x86ec), (0x12064, 0x120c2)]
        assert any(lo <= at < hi for lo, hi in allowed), f'Unexpected LK instruction {at:#x}'

    def write(self, uc, access, address, size, value, _):
        if self.mmio <= address < self.mmio+0x1000:
            offset = address-self.mmio
            assert size == 4 and offset in range(0xf3c, 0xf70, 4), hex(address)
            self.events.append((offset, value))
        elif self.stack-0x1000 <= address and address+size <= self.stack:
            pass
        else:
            at = address-self.bias
            allowed = self.pointers | self.cache
            if self.primary:
                allowed |= {0xbb200, 0xbb21c, 0xbb224, 0xbb230, 0x6001200c-self.bias}
            assert size == 4 and at in allowed, (hex(address), size, hex(value))

    def call(self, offset, *values):
        uc = self.uc
        uc.reg_write(UC_ARM_REG_SP, self.stack)
        uc.reg_write(UC_ARM_REG_LR, self.stop | 1)
        for reg, value in zip(ARG_REGS, list(values)+[0]*4):
            uc.reg_write(reg, value)
        for i, reg in enumerate(SAVED_REGS):
            uc.reg_write(reg, 0xa000+i)
        start = len(self.events)
        uc.emu_start((self.bias+offset) | 1, self.stop, count=100000)
        assert uc.reg_read(UC_ARM_REG_PC) == self.stop, 'LK did not return'
        assert uc.reg_read(UC_ARM_REG_SP) == self.stack
        assert all(uc.reg_read(reg) == 0xa000+i for i, reg in enumerate(SAVED_REGS))
        return self.events[start:]

    def routes(self, connect, scenario=0):
        return self.call(0xa04c if connect else 0xa084, scenario)

    def path(self, scenario):
        values = struct.unpack('<20i', self.uc.mem_read(self.bias+0x98048+scenario*0x50, 80))
        return list(values[:values.index(-1)])

    def names(self, modules):
        result = []
        for module in modules:
            self.call(0xbd44, module)
            ptr = self.uc.reg_read(UC_ARM_REG_R0)
            assert self.bias <= ptr < self.bias+0xb7874
            result.append(bytes(self.uc.mem_read(ptr, 64)).split(b'\0')[0].decode('ascii'))
        return result

    def primary_selection(self, video_mode=1):
        """Run the primary-init prefix with a modeled, already discovered r1 LCM."""
        self.primary = True
        uc = self.uc
        uc.mem_write(0x60010000, struct.pack('<III', 0x60011000, 0x60013000, 1))
        uc.mem_write(0x60011000, struct.pack('<I', 2))  # LCM_TYPE_DSI
        uc.mem_write(0x6001116c, struct.pack('<I', video_mode))
        uc.reg_write(UC_ARM_REG_SP, self.stack)
        uc.reg_write(UC_ARM_REG_LR, self.stop | 1)
        for reg in ARG_REGS:
            uc.reg_write(reg, 0)
        uc.emu_start((self.bias+0x10a68) | 1, self.stop, count=10000)
        assert uc.reg_read(UC_ARM_REG_PC) == self.bias+0x8a00
        assert self.word(0x600120d8) == 0
        assert self.word(0x6001200c) == int(video_mode == 0)
        assert [c[0] for c in self.calls] == [0x923c, 0x2b2f4, 0x11c90,
                                             0x8714, 0x89a0, 0x8f20, 0x8a00]
        assert not self.events
        return {'dsi_mode': video_mode, 'scenario': 0, 'destination': 17,
                'mutex_mode': self.word(0x6001200c), 'modeled_calls': self.calls}


def check():
    raw = (ROOT/'firmware/stock-v0.8.293/lk.img').read_bytes()
    paths, cases = [], []
    for scenario in range(4):
        lk = StockLK(raw)
        if scenario < 3:
            modules = lk.path(scenario)
            paths.append({'scenario': scenario, 'modules': modules, 'names': lk.names(modules)})
    assert paths[0]['modules'] == PRIMARY
    expected = [PRIMARY_WRITES, [(0xf48, 0), (0xf4c, 0), (0xf60, 0), (0xf68, 0)],
                [(0xf3c, 2), (0xf40, 2), (0xf6c, 2)],
                PRIMARY_WRITES+[(0xf3c, 2), (0xf40, 3), (0xf6c, 2)]]
    for seed in (0, 0xffffffff, 0xa5a5a5a5, 0x5a5a5a5a):
        for scenario in range(4):
            lk = StockLK(raw, seed)
            connected = lk.routes(True, scenario)
            assert connected == expected[scenario]
            removed = lk.routes(False, scenario)
            mout = set(dict(connected)) & {0xf3c, 0xf40, 0xf44, 0xf50}
            assert set(dict(removed)) == mout
            assert all(value == 0 for value in dict(removed).values())
            assert lk.routes(True, scenario) == connected
            # Cached MOUT writes replace whole registers. Neither the primary
            # path nor its disconnect clears unrelated RSZ/WDMA route registers.
            for address in (0xf30, 0xf44, 0xf58):
                assert lk.word(lk.mmio+address) == seed
            cases.append({'seed': seed, 'scenario': scenario,
                          'connect': connected, 'disconnect': removed})
    primary = [StockLK(raw).primary_selection(mode) for mode in range(4)]
    result = {'lk_sha256': SHA256, 'paths': paths, 'primary_init_prefix': primary, 'cases': cases}
    (ROOT/'out/lk-display-path-audit.json').write_text(json.dumps(result, indent=2)+'\n')
    print('PASS: LK primary-init selection, 16 route/reconnect fixtures and callee-saved registers')
    print('LCM discovery/allocation and MMIO are modeled; full boot and final handoff state are not established.')
    return result


if __name__ == '__main__':
    check()
