#!/usr/bin/env python3
"""Test production boot snapshots, lifetime, stock decoding and bounded LK parsing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/battery-review/boot-ocv/implementation'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/mfd/mt6397-core.c')
parser.add_argument('--replay-stock', action='store_true',
                    help='also execute pinned stock ELF conversion using Unicorn (requires boot-tools Python)')
args = parser.parse_args()
if not args.source.resolve().is_relative_to(ROOT):
    raise SystemExit('All inputs must stay under /rabbitr1')
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(OUT)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
project = Path(__file__).resolve().parent.parent
fixture_path = project/'tests/battery/mt6357-boot-stock.json'
fixture = json.loads(fixture_path.read_text())
lock = json.loads((project/'sources.lock.json').read_text())
stock_path = ROOT/'src/kernel/drivers/power/supply/mt6357-gauge.c'
pin = lock['ci_files'][str(stock_path.relative_to(ROOT))]
stock = stock_path.read_bytes()
assert pin['url'].startswith('https://raw.githubusercontent.com/')
assert len(stock) == pin['bytes'] and hashlib.sha256(stock).hexdigest() == pin['sha256']
assert pin['sha256'] == fixture['stock_gauge_source_sha256']
# Independently pinned stock address mapping, not production register definitions.
stock = stock.decode()
names = ('PMIC_AUXADC_ADC_RDY_PWRON_PCHR_ADDR',
         'PMIC_AUXADC_ADC_RDY_BAT_PLUGIN_PCHR_ADDR',
         'PMIC_RG_STRUP_AUXADC_START_SEL_ADDR',
         'PMIC_RG_SYSTEM_INFO_CON0_ADDR', 'PMIC_RGS_BATON_UNDET_ADDR')
registers = [int(re.search(r'^#define\s+'+name+r'\s+(0x[0-9a-f]+)', stock, re.M)[1], 16)
             for name in names]
assert registers == fixture['physical_registers']
blob = (fixture_path.parent/fixture['conversion']['file']).read_bytes()
assert len(blob) == 32768*4 and hashlib.sha256(blob).hexdigest() == fixture['conversion']['sha256']
conversion = struct.unpack('<32768I', blob)
stock_replay = None
if args.replay_stock:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X30,
                                     UC_ARM64_REG_SP, UC_ARM64_REG_PC)
    elf = (ROOT/'out/stock-kernel.elf').read_bytes()
    assert hashlib.sha256(elf).hexdigest() == fixture['stock_kernel_elf_sha256']
    oracle = fixture['conversion']['instruction']
    first, last = int(oracle['address'], 16), int(oracle['end'], 16)
    shoff = struct.unpack_from('<Q', elf, 40)[0]
    shsize, shnum = struct.unpack_from('<HH', elf, 58)
    for index in range(shnum):
        section = struct.unpack_from('<IIQQQQIIQQ', elf, shoff+index*shsize)
        address, offset, size = section[3:6]
        if address <= first and last <= address+size:
            code = elf[offset+first-address:offset+last-address]
            break
    else:
        raise AssertionError('Missing stock instruction range')
    assert hashlib.sha256(code).hexdigest() == oracle['sha256']
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    base, stack, stop = 0x100000, 0x208000, 0x300000
    for start, size in [(base, 0x1000), (0x200000, 0x10000), (stop, 0x1000)]:
        uc.mem_map(start, size)
    uc.mem_write(base, code)

    def trace(machine, pc, size, opaque):
        instruction = struct.unpack_from('<I', code, pc-base)[0]
        if instruction >> 26 != 0x25:
            return
        immediate = instruction & 0x3ffffff
        if immediate & 0x2000000:
            immediate -= 0x4000000
        target = first+pc-base+immediate*4
        assert target in (0xffffff800809f01c, 0xffffff8008ad6288)
        if target == 0xffffff8008ad6288:
            machine.reg_write(UC_ARM64_REG_X0, 0)
        machine.reg_write(UC_ARM64_REG_PC, pc+4)

    uc.hook_add(UC_HOOK_CODE, trace, begin=base, end=base+len(code)-1)
    for raw, expected in enumerate(conversion):
        uc.reg_write(UC_ARM64_REG_X0, raw)
        uc.reg_write(UC_ARM64_REG_X30, stop)
        uc.reg_write(UC_ARM64_REG_SP, stack)
        uc.emu_start(base, stop, count=100)
        assert uc.reg_read(UC_ARM64_REG_PC) == stop
        assert uc.reg_read(UC_ARM64_REG_X0) == expected
    stock_replay = dict(elf_sha256=fixture['stock_kernel_elf_sha256'],
                        function=oracle, cases=len(conversion),
                        mocked='Tracing and debug verbosity only; all conversion instructions execute')
    print('PASS: 32768 actual pinned stock ARM64 conversions match captured vectors', flush=True)
header_path = SRC/'include/linux/mfd/mt6357/boot.h'
core_header_path = SRC/'include/linux/mfd/mt6397/core.h'
adc_path = SRC/'drivers/iio/adc/mt6359-auxadc.c'
s = args.source.read_text()
header = re.sub(r'^#include .*\n', '', header_path.read_text(), flags=re.M)
core_header = core_header_path.read_text()
adc = adc_path.read_text()


def block(text, start, closing='\n};'):
    at = text.index(start)
    return text[at:text.index(closing, at)+len(closing)]+'\n'


def function(text, name):
    match = re.search(r'^(?:static )?(?:const )?[\w *]+\b'+name+r'\([^;]*?\)\n\{', text, re.M)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+3]+'\n'


def macro(text, name):
    match = re.search(r'^#define\s+'+name+r'\s+[^\n]+', text, re.M)
    assert match, name
    return match[0]+'\n'


# Only the kernel/framework boundary is modeled. Capture/getter/complete MFD
# probe, all pure helpers and the ADC reset invoked by child registration are
# copied from the production sources, never reimplemented in the harness.
types = header
for token in ('enum chip_id {', 'struct mt6397_chip {'):
    types += block(core_header, token)
types += block(s, 'struct chip_data {')
for token in ('enum mtk_pmic_auxadc_regs {', 'struct mt6359_auxadc {',
              'struct mtk_pmic_auxadc_info {'):
    types += block(adc, token)
constants = ''
for model, name in [('mt6323', 'MT6323_CID'), ('mt6328', 'MT6328_HWCID'),
                    ('mt6331', 'MT6331_HWCID'), ('mt6357', 'MT6357_SWCID'),
                    ('mt6358', 'MT6358_SWCID'), ('mt6359', 'MT6359_SWCID'),
                    ('mt6397', 'MT6397_CID')]:
    constants += macro((SRC/f'include/linux/mfd/{model}/registers.h').read_text(), name)
reg_header = (SRC/'include/linux/mfd/mt6357/registers.h').read_text()
for name in ('MT6357_AUXADC_ADC20', 'MT6357_AUXADC_ADC31', 'MT6357_STRUP_CON6',
             'MT6357_SYSTEM_INFO_CON0', 'MT6357_BATON_ANA_CON0'):
    constants += macro(reg_header, name)
for name in ('PMIC_RG_RESET_VAL', 'MT6357_AUXADC_RQST1'):
    constants += macro(adc, name)
constants += 'static const unsigned int stock_registers[] = {'+','.join(map(str, registers))+'};\n'
constants += 'static const u32 stock_conversion[] = {'+','.join(map(str, conversion))+'};\n'
vectors = []
for case in fixture['lk_cases']:
    for captured in case['fdt_setprop_calls']:
        data = bytes.fromhex(captured['value_hex'])
        assert captured['length'] == len(data) and b'\0' not in data
        assert case['output'][captured['property']].encode() == data
        vectors.append((data, int(data), 0))
assert len(vectors) == 24
for data, result, error in [(b'0\0', 0, 0), (b'-2147483648\0', -2147483648, 0),
                           (b'2147483647\0', 2147483647, 0), (b'-0', 0, 0),
                           (b'00000000000', 0, 0), (b'', 0, -22), (b'\0', 0, -22),
                           (b'-', 0, -22), (b'-\0', 0, -22), (b'+1', 0, -22),
                           (b' 1', 0, -22), (b'1 ', 0, -22), (b'1\n', 0, -22),
                           (b'1\0x', 0, -22), (b'1\0\0', 0, -22), (b'0xff', 0, -22),
                           (b'\xff', 0, -22), (b'2147483648', 0, -34),
                           (b'-2147483649', 0, -34), (b'99999999999', 0, -34),
                           (b'000000000000', 0, -22), (b'1'*13, 0, -22)]:
    vectors.append((data, result, error))
constants += 'struct lk_vector { const u8 *data; size_t len; s32 expected; int error; };\n'
constants += 'static const struct lk_vector lk_vectors[] = {\n'
for data, result, error in vectors:
    escaped = ''.join(f'\\x{x:02x}' for x in data)
    constants += f'{{(const u8 *)"{escaped}",{len(data)},{result},{error}}},\n'
constants += '};\n'
body = s[s.index('static const struct chip_data mt6323_core'):s.index('static const struct of_device_id mt6397_of_match')]
body += block(adc, 'static const u16 mt6357_auxadc_regs[]')
body += function(adc, 'mt6357_auxadc_restore_requests')
body += function(adc, 'mt6359_auxadc_reset')
fixture_c = project/'tests/battery/mt6357-boot-snapshot.c'
prelude, rest = fixture_c.read_text().split('/* INSERT TYPES HERE */')
model, checks = rest.split('/* INSERT PRODUCTION HERE */')
host = OUT/'host.c'
host.write_text(prelude+types+constants+model+body+checks)
binary = OUT/'host'
command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
           '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-const-variable',
           '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
           '-fno-pie', '-no-pie', str(host), '-o', str(binary)]
subprocess.run(command, check=True)
result = subprocess.run([str(binary)], check=True, timeout=60, text=True, capture_output=True)
print(result.stdout, end='', flush=True)
(OUT/'audit.json').write_text(json.dumps(dict(
    hardware_tested=False, stock_replay=stock_replay, source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
    header_sha256=hashlib.sha256(header_path.read_bytes()).hexdigest(),
    core_header_sha256=hashlib.sha256(core_header_path.read_bytes()).hexdigest(),
    adc_source_sha256=hashlib.sha256(adc_path.read_bytes()).hexdigest(), stock_source_pin=pin,
    fixture_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    conversion_sha256=hashlib.sha256(blob).hexdigest(),
    host_source_sha256=hashlib.sha256(host.read_bytes()).hexdigest(),
    compiler_command=command, test_command=[str(binary)], exit_code=result.returncode,
    captured_stdout=result.stdout, captured_stderr=result.stderr,
    scope='Actual complete MFD probe, capture/getter and pure helpers; actual AUXADC reset through modeled child registration. Independent stock instruction conversion vectors and captured actual LK byte payloads. Framework/regmap/device locks and devres lifetime modeled, real pthread races, ASan/UBSan. No PMIC retention, voltage calibration or SOC validation.'
), indent=2)+'\n')
