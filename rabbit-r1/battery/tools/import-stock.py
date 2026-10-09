#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Import verified stock DT data and captured ARM64 results; never derive an oracle from the C port."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import struct

ROOT = Path('/rabbitr1')
DEST = Path(__file__).resolve().parents[1]
EXPECTED = {
    'firmware/stock-v0.8.293/merged.dtb': 'e2b20d7cfc48bbf4498e8fd12610328eac8d2f6887d19d2c1df149e3145c87f4',
    'out/stock-kernel.elf': 'a03b5ff0b811de4c3d2b8c971b213b36274106536191f6588b2fca7372d4be75',
    'src/kernel/drivers/power/supply/mtk_battery_algo.c': '77fe6b856ef762f92d79c0fded5d0382dd89ff9c87412685ddeb48b29aeabbb8',
    'src/kernel/drivers/power/supply/mtk_battery_table.h': '9a0a18613be721cfb16f2a0a2e0d9159e1939a1fc96fbd0624736f9b4ec228f3',
    'src/kernel/drivers/power/supply/mtk_battery.h': '1539e6415c87ddcc9d652e07c9e6bc1bb9e150e8d21f69fb83324ac9b9c652a2',
    'src/kernel/arch/arm64/boot/dts/mediatek/bat_setting/mt6765_battery_table.dtsi': 'de9e7650a298f7a986c5419fe7ef6a02a3bcd3eb600d3223097d9a45cf0878a3',
    'out/battery-review/stock-soc-math-replay.json': 'aef27bd2249edc6ed4e0f1efe8276f3ed82f39b33822fee540011c51b2f63e04',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


for name, expected in EXPECTED.items():
    if sha((ROOT/name).read_bytes()) != expected:
        raise SystemExit('Source hash mismatch: ' + name)

spec = importlib.util.spec_from_file_location('r1_validate', ROOT/'scripts/validate-kernel.py')
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)
node = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())[
    '/pwrap@1000d000/main_pmic/mtk_gauge']


def cells(key):
    return struct.unpack('>' + str(len(node[key])//4) + 'i', node[key])


header = (ROOT/'src/kernel/drivers/power/supply/mtk_battery_table.h').read_text()


def capacities(name):
    block = re.search(r'int ' + name + r'\[MAX_TABLE\]\[TOTAL_BATTERY_NUMBER\] = \{(.*?)\n};',
                      header, re.S)[1]
    return [int(row[0])*10 for row in re.findall(r'\{\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\}', block)][:4]


qmax, qhigh = capacities('g_Q_MAX'), capacities('g_Q_MAX_H_CURRENT')
capture = json.loads((ROOT/'out/battery-review/stock-soc-math-replay.json').read_text())
edge_path = ROOT/'out/battery-review/stock-soc-boundaries.json'
edges = json.loads(edge_path.read_text())
assert edges['hardware_tested'] is False
assert edges['stock_kernel_elf_sha256'] == EXPECTED['out/stock-kernel.elf']
assert edges['stock_dtb_sha256'] == EXPECTED['firmware/stock-v0.8.293/merged.dtb']
output = ['/* SPDX-License-Identifier: GPL-2.0-only */',
          '/* Generated from the verified stock DTB; see provenance.json. */',
          '#include "profile.h"', '',
          'const struct r1_battery_table r1_battery_stock_tables[R1_BATTERY_TABLES] = {']
for index in range(4):
    temperature = cells(f'TEMPERATURE_T{index}')[0]
    values = cells(f'battery0_profile_t{index}')
    assert cells(f'battery0_profile_t{index}_num') == (53,) and len(values) == 53*3
    rows = [list(values[i:i+3]) for i in range(0, len(values), 3)]
    oracle = next(case for case in capture['temperature_profiles'] if case['temp_c'] == temperature)
    assert rows == oracle['profile']
    assert qmax[index] == oracle['qmax_01mah'] and qhigh[index] == oracle['qmax_high_01mah']
    output += ['    {', f'        .temperature_c = {temperature},',
               f'        .qmax_01mah = {qmax[index]}, .qmax_high_01mah = {qhigh[index]},',
               '        .count = 53,', '        .points = {']
    output += ['            {' + ', '.join(map(str, row)) + '},' for row in rows]
    output += ['        },', '    },']
output += ['};', '']
(DEST/'stock_profiles.c').write_text('\n'.join(output))
fixtures = {key: capture[key] for key in ('temperature_profiles', 'ocv_vectors', 'live_counter_vectors')}
fixtures.update({key: edges[key] for key in ('normalization_vectors', 'repeated_dod_vectors')})
(DEST/'tests/stock-oracles.json').write_text(json.dumps(fixtures, separators=(',', ':')) + '\n')
provenance = {
    'hardware_tested': False,
    'battery_profile_id': 0,
    'source_files_sha256': EXPECTED,
    'boundary_capture_sha256': sha(edge_path.read_bytes()),
    'stock_function_hashes': capture['function_hashes'],
    'replay_script_sha256': sha((DEST/'tests/replay-stock.py').read_bytes()),
    'generated_sha256': {name: sha((DEST/name).read_bytes()) for name in ('stock_profiles.c', 'tests/stock-oracles.json')},
    'scope': '53 explicit DT rows only; usable capacities in conversion fixtures are supplied inputs, not measured capacity. No cutoff/RAC estimator or initialization is implemented.',
}
(DEST/'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
print('Imported four verified 53-row stock curves and independent ARM64 instruction fixtures')
