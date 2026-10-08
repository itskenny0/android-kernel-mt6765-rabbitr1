#!/usr/bin/env python3
"""Trace shipped panel power callbacks; PMIC transport and GPIOs are modeled."""
import hashlib
import json
from pathlib import Path
import resource
import struct

from unicorn import (Uc, UC_ARCH_ARM, UC_ARCH_ARM64, UC_MODE_ARM, UC_MODE_THUMB,
                     UC_HOOK_CODE, UC_HOOK_MEM_WRITE)
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
    UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP,
                                UC_ARM64_REG_X0, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1')
LK_SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
IMAGE_SHA = '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
CALLBACKS = {'init_power': 0x290fc, 'suspend_power': 0x28fbc,
             'resume_power': 0x28f00}
REGISTERS = [0xb0, 0xb1, 0xb1, 0xb3, 0xb3, 0xb3, 0xb3]
SAVED = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
         UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class LKPanelPower:
    def __init__(self, raw):
        assert hashlib.sha256(raw).hexdigest() == LK_SHA, 'Unknown stock LK'
        self.bias, self.stack, self.stop = 0x47fffe00, 0x60008000, 0x70000000
        uc = self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        uc.mem_map(self.bias & ~4095, 0x200000)
        uc.mem_write(self.bias, raw)
        start, end = struct.unpack_from('<II', raw, 0x32c)
        assert (start, end) == (0x480b7874, 0x4816a594)
        # The appended FDT overlaps these addresses in the file. LK clears
        # the linker-delimited BSS before using its panel utility table.
        uc.mem_write(start, bytes(end-start))
        uc.mem_map(0x60000000, 0x10000)
        uc.mem_map(self.stop, 0x1000)
        util = self.bias+0xc8a50
        uc.mem_write(util, struct.pack('<I', (self.stop+0x100) | 1))
        uc.mem_write(util+0x10, struct.pack('<I', (self.stop+0x104) | 1))
        # LCM_DRIVER: name, set_util, params, init, suspend, resume, then
        # the three power callbacks. Check the actual shipped driver object.
        table = struct.unpack_from('<9I', raw, 0x99f5c)
        assert table[0] == self.bias+0x7eab8
        assert raw.startswith(b'ili9883_boe_mipi_hd_lcm_drv\0', 0x7eab8)
        assert table[6:] == tuple((self.bias+at) | 1 for at in CALLBACKS.values())
        self.descriptor = self.bias+0x9a1d0
        self.descriptor_before = bytes(uc.mem_read(self.descriptor, 0x40))
        uc.hook_add(UC_HOOK_CODE, self.code)
        uc.hook_add(UC_HOOK_MEM_WRITE, self.write)

    def string(self, pointer):
        assert self.bias <= pointer < self.bias+0xb7874
        return bytes(self.uc.mem_read(pointer, 128)).split(b'\0')[0].decode('ascii')

    def code(self, uc, address, size, _):
        at = address-self.bias
        a, b, c, d = [uc.reg_read(reg) for reg in
                      (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        result = 0
        if address == self.stop+0x100:
            assert a in (0, 1)
            self.events.append(('reset', a))
        elif address == self.stop+0x104:
            self.events.append(('delay_ms', a))
        elif at == 0x24b8:
            # Combined one-byte register-address write / one-byte read.
            # No register-value write is accepted at this modeled boundary.
            assert a == self.descriptor and (c, d) == (1, 1)
            assert self.stack-0x100 <= b < self.stack
            register = uc.mem_read(b, 1)[0]
            index = self.reads
            assert index < len(REGISTERS) and register == REGISTERS[index]
            failed = bool(self.failures & (1 << index))
            value = (self.seed+register+index) & 255
            self.events.append(('read', register, -1 if failed else value))
            self.reads += 1
            # A failed read can leave arbitrary transport-buffer contents.
            # The actual helper must log the failure without copying them.
            uc.mem_write(b, bytes([0xee if failed else value]))
            self.last_read = (register, value, failed)
            result = self.error & 0xffffffff if failed else 0
        elif at == 0x2a3bc:
            fmt = self.string(a)
            name = self.string(b)
            if name == 'lcm_mt6370_i2c_read_byte':
                register, value, failed = self.last_read
                assert c == register
                if failed:
                    assert fmt == '%s: I2CR[0x%02X] fail(%d)\n'
                    assert d == self.error & 0xffffffff
                else:
                    assert fmt == '%s: I2CR[0x%02X] = 0x%02X\n' and d == value
                self.logged_reads += 1
            else:
                assert name in ('lcm_init_power', 'lcm_suspend_power')
                expected = '[DISP]%s test' if name == 'lcm_init_power' else '[LK/LCM]%sn'
                assert fmt == expected, fmt
                self.logs.append(name)
        else:
            allowed = [(0x28f00, 0x28f16), (0x28fbc, 0x28fde),
                       (0x29094, 0x290e6), (0x290fc, 0x29166)]
            assert any(lo <= at < hi for lo, hi in allowed), hex(at)
            if at == 0x29094:
                self.read_destination = b
                self.previous_value = uc.mem_read(b, 1)[0]
            elif at in (0x2911a, 0x29122, 0x2912a, 0x29132, 0x2913a, 0x29148, 0x29156):
                register, value, failed = self.last_read
                assert uc.mem_read(self.read_destination, 1)[0] == (
                    self.previous_value if failed else value)
            return
        uc.reg_write(UC_ARM_REG_R0, result)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def write(self, uc, access, address, size, value, _):
        # Only callback stack writes are permitted. No MMIO, PMIC state or
        # other LK global write is hidden by the emulator's mapped image.
        assert self.stack-0x100 <= address and address+size <= self.stack, hex(address)

    def call(self, name, seed=0, failures=0, error=-1):
        self.seed, self.failures, self.error = seed, failures, error
        self.events, self.logs = [], []
        self.reads = self.logged_reads = 0
        uc = self.uc
        uc.mem_write(self.stack-0x100, bytes([0xa5])*0x100)
        uc.reg_write(UC_ARM_REG_SP, self.stack)
        uc.reg_write(UC_ARM_REG_LR, self.stop | 1)
        for i, reg in enumerate(SAVED):
            uc.reg_write(reg, 0xa000+i)
        uc.emu_start((self.bias+CALLBACKS[name]) | 1, self.stop, count=10000)
        assert uc.reg_read(UC_ARM_REG_PC) == self.stop
        assert uc.reg_read(UC_ARM_REG_SP) == self.stack
        assert all(uc.reg_read(reg) == 0xa000+i for i, reg in enumerate(SAVED))
        assert bytes(uc.mem_read(self.descriptor, 0x40)) == self.descriptor_before
        if name == 'init_power':
            expected = [('delay_ms', 50)]
            for i, register in enumerate(REGISTERS):
                if i in (5, 6):
                    expected.append(('delay_ms', 20))
                expected.append(('read', register,
                                 -1 if failures & (1 << i) else (seed+register+i) & 255))
            assert self.reads == self.logged_reads == 7
            assert self.logs == ['lcm_init_power']
        else:
            expected = [('reset', 0), ('delay_ms', 10 if name == 'suspend_power' else 15)]
            assert self.reads == self.logged_reads == 0
            assert self.logs == (['lcm_suspend_power'] if name == 'suspend_power' else [])
        assert self.events == expected, self.events
        return self.events


def check_kernel_stubs(raw):
    assert hashlib.sha256(raw).hexdigest() == IMAGE_SHA, 'Unknown stock Image'
    bias, stack, stop = 0x1000000, 0x20008000, 0x30000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias, (len(raw)+4095) & ~4095)
    uc.mem_write(bias, raw)
    uc.mem_map(0x20000000, 0x10000)
    uc.mem_map(stop, 0x1000)
    routines = {'display_bias_regulator_init': 0x6eec74,
                'display_bias_enable': 0x6eec8c, 'disp_late_bias_enable': 0x6eeca4,
                'display_bias_disable': 0x6eecbc}
    def code(uc, address, size, _):
        if address == bias+0x1f01c:  # _mcount only
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert bias+entry <= address < bias+entry+24, hex(address-bias)
    def write(uc, access, address, size, value, _):
        assert stack-16 <= address and address+size <= stack, hex(address)
    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    for entry in routines.values():
        uc.reg_write(UC_ARM64_REG_SP, stack)
        uc.reg_write(UC_ARM64_REG_X30, stop)
        uc.reg_write(UC_ARM64_REG_X0, 0xdeadbeef)
        uc.emu_start(bias+entry, stop, count=100)
        assert uc.reg_read(UC_ARM64_REG_PC) == stop
        assert uc.reg_read(UC_ARM64_REG_SP) == stack
        assert uc.reg_read(UC_ARM64_REG_X0) == 0
    return routines


def main():
    stock = ROOT/'firmware/stock-v0.8.293'
    lk = LKPanelPower((stock/'lk.img').read_bytes())
    baseline = {name: lk.call(name) for name in CALLBACKS}
    for seed in range(256):
        lk.call('init_power', seed=seed)
    # Every subset of failed reads, including all reads failing. The stock
    # callback ignores failures, continues reading, and returns normally.
    for failures in range(128):
        for error in (-1, -22, -110):
            lk.call('init_power', seed=0x5a, failures=failures, error=error)
    stubs = check_kernel_stubs((stock/'boot-unpacked/Image').read_bytes())
    report = {'stock_lk_sha256': LK_SHA, 'stock_image_sha256': IMAGE_SHA,
              'lk_callbacks': CALLBACKS, 'baseline_events': baseline,
              'lk_cases': 643, 'kernel_noop_callbacks': stubs,
              'scope': 'complete selected callbacks; I2C, GPIO, delay, log and ftrace modeled',
              'limits': 'no physical rail state, wiring, cold start or whole-firmware power audit'}
    (ROOT/'out/panel-power-audit.json').write_text(json.dumps(report, indent=2)+'\n')
    print('PASS: 643 LK power traces; register reads and reset/delays only')
    print('PASS: four shipped kernel display-bias helpers return zero without regulator access')
    print('Panel supply wiring and cold power-on remain unidentified.')


if __name__ == '__main__':
    main()
