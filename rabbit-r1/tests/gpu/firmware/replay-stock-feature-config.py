#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Replay bounded stock feature configuration; emit anonymous metadata as JSON.

Requires the separately supplied, hash-pinned ARM64 stock library and Unicorn.
No native vendor loading, imported function calls, device I/O or file writes.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct
import sys

LIBRARY_SHA256 = '5b3048e576df364bf82e9d85e2a61e05726fae03778b2c7f6f4c4df036658641'
BVNC = (22, 87, 104, 18)
PACKED_BVNC = 0x0016005700680012
PRODUCER = (0x36f2c, 0x37320)
GETTERS = {'value': (0x3bc50, 24), 'string': (0x3bc68, 24), 'bvnc': (0x3bca4, 8)}
HEAP, STACK, STOP = 0x30000000, 0x40000000, 0x50000000
SLOTS, CONTEXT_SIZE = 70, 0x280


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Elf:
    def __init__(self, path):
        self.raw = path.read_bytes()
        require(hashlib.sha256(self.raw).hexdigest() == LIBRARY_SHA256,
                'stock library SHA-256 mismatch')
        require(self.raw[:6] == b'\x7fELF\x02\x01' and
                struct.unpack_from('<H', self.raw, 18)[0] == 183,
                'expected little-endian ELF64 AArch64')
        phoff = struct.unpack_from('<Q', self.raw, 32)[0]
        stride, count = struct.unpack_from('<HH', self.raw, 54)
        require(stride == 56, 'unexpected program-header stride')
        self.loads = [s for i in range(count) if
                      (s := struct.unpack_from('<IIQQQQQQ', self.raw,
                                               phoff + i * stride))[0] == 1]
        self.end = max(s[3] + s[6] for s in self.loads)
        shoff = struct.unpack_from('<Q', self.raw, 40)[0]
        stride, count = struct.unpack_from('<HH', self.raw, 58)
        require(stride == 64, 'unexpected section-header stride')
        self.relocations = []
        for i in range(count):
            section = struct.unpack_from('<IIQQQQIIQQ', self.raw, shoff + i * stride)
            if section[1] != 4:  # SHT_RELA
                continue
            require(section[9] == 24, 'unexpected RELA stride')
            for offset in range(section[4], section[4] + section[5], section[9]):
                location, info, addend = struct.unpack_from('<QQq', self.raw, offset)
                self.relocations.append((location, info & 0xffffffff, info >> 32, addend))
        self.relative = {location: addend for location, kind, symbol, addend
                         in self.relocations if kind == 1027}
        require(len(self.relative) == 828, 'unexpected relative relocation count')

    def at(self, address, size):
        matches = [s for s in self.loads if
                   s[3] <= address and address + size <= s[3] + s[5]]
        require(len(matches) == 1, 'address is not in one file-backed PT_LOAD')
        segment = matches[0]
        offset = segment[2] + address - segment[3]
        return self.raw[offset:offset + size]

    def integer(self, address, size):
        return int.from_bytes(self.at(address, size), 'little')

    def pointer(self, address):
        return self.relative.get(address, self.integer(address, 8))

    def string(self, address):
        return self.at(address, 64).split(b'\0', 1)[0].decode('ascii')

    def decode(self):
        # This exact BNC row is selected by the real producer after its caller
        # validates BNC/revision. The values are NOT assigned feature names.
        row = 0x19fb0 + 16 * 0x38
        require(self.integer(row, 8) == 0x0016000000680012, 'BNC row mismatch')
        result = []
        for selector in range(SLOTS):
            encoding = self.integer(0x1a560 + selector * 2, 2)
            kind, shift = encoding >> 14, encoding & 63
            require(kind in (0, 1, 2), 'unknown slot encoding')
            mask = self.integer(0x1a5f0 + selector * 8, 8)
            word = self.integer(0x1a820 + selector * 2, 2)
            index = (self.integer(row + 0x20 + word * 8, 8) & mask) >> shift
            require(index < 0x80000000, 'unsupported signed table index')
            table = self.pointer(0x652c8 + selector * 8)
            width = (2, 8, 4)[kind]
            location = table + index * width
            value = self.pointer(location) if kind == 1 else self.integer(location, width)
            if kind == 0 and value == 0xffff:
                value = 0xffffffff
            result.append((kind, value))
        revisions = [0x1a8b0 + i * 0x38 for i in range(32)
                     if self.integer(0x1a8b0 + i * 0x38, 8) == PACKED_BVNC]
        require(len(revisions) == 1, 'exact revision row mismatch')
        return (result, struct.unpack('<3Q', self.at(row + 8, 24)),
                struct.unpack('<6Q', self.at(revisions[0] + 8, 48)))


