#!/usr/bin/env python3
"""Replay the shipped LK's A/B record code against an in-memory para partition.

Only partition I/O, device discovery, the eMMC boot selector and logging are
stubbed. Slot selection, parsing, mutations, memcpy and CRC run LK instructions.
This does not exercise the complete boot path, eMMC or power-loss recovery.
"""
import hashlib
import json
from pathlib import Path
import resource
import struct
import sys
import zlib

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_PC, UC_ARM_REG_SP, UC_ARM_REG_LR,
                              UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                              UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5,
                              UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8,
                              UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11)

ROOT = Path('/rabbitr1')
STOCK_SHA = '534c72bea2bbb2173786594f650c2c1ec454258aefaa05de699349e26b71417e'
BIAS, DATA, STACK, STOP = 0x47fffe00, 0x50000000, 0x60008000, 0x70000000
SAVED = [UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
         UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11]
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def crc(record):
    return bytes(record[:28]) + struct.pack('<I', zlib.crc32(record[:28]))


def record(a=0x7f, b=0x7e):
    data = bytearray(32)
    data[:4] = b'_a\0\0'
    struct.pack_into('<I', data, 4, 0x42414342)
    data[8:10] = bytes([1, 2])
    data[12], data[14] = a, b
    # Sentinel reserved bytes, including the stock HAL's avbbctl flag.
    data[20:28] = bytes([1, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
    return crc(data)


class Emulator:
    def __init__(self, image, control, read_error=False, write_error=False,
                 region_error=False):
        self.uc = uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        for address, size in [(BIAS & ~4095, 0x200000), (DATA, 0x10000),
                              (STACK & ~0xffff, 0x10000), (STOP, 0x1000)]:
            uc.mem_map(address, size)
        uc.mem_write(BIAS, image)
        bss_start, bss_end = struct.unpack_from('<II', image, 0x32c)
        assert (bss_start, bss_end) == (0x480b7874, 0x4816a594)
        uc.mem_write(bss_start, bytes(bss_end - bss_start))
        self.media = bytearray(b'\xa5' * 0x80000)
        self.media[2048:2080] = control
        self.before = bytes(self.media)
        self.events = []
        self.read_error, self.write_error = read_error, write_error
        self.region_error = region_error
        # A controlled eMMC device fixture (type 1 at *(device + 8) + 11).
        uc.mem_write(DATA, struct.pack('<III', 0, 0, DATA + 0x100))
        uc.mem_write(DATA + 0x10b, b'\x01')
        uc.mem_write(DATA + 0x200, b'_a\0_b\0_bad\0')
        for entry in (0x46428, 0x46574, 0x52e10, 0x531d0, 0x18aa4, 0x2a3bc,
                      0x2a510):
            uc.hook_add(UC_HOOK_CODE, self.hook, begin=BIAS + entry,
                        end=BIAS + entry)

    def text(self, address):
        return bytes(self.uc.mem_read(address, 160)).split(b'\0')[0].decode('ascii')

    def hook(self, uc, address, size, _):
        offset = address - BIAS
        result = 0
        if offset in (0x46428, 0x46574):
            name = self.text(uc.reg_read(UC_ARM_REG_R0))
            position = uc.reg_read(UC_ARM_REG_R2) | (uc.reg_read(UC_ARM_REG_R3) << 32)
            buffer, count = struct.unpack('<II', uc.mem_read(uc.reg_read(UC_ARM_REG_SP), 8))
            # LK calls this logical partition "misc"; stock Android's fstab
            # maps /misc to the physical GPT partition named para.
            assert (name, position, count) == ('misc', 2048, 32), (name, position, count)
            write = offset == 0x46574
            self.events.append(('write' if write else 'read', position, count))
            error = self.write_error if write else self.read_error
            if not error:
                if write:
                    self.media[position:position + count] = uc.mem_read(buffer, count)
                else:
                    uc.mem_write(buffer, bytes(self.media[position:position + count]))
            result = -1 if error else count
        elif offset == 0x52e10:
            result = DATA
        elif offset == 0x531d0:
            self.events.append(('region', uc.reg_read(UC_ARM_REG_R0)))
            result = -1 if self.region_error else 0
        elif offset == 0x2a510:
            raise AssertionError('LK assertion reached')
        # Logging and verbosity have no physical side effects in this replay.
        uc.reg_write(UC_ARM_REG_R0, result & 0xffffffff)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def run(self, offset, r0=0, r1=0):
        uc = self.uc
        saved = dict(zip(SAVED, range(0x12340000, 0x12340008)))
        for register, value in saved.items():
            uc.reg_write(register, value)
        uc.reg_write(UC_ARM_REG_R0, r0)
        uc.reg_write(UC_ARM_REG_R1, r1)
        uc.reg_write(UC_ARM_REG_SP, STACK)
        uc.reg_write(UC_ARM_REG_LR, STOP | 1)
        uc.emu_start((BIAS + offset) | 1, STOP, count=200000)
        assert uc.reg_read(UC_ARM_REG_PC) == STOP, 'LK did not return'
        assert uc.reg_read(UC_ARM_REG_SP) == STACK
        for register, value in saved.items():
            assert uc.reg_read(register) == value
        assert self.media[:2048] == self.before[:2048]
        assert self.media[2080:] == self.before[2080:]
        return uc.reg_read(UC_ARM_REG_R0)


def check(image):
    cases = 0
    # Priority comparison and tie handling are independent of success/tries.
    for a in range(16):
        for b in range(16):
            for flags in (0, 0x70, 0x80, 0xf0):
                emu = Emulator(image, record(a | flags, b | flags))
                selected = emu.run(0x3dce4)
                assert emu.text(selected) == ('_a' if a >= b else '_b')
                assert emu.events == [('read', 2048, 32)]
                assert bytes(emu.media) == emu.before
                cases += 1

    # The helpers expose separate priority, retry and success fields. A caller
    # must combine them; this is not a test of the complete rollback algorithm.
    for value in range(256):
        for slot in (0, 1):
            emu = Emulator(image, record(value, value))
            suffix = DATA + 0x200 + 3 * slot
            assert emu.run(0x3df30, suffix) == (value >> 4) & 7
            assert emu.run(0x3df94, suffix) == value >> 7
            assert emu.run(0x3dff8, suffix) == bool(value & 15)
            assert emu.events == [('read', 2048, 32)]  # LK caches the record
            assert bytes(emu.media) == emu.before
            cases += 1

    for slot in (0, 1):
        for other in range(256):
            initial = record(0xff, other) if slot == 0 else record(other, 0xff)
            emu = Emulator(image, initial)
            assert emu.run(0x3dd8c, DATA + 0x200 + slot * 3) == 0
            expected = bytearray(initial)
            expected[:3] = b'_a\0' if slot == 0 else b'_b\0'
            expected[12 + slot * 2] = 0x7f
            if other & 15 == 15:
                expected[14 - slot * 2] = (other & 0xf0) | 14
            assert bytes(emu.media[2048:2080]) == crc(expected)
            assert emu.events == [('read', 2048, 32), ('region', slot + 1),
                                  ('write', 2048, 32)]
            # Observe persisted metadata through a fresh simulated LK boot.
            reboot = Emulator(image, bytes(emu.media[2048:2080]))
            assert reboot.text(reboot.run(0x3dce4)) == ('_a' if slot == 0 else '_b')
            cases += 1

        for fault, events in (
                ('read_error', [('read', 2048, 32)]),
                ('region_error', [('read', 2048, 32), ('region', slot + 1)]),
                ('write_error', [('read', 2048, 32), ('region', slot + 1), ('write', 2048, 32)])):
            emu = Emulator(image, record(), **{fault: True})
            assert emu.run(0x3dd8c, DATA + 0x200 + slot * 3) == 0xffffffff
            assert emu.events == events
            assert bytes(emu.media) == emu.before
            cases += 1

    for suffix in (0, DATA + 0x206):
        emu = Emulator(image, record())
        assert emu.run(0x3dd8c, suffix) == 0xffffffff
        assert not emu.events and bytes(emu.media) == emu.before
        cases += 1

    # Document a stock limitation: this LK helper checks magic, not the CRC.
    bad_crc = bytearray(record(0x7e, 0x7f))
    bad_crc[28] ^= 1
    emu = Emulator(image, bad_crc)
    assert emu.text(emu.run(0x3dce4)) == '_b'
    assert bytes(emu.media) == emu.before
    cases += 1
    return cases


def main():
    stock = (ROOT / 'firmware/stock-v0.8.293/lk.img').read_bytes()
    assert hashlib.sha256(stock).hexdigest() == STOCK_SHA
    candidate = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else ROOT / 'dist/lk/lk.bin'
    assert candidate.is_relative_to(ROOT)
    patched = candidate.read_bytes()
    assert len(patched) == len(stock)
    results = []
    for name, image in [('stock', stock), ('patched', patched)]:
        results.append({'image': name, 'sha256': hashlib.sha256(image).hexdigest(),
                        'cases': check(image)})
    output = {'hardware_tested': False, 'results': results,
              'scope': 'LK A/B helpers; mocked partition I/O and boot-region switch'}
    (ROOT / 'out/lk-boot-control-tests.json').write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
