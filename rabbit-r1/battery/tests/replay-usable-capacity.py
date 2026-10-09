#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Capture stock instructions, never portable-C or Python expected-value math."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
    UC_ARM64_REG_X2, UC_ARM64_REG_X10, UC_ARM64_REG_X11, UC_ARM64_REG_X23, UC_ARM64_REG_X25,
    UC_ARM64_REG_X26, UC_ARM64_REG_X30, UC_ARM64_REG_SP, UC_ARM64_REG_PC)

ROOT = Path('/rabbitr1')
SOURCE = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path,
                    default=ROOT/'out/battery-review/usable-capacity/stock-replay.json')
args = parser.parse_args()
provenance = json.loads((SOURCE/'provenance.json').read_text())
fixture_path = SOURCE/'tests/stock-oracles.json'
assert hashlib.sha256(fixture_path.read_bytes()).hexdigest() == provenance['generated_sha256']['tests/stock-oracles.json']
profiles = {case['temp_c']: case['profile'] for case in
            json.loads(fixture_path.read_text())['temperature_profiles']}
elf = (ROOT/'out/stock-kernel.elf').read_bytes()
assert hashlib.sha256(elf).hexdigest() == provenance['source_files_sha256']['out/stock-kernel.elf']
ORIG = 0xffffff8008ad6000
SIZE = 0x9000
BASE, GM, STACK, STOP = 0x100000, 0x200000, 0x308000, 0x400000
shoff = struct.unpack_from('<Q', elf, 40)[0]
shsize, shnum = struct.unpack_from('<HH', elf, 58)
for i in range(shnum):
    section = struct.unpack_from('<IIQQQQIIQQ', elf, shoff + i * shsize)
    va, offset, size = section[3:6]
    if va <= ORIG and ORIG + SIZE <= va + size:
        code = elf[offset + ORIG - va:offset + ORIG - va + SIZE]
        break
else:
    raise AssertionError('Missing stock text')
# Pin every executed algorithm entry and the exact full vboot orchestration.
functions = {key: provenance['stock_function_hashes'][key] for key in (
    'fg_construct_battery_profile_by_vboot', 'fg_compensate_battery_voltage_from_low')}
functions['fgr_construct_vboot'] = {'address': '0xffffff8008add0ec', 'size': 880,
    'sha256': '0039376f7fb6a2ec3c6ba3dbe927d363a01ca9b13d08a5e784d34707e95089ff'}
for name, entry in functions.items():
    start = int(entry['address'], 16) - ORIG
    digest = hashlib.sha256(code[start:start + entry['size']]).hexdigest()
    assert entry['sha256'] == digest, (name, digest)
# Addresses come from the verified ELF symbol list, not an arbitrary host binary.
symbols = {'gauge_get_int_property': 0xffffff8008ad74e4,
    'force_get_tbat': 0xffffff8008ad7478, 'power_supply_get_property': 0xffffff8008af7c04,
    'bat_get_debug_level': 0xffffff8008ad6288, '_mcount': 0xffffff800809f01c}
uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
for address, size in [(BASE, SIZE), (GM, 0x10000), (0x300000, 0x10000), (STOP, 0x1000)]:
    uc.mem_map(address, size)
uc.mem_write(BASE, code)
canary = 0xffffff800973db08 - ORIG + BASE
uc.mem_map(canary & ~0xfff, 0x1000)
uc.mem_write(canary, bytes(8))
active = {}

def put(offset, value):
    uc.mem_write(GM + offset, struct.pack('<I', value & 0xffffffff))

def get(offset):
    return struct.unpack('<i', uc.mem_read(GM + offset, 4))[0]

def rows():
    return [list(struct.unpack('<IHHHH', uc.mem_read(GM + 16784 + i * 12, 12)))
            for i in range(53)]