def replay(elf, base, poison):
    # Import after mandatory hash verification. No Capstone or other scripts.
    import unicorn
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
    from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
    import unicorn.arm64_const as reg

    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(base, (elf.end + 4095) & ~4095)
    for segment in elf.loads:
        uc.mem_write(base + segment[3],
                     elf.raw[segment[2]:segment[2] + segment[5]])
    for location, kind, symbol, addend in elf.relocations:
        if kind == 1027:  # R_AARCH64_RELATIVE; other imports are never needed.
            require(not symbol, 'RELATIVE relocation unexpectedly has a symbol')
            uc.mem_write(base + location, struct.pack('<Q', base + addend))
    for address in (HEAP, STACK):
        uc.mem_map(address, 0x10000)
        uc.mem_write(address, bytes([poison & 255]) * 0x10000)
    uc.mem_map(STOP, 0x1000)
    uc.mem_write(HEAP, bytes(CONTEXT_SIZE))  # successful calloc(1, 0x280)
    for i in range(31):
        uc.reg_write(getattr(reg, f'UC_ARM64_REG_X{i}'), poison)
    for i in range(32):
        uc.reg_write(getattr(reg, f'UC_ARM64_REG_Q{i}'), poison | (poison << 64))
    uc.reg_write(reg.UC_ARM64_REG_NZCV, 0xf0000000 if poison & 1 else 0)
    for register, value in ((reg.UC_ARM64_REG_X0, HEAP),
                            (reg.UC_ARM64_REG_X26, PACKED_BVNC),
                            (reg.UC_ARM64_REG_X28, HEAP + 0x1000),
                            (reg.UC_ARM64_REG_SP, STACK + 0x8000),
                            (reg.UC_ARM64_REG_LR, STOP)):
        uc.reg_write(register, value)
    steps, reads, writes = [], [], []

    def code(_uc, pc, _size, _data):
        address = pc - base
        require(PRODUCER[0] <= address < PRODUCER[1] and
                address not in (0x36fb0, 0x370d8), 'producer left successful slice')
        steps.append(address)

    def read(_uc, _kind, address, size, _value, _data):
        if base <= address and address + size <= base + elf.end:
            elf.at(address - base, size)  # Reject uninitialized ELF/BSS reads.
            require(not any(kind != 1027 and address < base + location + 8 and
                            base + location < address + size
                            for location, kind, _, _ in elf.relocations),
                    'producer reads unresolved imported relocation')
            reads.append(('elf', address - base, size))
        else:
            # x8 receives this value for the omitted code after PRODUCER[1].
            require(address == STACK + 0x8088 and size == 8, 'unexpected producer read')
            reads.append(('unused_post_boundary_stack', 0x88, size))

    def write(_uc, _kind, address, size, _value, _data):
        require((HEAP <= address and address + size <= HEAP + CONTEXT_SIZE) or
                (address == HEAP + 0x1098 and size == 8), 'unexpected producer write')
        writes.append((address - HEAP, size))

    hooks = [uc.hook_add(UC_HOOK_CODE, code), uc.hook_add(UC_HOOK_MEM_READ, read),
             uc.hook_add(UC_HOOK_MEM_WRITE, write)]
    uc.emu_start(base + PRODUCER[0], base + PRODUCER[1], count=20000)
    require(uc.reg_read(reg.UC_ARM64_REG_PC) == base + PRODUCER[1], 'producer did not finish')
    for hook in hooks:
        uc.hook_del(hook)
    context = bytes(uc.mem_read(HEAP, CONTEXT_SIZE))
    require(struct.unpack('<Q', uc.mem_read(HEAP + 0x1098, 8))[0] == HEAP,
            'device feature pointer mismatch')

    def getter(name, selector=0):
        start, length = GETTERS[name]
        width = 4 if name == 'value' else 8
        slot = HEAP + (0x278 if name == 'bvnc' else 0x48 + 8 * selector)
        uc.mem_write(HEAP + 0x2000, b'\x5a' * 8)
        for register, value in ((reg.UC_ARM64_REG_X0, HEAP),
                                (reg.UC_ARM64_REG_X1, selector),
                                (reg.UC_ARM64_REG_X2, HEAP + 0x2000),
                                (reg.UC_ARM64_REG_LR, STOP)):
            uc.reg_write(register, value)

        def guard_code(_uc, pc, _size, _data):
            require(base + start <= pc < base + start + length, 'getter left its body')

        def guard_read(_uc, _kind, address, size, _value, _data):
            require(address == slot and size == width, 'unexpected getter read')

        def guard_write(_uc, _kind, address, size, _value, _data):
            require(name != 'bvnc' and address == HEAP + 0x2000 and size == width,
                    'unexpected getter write')

        hooks = [uc.hook_add(UC_HOOK_CODE, guard_code),
                 uc.hook_add(UC_HOOK_MEM_READ, guard_read),
                 uc.hook_add(UC_HOOK_MEM_WRITE, guard_write)]
        uc.emu_start(base + start, STOP, count=16)
        require(uc.reg_read(reg.UC_ARM64_REG_PC) == STOP, 'getter did not return')
        for hook in hooks:
            uc.hook_del(hook)
        return uc.reg_read(reg.UC_ARM64_REG_X0), int.from_bytes(
            uc.mem_read(HEAP + 0x2000, width), 'little')

    decoded, feature_words, quirk_words = elf.decode()
    slots, normalized = [], bytearray(context)
    for selector, (kind, expected) in enumerate(decoded):
        word = struct.unpack_from('<Q', context, 0x48 + selector * 8)[0]
        available, numeric = getter('value', selector)
        require(numeric == word & 0xffffffff and available == (numeric != 0xffffffff),
                'numeric getter mismatch')
        if kind == 1:
            present, pointer = getter('string', selector)
            require(pointer == word and present == bool(word), 'string getter mismatch')
            require(word == base + expected, 'string relocation mismatch')
            value = elf.string(expected)
            require(bytes(uc.mem_read(word, 64)).split(b'\0', 1)[0].decode('ascii') == value,
                    'string contents mismatch')
            struct.pack_into('<Q', normalized, 0x48 + selector * 8, expected)
            slots.append({'selector': selector, 'encoding': 'pointer',
                          'string': value, 'pointer_elf_vaddr': hex(expected)})
        else:
            require(word == expected, 'packed table and producer value differ')
            slots.append({'selector': selector, 'encoding': ('u16', 'pointer', 'u32')[kind],
                          'available': bool(available), 'raw_u32': numeric})
    require(getter('bvnc')[0] == PACKED_BVNC, 'BVNC getter mismatch')
    require(struct.unpack_from('<3Q', context, 0x30) == feature_words, 'feature words differ')
    require(struct.unpack_from('<6Q', context) == quirk_words, 'quirk words differ')
    require(bytes(uc.mem_read(HEAP, CONTEXT_SIZE)) == context, 'getters mutated context')

    revision_cases = []
    parts_cases = [BVNC, *(tuple(v if j != i else v + delta for j, v in enumerate(BVNC))
                           for i in range(4) for delta in (-1, 1)),
                   (0, 0, 0, 0), (65535, 65535, 65535, 65535)]
    for parts in parts_cases:
        uc.reg_write(reg.UC_ARM64_REG_X26, sum(v << (48 - i * 16)
                                             for i, v in enumerate(parts)))

        def check_code(machine, pc, _size, _data):
            if pc - base in (0x369b4, 0x36e3c, 0x36e44):
                machine.emu_stop()
            else:
                require(0x36990 <= pc - base < 0x369b4, 'revision check left its slice')

        def no_memory(*_args):
            raise ValueError('unexpected revision-check memory access')

        hooks = [uc.hook_add(UC_HOOK_CODE, check_code),
                 uc.hook_add(UC_HOOK_MEM_READ, no_memory),
                 uc.hook_add(UC_HOOK_MEM_WRITE, no_memory)]
        uc.emu_start(base + 0x36990, STOP, count=20)
        for hook in hooks:
            uc.hook_del(hook)
        address = uc.reg_read(reg.UC_ARM64_REG_PC) - base
        expected = (0x369b4 if parts == BVNC else
                    0x36e44 if parts[0] == 22 and parts[2:] == (104, 18) else 0x36e3c)
        require(address == expected, 'revision check differs')
        revision_cases.append({'bvnc': '.'.join(map(str, parts)),
                               'accepted': address == 0x369b4, 'exit_vaddr': hex(address)})
    return {'load_base': hex(base), 'unused_state_poison': hex(poison),
            'unicorn_version': unicorn.__version__, 'executed_instruction_count': len(steps),
            'producer_reads': len(reads), 'producer_writes': len(writes),
            'unresolved_relocation_reads': 0,
            'normalized_context_sha256': hashlib.sha256(normalized).hexdigest(),
            'slots': slots, 'feature_words': [hex(v) for v in feature_words],
            'quirk_words': [hex(v) for v in quirk_words], 'revision_cases': revision_cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True,
                        help='separately obtained stock ARM64 libsrv_um.so')
    args = parser.parse_args()
    elf = Elf(args.library)
    runs = [replay(elf, base, poison) for base, poison in
            ((0x10000000, 0x1111111111111111), (0x60000000, 0xeeeeeeeeeeeeeeee))]
    for field in ('normalized_context_sha256', 'slots', 'feature_words', 'quirk_words',
                  'revision_cases'):
        require(runs[0][field] == runs[1][field], 'load/unused-state dependence: ' + field)
    slots = runs[0]['slots']
    result = {'result': 'PASS', 'library_sha256': LIBRARY_SHA256,
              'bvnc': '.'.join(map(str, BVNC)), 'hardware_tested': False,
              'native_vendor_execution': False, 'device_io': False,
              'producer_vaddr_interval': [hex(v) for v in PRODUCER],
              'producer_bytes_sha256': hashlib.sha256(elf.at(PRODUCER[0],
                                             PRODUCER[1] - PRODUCER[0])).hexdigest(),
              'relative_relocations_applied': len(elf.relative),
              'elf_relocation_type_counts': dict(Counter(r[1] for r in elf.relocations)),
              'slot_counts': {'total': SLOTS,
                              'strings': sum('string' in x for x in slots),
                              'available_numeric': sum(x.get('available', False) for x in slots),
                              'unavailable_numeric': sum(x.get('available') is False for x in slots)},
              'slots': slots, 'feature_words': runs[0]['feature_words'],
              'quirk_words': runs[0]['quirk_words'], 'revision_cases': runs[0]['revision_cases'],
              'runs': [{k: v for k, v in run.items() if k not in
                        ('slots', 'feature_words', 'quirk_words', 'revision_cases')} for run in runs],
              'scope': ['Success calloc boundary supplied; allocator and connection/ioctls omitted.',
                        'Exact packed BVNC supplied; preceding BNC/revision instructions tested separately.',
                        'Only bounded producer/getter/check instructions emulated; no imported functions called.',
                        'String pointers normalized to ELF virtual addresses; their numeric getter bits are not limits.',
                        'Slot and mask semantics remain anonymous; no Mesa table or GPU enablement follows.']}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, ImportError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
