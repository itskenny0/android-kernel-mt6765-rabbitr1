#!/usr/bin/env python3
"""Trace the stock I2C SiP service in Unicorn; no SMC or device access."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (
    UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X2,
    UC_ARM64_REG_X19, UC_ARM64_REG_X20, UC_ARM64_REG_X21, UC_ARM64_REG_X22,
    UC_ARM64_REG_X29, UC_ARM64_REG_X30,
)

ROOT = Path('/rabbitr1')
STOCK = ROOT/'firmware/stock-v0.8.293'
TEE_SHA256 = 'e6de1331346ea1df4a0b78106de5ec5886f6eda99270de5e23ac9e7ba7101b62'
SERVICE = 0xfcc0
TABLE = 0x1cc08
BASES = (0, 0, 0x11009000, 0x1100f000, 0x11011000, 0, 0x1100d000)
DISPATCH = {0xc20002a0: 0x5d08, 0x820002a0: 0x5fc0}


def emulate(data, command, controller, offset, value, bias):
    """Enter the selected dispatch arm with the platform handler's live state.

    The earlier caller/security checks and exception entry are outside this
    test. The real dispatch, helper, return conversion and epilogue execute.
    """
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    page = bias & ~4095
    uc.mem_map(page, (bias - page + len(data) + 4095) & ~4095)
    uc.mem_write(bias, data)
    stack, context, stop = 0x20008000, 0x20001000, 0x20010000
    uc.mem_map(0x20000000, 0x20000)
    for base in BASES:
        if base:
            uc.mem_map(base, 0x1000)
    # Reconstruct only the saved frame that the selected branch unwinds.
    uc.mem_write(stack, struct.pack('<QQQQQQ', 0, stop, 0x19, 0x20, 0x21, 0x22))
    uc.reg_write(UC_ARM64_REG_SP, stack)
    uc.reg_write(UC_ARM64_REG_X29, stack)
    uc.reg_write(UC_ARM64_REG_X30, stop)
    uc.reg_write(UC_ARM64_REG_X19, command)
    uc.reg_write(UC_ARM64_REG_X20, controller)
    uc.reg_write(UC_ARM64_REG_X21, offset)
    uc.reg_write(UC_ARM64_REG_X2, value)
    uc.reg_write(UC_ARM64_REG_X22, context)
    writes, executed = [], []

    def code(machine, address, size, _):
        at = address - bias
        # Do not silently emulate unrelated firmware after a wrong branch.
        allowed = (DISPATCH[command] <= at < DISPATCH[command] + 16
                   or 0x60c8 <= at < 0x60d8 or SERVICE <= at < 0xfd1c
                   or 0x5a98 <= at < 0x5aa0 or 0x62dc <= at < 0x62f4)
        assert allowed, f'Unexpected instruction: {at:#x}'
        executed.append(at)

    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_WRITE,
                lambda machine, access, address, size, contents, _: writes.append(
                    (address, size, contents)))
    uc.emu_start(bias + DISPATCH[command], stop, count=100)
    assert uc.reg_read(UC_ARM64_REG_PC) == stop, 'Firmware did not return'
    assert SERVICE in executed, 'Dispatch did not call the I2C service'
    assert uc.reg_read(UC_ARM64_REG_X0) == context
    assert uc.reg_read(UC_ARM64_REG_SP) == stack + 0x70
    assert [uc.reg_read(reg) for reg in (UC_ARM64_REG_X19, UC_ARM64_REG_X20,
                                       UC_ARM64_REG_X21, UC_ARM64_REG_X22)] == [0x19, 0x20, 0x21, 0x22]
    result = struct.unpack('<q', uc.mem_read(context, 8))[0]
    return result, writes


def main():
    data = (STOCK/'tee.img').read_bytes()
    assert len(data) == 140944 and hashlib.sha256(data).hexdigest() == TEE_SHA256, \
        'Requires the exact RabbitOS v0.8.293 tee.img'
    assert data[:4] == bytes.fromhex('88168858') and data[8:12] == b'atf\0'
    # The first word in each table record is not used by the service. In this
    # build record 3 contains 2 there; it is the index that selects I2C3.
    records = [struct.unpack_from('<III', data, TABLE + 12 * i) for i in range(7)]
    assert tuple(record[1] for record in records) == BASES
    assert tuple(record[2] for record in records) == (0, 0, 1, 1, 1, 0, 1)

    spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
    validate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate)
    nodes = validate.fdt_nodes((STOCK/'merged.dtb').read_bytes())
    for ident in (2, 3, 4, 6):
        node = nodes[f'/i2c{ident}@{BASES[ident]:x}']
        assert struct.unpack('>I', node['id'])[0] == ident
        assert struct.unpack('>4I', node['reg'][:16])[1] == BASES[ident]
        assert struct.unpack('>I', node['ch_offset_default'])[0] == 0x100

    # These are synthetic load locations, not claims about the running ATF
    # address. Low bits 0x3c0 make the unmodified ADRP/ADD resolve the table at
    # file offset 0x1cc08; the same bias resolves the dispatcher name string.
    biases = (0x10003c0, 0x446003c0)
    runs = 0
    cases = [(ident, 0xf8c, 2) for ident in range(8)]
    cases += [(0xffffffff, 0xf8c, 2)]
    cases += [(4, offset, 2) for offset in (0xeff, 0xf00, 0xfa0, 0xfa2, 0x1000, 0x1f8c)]
    cases += [(4, 0xf8c, 0x12345678)]
    for bias in biases:
        for command in DISPATCH:
            for ident, offset, value in cases:
                accepted = ident < 7 and BASES[ident] and 0xf00 <= (offset & 0xfff) <= 0xfa0
                result, writes = emulate(data, command, ident, offset, value, bias)
                expected = []
                if accepted:
                    expected.append((BASES[ident] + (offset & 0xfff), 2, value & 0xffff))
                expected.append((0x20001000, 8, 0 if accepted else 0xffffffffffffffff))
                # Unicorn exposes a signed hook value for the 64-bit -1 store.
                writes = [(address, size, contents & ((1 << (size * 8)) - 1))
                          for address, size, contents in writes]
                assert result == (0 if accepted else -1), (ident, offset, result)
                assert writes == expected, (ident, offset, writes, expected)
                runs += 1

    report = {'tee_sha256': TEE_SHA256, 'service_file_offset': SERVICE,
              'table_file_offset': TABLE, 'controller_records': records,
              'emulated_cases': runs, 'hardware_tested': False,
              'scope': 'selected dispatch arms, I2C service and return; not EL3 entry or caller checks'}
    (ROOT/'out/i2c-firmware-audit.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS: {runs} stock I2C SiP dispatch cases, table/DT identities, MMIO writes and signed returns')
    print('No SMC executed on the host; caller checks, clocks and physical routing remain untested.')


if __name__ == '__main__':
    main()