def hook(cpu, pc, size, ignored):
    original_pc = pc - BASE + ORIG
    if original_pc == 0xffffff8008adcd88:
        active['dod_before_u16_store'] = []
    elif original_pc == 0xffffff8008adcf8c:
        row_address = cpu.reg_read(UC_ARM64_REG_X10)
        if row_address < GM + 16784 + 53 * 12:
            assert row_address == GM + 16784 + len(active['dod_before_u16_store']) * 12
            active['dod_before_u16_store'].append(cpu.reg_read(UC_ARM64_REG_X11) & 0xffffffff)
    elif original_pc == 0xffffff8008add234:
        active['initial_cutoff_01mv'] = cpu.reg_read(UC_ARM64_REG_X1)
    elif original_pc == 0xffffff8008add238:
        active['initial_usable_01mah'] = get(596)
        active['initial_dod'] = [row[4] for row in rows()]
        # Stop zero-capacity or ushort-wrapped inputs before the search.
        if get(596) <= 0 or any(a > b for a, b in zip(active['initial_dod'], active['initial_dod'][1:])):
            active['rejected'] = 'stock_zero_capacity_or_wrapped_dod'
            cpu.emu_stop()
            return
    elif original_pc == 0xffffff8008add618:
        if cpu.reg_read(UC_ARM64_REG_X23) & 0xffffffff == 0:
            active['rejected'] = 'stock_negative_row_index'
            cpu.emu_stop()
            return
        high = cpu.reg_read(UC_ARM64_REG_X25) & 0xffffffff
        low = cpu.reg_read(UC_ARM64_REG_X26) & 0xffffffff
        if high <= low or high - low > 10000:
            active['rejected'] = 'duplicate_or_unbounded_dod_bracket'
            cpu.emu_stop()
            return
    elif original_pc == 0xffffff8008add704:
        active['rejected'] = 'stock_search_fell_through_without_crossing'
        cpu.emu_stop()
        return
    elif original_pc == 0xffffff8008add728:
        active['strict_loaded_crossing'] = True
    instruction = struct.unpack_from('<I', code, pc - BASE)[0]
    if instruction >> 26 != 0x25:
        return
    displacement = instruction & 0x3ffffff
    if displacement & 0x2000000:
        displacement -= 0x4000000
    target = original_pc + displacement * 4
    if target == symbols['gauge_get_int_property']:
        prop = cpu.reg_read(UC_ARM64_REG_X0)
        assert prop in (15, 16)
        # Explicit 0.1 mOhm arithmetic operand; not a claim that stock's raw
        # PTIM supplier (mOhm) already has this unit or is hardware validated.
        cpu.reg_write(UC_ARM64_REG_X0, active['rac_01mohm'] if prop == 16 else 0)
    elif target == symbols['force_get_tbat']:
        cpu.reg_write(UC_ARM64_REG_X0, active['temp_c'] & 0xffffffff)
    elif target == symbols['power_supply_get_property']:
        cpu.mem_write(cpu.reg_read(UC_ARM64_REG_X2), bytes(4))
        cpu.reg_write(UC_ARM64_REG_X0, 0)
    elif target == symbols['bat_get_debug_level']:
        cpu.reg_write(UC_ARM64_REG_X0, 0)
    elif target == symbols['_mcount']:
        pass
    elif ORIG <= target < ORIG + SIZE:
        return
    else:
        raise AssertionError(('Unexpected external call', hex(target)))
    cpu.reg_write(UC_ARM64_REG_PC, pc + 4)

uc.hook_add(UC_HOOK_CODE, hook, begin=BASE, end=BASE + SIZE - 1)

