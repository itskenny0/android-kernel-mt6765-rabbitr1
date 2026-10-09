#!/usr/bin/env python3
"""Test actual MT6765 bus-protection helpers and provider admission on the host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path('/rabbitr1')
PROJECT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=ROOT / 'src/mainline')
parser.add_argument('--out', type=Path, required=True, help='new absolute output directory under /rabbitr1')
args = parser.parse_args()
if not args.source.is_absolute() or not args.source.resolve().is_relative_to(ROOT):
    parser.error('--source must be absolute and stay under /rabbitr1')
source = args.source.resolve()
if not args.out.is_absolute() or not args.out.resolve().is_relative_to(ROOT):
    parser.error('--out must be absolute and stay under /rabbitr1')
out = args.out.resolve()
if os.path.lexists(args.out) or out.is_relative_to(source) or source.is_relative_to(out):
    parser.error('--out must be absent and separate from the source tree')
fixtures = PROJECT / 'tests/gpu'
inputs = {}

def read(path):
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f'Input escapes /rabbitr1: {path}')
    data = path.read_bytes()
    inputs[path] = data
    return data

def digest(data):
    return hashlib.sha256(data).hexdigest()

contract = json.loads(read(fixtures / 'mt6765-power-contract.json'))
if contract['schema'] != 1 or contract['source_record'] != 'mt6765-bus-protection.json':
    raise ValueError('Unexpected source-contract record')
record_bytes = read(fixtures / contract['source_record'])
if digest(record_bytes) != contract['source_record_sha256']:
    raise ValueError('Source-contract record changed; review its fixture pins')
record = json.loads(record_bytes)
if contract['stock_source'] not in record['stock_sources']:
    raise ValueError('Oracle does not identify a pinned stock source')
if contract['oracle']['file'] != 'mt6765-mfg-stock-oracle.h':
    raise ValueError('Unexpected oracle fixture')
oracle = read(fixtures / contract['oracle']['file'])
if digest(oracle) != contract['oracle']['sha256']:
    raise ValueError('Stock-sequence oracle differs from its pin')

relative_inputs = [
    'drivers/pmdomain/mediatek/mtk-pm-domains.c',
    'drivers/pmdomain/mediatek/mtk-pm-domains.h',
    'drivers/pmdomain/mediatek/mt6765-pm-domains.h',
    'include/dt-bindings/power/mediatek,mt6765-power.h',
]
snapshots = {name: read(source / name) for name in relative_inputs}
body = snapshots[relative_inputs[0]].decode()
operation = read(fixtures / 'mt6765-power-sequence.c').decode()
provider = read(fixtures / 'mt6765-power-provider.c').decode()
read(Path(__file__))

# Keep extraction narrow: missing/ambiguous functions or probe boundaries fail.
def function(name):
    matches = list(re.finditer(r'^static [^;\n]*\b' + re.escape(name) + r'\([^;]+?\n\{', body, re.M))
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one function: {name}')
    match = matches[0]
    pos, depth = match.end(), 1
    while depth:
        if pos >= len(body):
            raise ValueError(f'Unterminated function: {name}')
        if body[pos] == '{':
            depth += 1
        elif body[pos] == '}':
            depth -= 1
        pos += 1
    return body[match.start():pos]

def insert(text, marker, value):
    if text.count(marker) != 1:
        raise ValueError(f'Expected exactly one fixture marker: {marker}')
    return text.replace(marker, value, 1)

helpers = '\n'.join(function(name) for name in [
    'scpsys_bus_protect_get_regmap', 'scpsys_bus_protect_get_sta_regmap',
    'scpsys_bus_protect_clear', 'scpsys_bus_protect_set',
    'scpsys_bus_protect_enable', 'scpsys_bus_protect_disable',
])
operation = insert(operation, '/* @BUS_HELPERS@ */', helpers)
helpers = '\n'.join(function(name) for name in [
    'scpsys_bus_protect_get_regmap', 'scpsys_get_bus_protection_legacy',
    'scpsys_get_bus_protection', 'scpsys_validate_bus_protection_domain',
])
provider = insert(provider, '/* @PROVIDER_HELPERS@ */', helpers)
probe = function('scpsys_probe')
initialization = re.findall(r'^\tmemset\(scpsys->bus_prot_index,[^\n]+', probe, re.M)
if len(initialization) != 1:
    raise ValueError('Missing unambiguous map-index initialization')
start = probe.index('\tif (of_find_property(np, "access-controllers", NULL))')
end = probe.index('\tret = -ENODEV;', start)
pipeline = ('static int pipeline(struct device *dev,struct scpsys *scpsys){\n'
            'struct device_node *np=dev->of_node;int ret;\n' + initialization[0] + '\n' +
            probe[start:end] + '\nadmitted++;return 0;\n}\n')
provider = insert(provider, '/* @PROBE_ADMISSION@ */', pipeline)

out.mkdir(parents=True, exist_ok=False)
include = out / 'include'
(include / 'dt-bindings/power').mkdir(parents=True)
for name, data in snapshots.items():
    target = out / 'source' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    if name.endswith('.h'):
        dest = include / (name.removeprefix('include/') if name.startswith('include/') else Path(name).name)
        dest.write_bytes(data)
(include / contract['oracle']['file']).write_bytes(oracle)
commands, results = [], {}
for name, code, extra in [
    ('operation', operation, []),
    ('provider', provider, ['-Wno-misleading-indentation', '-Wno-sign-compare', '-Wno-missing-field-initializers']),
]:
    c = out / f'{name}.c'
    binary = out / name
    c.write_text(code)
    command = ['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', *extra,
               '-O1', '-g', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
               '-no-pie', '-I' + str(include), str(c), '-o', str(binary)]
    commands.append(command)
    compiled = subprocess.run(command, capture_output=True, text=True)
    (out / f'{name}-build.log').write_text(compiled.stdout + compiled.stderr)
    compiled.check_returncode()
    commands.append([str(binary)])
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    (out / f'{name}-run.log').write_text(ran.stdout + ran.stderr)
    ran.check_returncode()
    result = json.loads(ran.stdout)
    if result.get('passed') is not True or result['controls'] != contract['controls'][name]:
        raise ValueError(f'Unexpected {name} result')
    results[name] = result
for path, data in inputs.items():
    if path.read_bytes() != data:
        raise ValueError(f'Input changed during checks: {path}')
summary = {
    'passed': True, 'controls': sum(result['controls'] for result in results.values()),
    'scope': 'Actual mainline functions/table/probe admission; modeled OF/regmap/allocation. No hardware, full-probe, lifetime, kernel-build or DT-schema proof.',
    'vendor_checkout_required': False, 'results': results, 'commands': commands,
    'inputs': [{'path': str(path), 'bytes': len(data), 'sha256': digest(data)} for path, data in inputs.items()],
    'generated': [{'path': str(out / name), 'sha256': digest((out / name).read_bytes())}
                  for name in ['operation.c', 'provider.c']],
    'input_bytes_unchanged': True,
}
(out / 'result.json').write_text(json.dumps(summary, indent=2) + '\n')
print(f'PASS: {summary["controls"]} MT6765 power controls; evidence: {out / "result.json"}')
