#!/usr/bin/env python3
"""Execute stock MediaTek boot HAL instructions with in-memory I/O fixtures.

Input: the unmodified vendor library extracted from stock v0.8.293. No vendor
binary is redistributed. This checks metadata operations and eMMC requests,
not Android IPC, SELinux, real eMMC behavior or interruption during a write.
"""
import hashlib
import json
from pathlib import Path
import resource
import struct
import sys
import zlib

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
                                UC_ARM64_REG_X2, UC_ARM64_REG_X8,
                                UC_ARM64_REG_SP, UC_ARM64_REG_LR,
                                UC_ARM64_REG_PC, UC_ARM64_REG_TPIDR_EL0)

ROOT = Path('/rabbitr1')
SHA = 'a760f28732f9bd16c734bb09ed1393c9dd9d5e6c886b8400f1dc2f0a41dd722a'
BASE, DATA, STACK, STOP = 0x100000, 0x200000, 0x308000, 0x400000
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def crc(data):
    return bytes(data[:28]) + struct.pack('<I', zlib.crc32(data[:28]))


def control(a=0xff, b=0xff, flag=1):
    data = bytearray(range(32))
    data[:4] = b'_a\0\0'
    struct.pack_into('<I', data, 4, 0x42414342)
    data[8:10] = bytes([1, 2])
    data[12], data[14], data[20] = a, b, flag
    return crc(data)


class Emulator:
    def __init__(self, image):
        self.uc = uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        for address, size in [(BASE, 0x21000), (DATA, 0x10000),
                              (STACK & ~0xffff, 0x10000), (STOP, 0x1000)]:
            uc.mem_map(address, size)
        assert image[:6] == b'\x7fELF\x02\x01'
        phoff = struct.unpack_from('<Q', image, 32)[0]
        size, count = struct.unpack_from('<HH', image, 54)
        for n in range(count):
            kind, _, offset, va, _, filesz, memsz, _ = struct.unpack_from(
                '<IIQQQQQQ', image, phoff + n * size)
            if kind == 1:
                assert va + memsz <= 0x21000
                if filesz:
                    uc.mem_write(BASE + va, image[offset:offset + filesz])
        uc.reg_write(UC_ARM64_REG_TPIDR_EL0, DATA + 0x1000)
        uc.hook_add(UC_HOOK_CODE, self.hook, begin=BASE + 0x1bd00,
                    end=BASE + 0x1c2ff)
        self.reset()

    def reset(self, data=None, current=0):
        self.media = control() if data is None else bytes(data)
        self.events = []
        self.read_error = self.write_error = self.ioctl_error = False
        self.ext_csd = bytearray(512)
        self.ext_csd[179] = 0x48
        self.uc.mem_write(DATA + 32, struct.pack('<II', 2, current))

    def text(self, address):
        return bytes(self.uc.mem_read(address, 96)).split(b'\0')[0].decode('ascii')

    def hook(self, uc, address, size, _):
        offset = address - BASE
        x0, x1, x2 = [uc.reg_read(r) for r in (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
                                             UC_ARM64_REG_X2)]
        result = 0
        if offset == 0x1bd00:  # get_bootloader_message_blk_device, short libc++ string
            uc.mem_write(uc.reg_read(UC_ARM64_REG_X8), b'\x08para\0' + bytes(18))
        elif offset == 0x1bd10:  # LoadBootloaderControl
            self.events.append('read')
            if not self.read_error:
                uc.mem_write(x1, self.media)
                result = 1
        elif offset == 0x1bd20:  # Execute the actual stock CRC implementation.
            uc.reg_write(UC_ARM64_REG_PC, BASE + 0xc2a0)
            return
        elif offset == 0x1bd40:  # UpdateAndSaveBootloaderControl: storage boundary
            self.events.append('write')
            if not self.write_error:
                self.media = crc(bytes(uc.mem_read(x1, 32)))
                result = 1
        elif offset == 0x1bfa0:  # open: eMMC fixture; UFS paths do not exist
            path = self.text(x0)
            self.events.append(('open', path, x1))
            assert path in ('/dev/block/sdc', '/dev/block/mmcblk0')
            result = 5 if path == '/dev/block/mmcblk0' else -1
        elif offset == 0x1bfb0:  # ioctl(MMC_IOC_CMD)
            assert (x0, x1) == (5, 0xc048b300)
            command = bytes(uc.mem_read(x2, 72))
            write, app, opcode, arg = struct.unpack_from('<iiII', command)
            flags, blksz, blocks = struct.unpack_from('<III', command, 32)
            pointer = struct.unpack_from('<Q', command, 64)[0]
            assert app == 0 and write == 0
            self.events.append(('mmc', opcode, arg))
            if self.ioctl_error is True or self.ioctl_error == opcode:
                result = -1
            elif opcode == 8:  # SEND_EXT_CSD
                assert (arg, flags, blksz, blocks) == (0, 0x35, 512, 1)
                uc.mem_write(pointer, bytes(self.ext_csd))
            else:
                assert opcode == 6 and flags == 0x1d  # SWITCH, R1B response
                assert arg >> 16 == 0x3b3 and arg & 255 == 1
                self.ext_csd[179] = (arg >> 8) & 255
        elif offset == 0x1bfc0:
            assert x0 == 5
            self.events.append('close')
        elif offset not in (0x1bd30, 0x1bfd0):  # ShouldLog=false, printf
            raise AssertionError(f'unexpected external call {offset:#x}')
        uc.reg_write(UC_ARM64_REG_X0, result & 0xffffffffffffffff)
        uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

    def run(self, address, slot=0):
        uc = self.uc
        uc.reg_write(UC_ARM64_REG_X0, DATA)
        uc.reg_write(UC_ARM64_REG_X1, slot)
        uc.reg_write(UC_ARM64_REG_X8, DATA + 0x2000)
        uc.reg_write(UC_ARM64_REG_SP, STACK)
        uc.reg_write(UC_ARM64_REG_LR, STOP)
        uc.emu_start(BASE + address, STOP, count=100000)
        assert uc.reg_read(UC_ARM64_REG_PC) == STOP
        assert uc.reg_read(UC_ARM64_REG_SP) == STACK
        return uc.reg_read(UC_ARM64_REG_X0) & 0xffffffff


