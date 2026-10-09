#!/usr/bin/env python3
"""Test the production read-only RTC provider against stock mapping and lifecycle faults."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6357-rtc-nvmem'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/rtc/rtc-mt6397.c')
parser.add_argument('--config', type=Path, help='also check an actual generated product .config')
parser.add_argument('--dtb', type=Path, help='also check a compiled r1 DTB')
args = parser.parse_args()
for value in (args.source, args.config, args.dtb):
    if value is not None and not value.resolve().is_relative_to(ROOT):
        raise SystemExit('All input paths must stay under /rabbitr1')
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = args.source.resolve()
s = source.read_text()
project = Path(__file__).resolve().parent.parent
lock = json.loads((project/'sources.lock.json').read_text())
stock_path = ROOT/'src/kernel/drivers/rtc/rtc-mt6397.c'
stock = stock_path.read_bytes()
pin = lock['ci_files'][str(stock_path.relative_to(ROOT))]
assert len(stock) == pin['bytes'] and hashlib.sha256(stock).hexdigest() == pin['sha256']
stock = stock.decode()
# Independent stock register-field table, not production constants or conversions.
stock_defines = dict(re.findall(r'^#define\s+(RTC_AL_(?:HOU|MTH))\s+(0x[0-9a-fA-F]+)', stock, re.M))
fields = re.findall(r'\[(SPARE_AL_(?:HOU|MTH))\]\s*= REG_FIELD\((RTC_AL_(?:HOU|MTH)), (\d+), (\d+)\)', stock)
assert fields == [('SPARE_AL_HOU', 'RTC_AL_HOU', '8', '15'),
                  ('SPARE_AL_MTH', 'RTC_AL_MTH', '8', '15')]
fixture_path = project/'tests/battery/mt6357-rtc-mapping.json'
fixture = json.loads(fixture_path.read_text())
assert fixture['stock_rtc_source_sha256'] == pin['sha256']
assert [int(stock_defines[item[1]], 16) for item in fields] == fixture['register_offsets']
assert re.search(r'enum mtk_rtc_spare_enum\s*\{\s*SPARE_AL_HOU,\s*SPARE_AL_MTH,', stock)
assert fixture['registers'] == [fixture['base']+x for x in fixture['register_offsets']]
required = ('RTC_CLASS', 'RTC_DRV_MT6397', 'RTC_NVMEM', 'NVMEM', 'NVMEM_SYSFS')
fragment = (project/'configs/mainline-r1.config').read_text()
for key in required:
    assert f'CONFIG_{key}=y\n' in fragment, key
if args.config:
    config = args.config.read_text()
    for key in required:
        assert f'CONFIG_{key}=y\n' in config, key
    print('PASS: actual generated RTC/NVMEM product configuration', flush=True)
if args.dtb:
    spec = importlib.util.spec_from_file_location('r1_validate_kernel', project/'scripts/validate-kernel.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    nodes = validator.fdt_nodes(args.dtb.read_bytes())
    rtc_paths = [name for name, props in nodes.items()
                 if props.get('compatible') == b'mediatek,mt6357-rtc\0']
    assert len(rtc_paths) == 1
    layout = rtc_paths[0]+'/nvmem-layout'
    assert nodes[layout]['compatible'] == b'fixed-layout\0'
    assert nodes[layout+'/initialization@0']['reg'].hex() == '0000000000000001'
    assert nodes[layout+'/state-of-charge@1']['reg'].hex() == '0000000100000001'
    for name, props in nodes.items():
        if props.get('compatible') == b'mediatek,mt6357-gauge\0':
            assert 'nvmem-cells' not in props and 'nvmem-cell-names' not in props
    print('PASS: compiled RTC fixed cells and no gauge consumer', flush=True)


def macro(name, text):
    match = re.search(r'^#define '+name+r'\(', text, re.M)
    assert match, name
    end = text.index('\n', match.end())
    while text[end-1] == '\\':
        end = text.index('\n', end+1)
    return text[match.start():end+1]


fixture_c = project/'tests/battery/mt6357-rtc-nvmem.c'
prelude, model, checks = fixture_c.read_text().split('/* INSERT PRODUCTION HERE */')
header_path = SRC/'include/linux/mfd/mt6397/rtc.h'
header = header_path.read_text()
header = header[header.index('#define RTC_BBPU'):header.rindex('#endif')]
helpers = ''
for name in ('poll_timeout_us', 'read_poll_timeout'):
    helpers += macro(name, (SRC/'include/linux/iopoll.h').read_text())
helpers += macro('regmap_read_poll_timeout', (SRC/'include/linux/regmap.h').read_text())
body = s[s.index('#define MT6357_RTC_SPARE_SIZE'):s.index('#ifdef CONFIG_PM_SLEEP')]
body += s[s.index('static const struct mtk_rtc_data mt6357_rtc_data'):s.index('static const struct of_device_id')]
stock_constants = f'#define STOCK_BASE {fixture["base"]}U\n'
stock_constants += ''.join(f'#define STOCK_SPARE_{i} {reg}U\n' for i, reg in enumerate(fixture['registers']))
path = OUT/'host.c'
path.write_text(prelude+header+helpers+stock_constants+model+body+checks)
results = []
for enabled in (1, 0):
    binary = OUT/f'host-{enabled}'
    command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
               '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-const-variable',
               '-Wno-pointer-sign',  # Existing RTC read_time passes int* to regmap_read.
               '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
               '-fno-pie', '-no-pie', f'-DCONFIG_RTC_NVMEM={enabled}', str(path), '-o', str(binary)]
    subprocess.run(command, check=True)
    result = subprocess.run([str(binary)], check=True, timeout=60, text=True, capture_output=True)
    print(result.stdout, end='', flush=True)
    results.append(dict(compiler_command=command, test_command=[str(binary)],
                        captured_stdout=result.stdout, captured_stderr=result.stderr,
                        exit_code=result.returncode))
(OUT/'audit.json').write_text(json.dumps(dict(
    hardware_tested=False, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    header_sha256=hashlib.sha256(header_path.read_bytes()).hexdigest(),
    stock_source_pin=pin, mapping_fixture_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    host_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), results=results,
    generated_config=str(args.config) if args.config else None,
    compiled_dtb=str(args.dtb) if args.dtb else None,
    scope='Actual RTC NVMEM callback, complete probe, alarm read/set and trigger; actual kernel polling macros. Stock register-field mapping is independent. Modeled PMIC, device/RTC locks, devres/RTC/NVMEM registration and parent lifetime, real pthread races, ASan/UBSan. No retention/SOC calibration or physical-device claim.'
), indent=2)+'\n')