def capture(temp, minimum, discharge, rac, shunt, meter, dc, padding):
    global active
    active = dict(temp_c=temp, minimum_01mv=minimum, discharge_01ma=discharge,
                  rac_01mohm=rac, shunt_01mohm=shunt, meter_01mohm=meter,
                  dc_ratio_percent=dc, padding=padding)
    uc.mem_write(GM, bytes(0x10000))
    for offset, value in [(3164, 4), (15568, 255), (16776, 254), (3204, 53),
        (652, dc), (2584, shunt), (2576, meter), (2556, 1), (2560, 0),
        (3188, minimum), (3192, discharge), (644, 10000)]:
        put(offset, value)
    # Only the logging-only CURRENT_NOW access needs a dummy object pointer.
    uc.mem_write(GM + 504, struct.pack('<Q', GM + 0xf000))
    uc.mem_write(GM + 0xf0c0, struct.pack('<Q', GM + 0xf100))
    points = profiles[temp]
    for i, (charge, voltage, resistance) in enumerate(points):
        uc.mem_write(GM + 16784 + i * 12,
                     struct.pack('<IHHHH', charge, voltage, resistance, resistance, 0))
    if padding == 'repeat_last':
        last = bytes(uc.mem_read(GM + 16784 + 52 * 12, 12))
        for i in range(53, 100):
            uc.mem_write(GM + 16784 + i * 12, last)
    else:
        assert padding == 'stock_zero'
    uc.reg_write(UC_ARM64_REG_X0, GM)
    uc.reg_write(UC_ARM64_REG_X1, 254)
    uc.reg_write(UC_ARM64_REG_X30, STOP)
    uc.reg_write(UC_ARM64_REG_SP, STACK)
    uc.emu_start(BASE + 0x70ec, STOP, count=100000)
    if 'rejected' not in active:
        assert uc.reg_read(UC_ARM64_REG_PC) == STOP
        assert active['strict_loaded_crossing']
        active.update(cutoff_01mv=get(588), usable_01mah=get(596),
                      dod=[row[4] for row in rows()])
        assert active['usable_01mah'] > 0
        assert len(active['dod_before_u16_store']) == 53
    return dict(active)

cases = []
# All existing temperature profiles plus an orthogonal input sweep. Values are
# mathematical fixtures, not calibrated device measurements or defaults.
inputs = {(temp, 33500, 5000, 1500, 100, 75, 100) for temp in profiles}
for temp in [-20, -10, -5, 0, 12, 25, 37, 50, 60]:
    for minimum in [33000, 35000, 37000]:
        for discharge in [0, 1000, 5000, 7000]:
            for rac in [0, 1000, 4000]:
                for dc in [80, 100, 125]:
                    inputs.add((temp, minimum, discharge, rac,
                                0 if dc == 80 else 100, 130 if dc == 125 else 75, dc))
for values in sorted(inputs):
    cases.append(capture(*values, 'repeat_last'))
# The exact historical zero-tail comparison remains durable evidence. These
# cases deliberately do NOT supply a measured RAC or silently pad production C.
boundaries = [capture(temp, 33500, 5000, 0, 100, 75, 100, padding)
              for temp in [-10, 0, 25, 50] for padding in ['stock_zero', 'repeat_last']]
result = {'hardware_tested': False,
    'stock_kernel_elf_sha256': hashlib.sha256(elf).hexdigest(),
    'input_profiles_sha256': hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    'function_hashes': functions,
    'scope': 'Actual stock fgr_construct_vboot QMAX_SEL=1 and nested instructions. '
        'Logging/instrumentation, unused voltage/current/temperature and explicit RAC input are mocked. '
        'Valid 53-row profiles are from existing pinned instruction captures. Repeat-last padding '
        'is a deliberate alternate stock-memory fixture for valid-row endpoint comparison, not actual '
        'stock constructor behavior. RAC operand is explicitly 0.1mOhm, correcting the source supplier '
        'unit mismatch at the future caller boundary. No expected cutoff/capacity math is implemented in Python.',
    'cases': cases, 'zero_tail_boundaries': boundaries}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({'captured': len(cases), 'valid': sum('rejected' not in c for c in cases),
                  'rejected': sum('rejected' in c for c in cases), 'boundary_cases': len(boundaries)}))