def main():
    path = (Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else ROOT /
            'research/android/stock-vendor/lib64/hw/android.hardware.boot@1.0-impl-1.2-mtkimpl.so')
    assert path.is_relative_to(ROOT)
    image = path.read_bytes()
    assert hashlib.sha256(image).hexdigest() == SHA
    emu = Emulator(image)
    cases = 0
    for current in (0, 1):
        for slot in (0, 1):
            for value in (0, 0x0f, 0x7f, 0x80, 0xff):
                data = control(value, value)
                emu.reset(data, current)
                assert emu.run(0xd638, slot) == 1
                expected = bytearray(data)
                expected[12 + 2 * slot] = (value & 0x80) | 0x6f
                if slot != current:
                    expected[13 + 2 * slot] &= ~1
                other = 14 - 2 * slot
                if expected[other] & 15 == 15:
                    expected[other] = (expected[other] & 0xf0) | 14
                assert emu.media == crc(expected)
                assert emu.events == ['read', 'write']
                cases += 1
        for value in (0, 0x0f, 0x7f, 0x80, 0xff):
            data = control(value, value)
            emu.reset(data, current)
            assert emu.run(0xd4f8) == 1
            expected = bytearray(data)
            expected[12 + 2 * current] = (value & 15) | 0x90
            assert emu.media == crc(expected)
            cases += 1

    for flag in (0, 1, 2, 255):
        for corrupt in (False, True):
            data = bytearray(control(flag=flag))
            if corrupt:
                data[28] ^= 1
            emu.reset(data)
            emu.run(0xb098)
            expected = bytearray(data)
            if flag == 1 and not corrupt:
                expected[20] = 0
                expected = crc(expected)
            assert emu.media == bytes(expected)
            assert emu.events == (['read', 'write'] if flag == 1 and not corrupt else ['read'])
            cases += 1

    for operation in (0xd4f8, 0xd638, 0xd800):
        for fault in ('read_error', 'write_error'):
            emu.reset()
            before = emu.media
            setattr(emu, fault, True)
            assert emu.run(operation, 1) == 0
            assert emu.media == before
            assert emu.events == (['read'] if fault == 'read_error' else ['read', 'write'])
            cases += 1

    for slot in (0, 1):
        for old in range(256):
            emu.reset()
            emu.ext_csd[179] = old
            assert emu.run(0xbd20, slot) == 1
            expected = (old & ~0x38) | ((slot + 1) << 3)
            assert emu.ext_csd[179] == expected
            commands = [event for event in emu.events if isinstance(event, tuple) and event[0] == 'mmc']
            assert len(commands) == (1 if old == expected else 2)
            assert emu.events[-1] == 'close'
            cases += 1
        for failed_command in (8, 6):
            emu.reset()
            # Force a switch request for both target slots.
            before = 0x50 if slot == 0 else 0x48
            emu.ext_csd[179] = before
            emu.ioctl_error = failed_command
            assert emu.run(0xbd20, slot) == 0
            assert emu.ext_csd[179] == before and emu.events[-1] == 'close'
            cases += 1
    for invalid in (2, 3, 0xffffffff):
        emu.reset()
        assert emu.run(0xbd20, invalid) == 0 and not emu.events
        cases += 1
    result = {'sha256': SHA, 'cases': cases, 'hardware_tested': False}
    (ROOT / 'out/stock-boot-hal-tests.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
