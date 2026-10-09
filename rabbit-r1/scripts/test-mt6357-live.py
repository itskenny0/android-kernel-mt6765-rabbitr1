#!/usr/bin/env python3
"""Test the production MT6357 live collector with latch-aware PMIC/IIO models."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path('/rabbitr1')
PROJECT = ROOT/'src/mainline/rabbit-r1'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=PROJECT.parent/'drivers/power/supply/mt6357-gauge.c')
parser.add_argument('--output', type=Path, default=ROOT/'out/mt6357-live')
args = parser.parse_args()
source, out = args.source.resolve(), args.output.resolve()
assert source.is_relative_to(ROOT) and out.is_relative_to(ROOT)
out.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
# Reuse the existing full translation-unit environment, stock source pin checks,
# production polling macros, probe resources and independent NTC/current oracle.
# Stop before its output/compile/run block; its original assertions stay intact.
runner = PROJECT/'scripts/test-mt6357-current.py'
script = runner.read_text()
prefix = script[:script.index("path = OUT/'host.c'")]
saved_argv = sys.argv
sys.argv = [str(runner), '--source', str(source)]
ns = {'__name__': '__mt6357_live_environment__', '__file__': str(runner)}
try:
    exec(compile(prefix, str(runner), 'exec'), ns)
finally:
    sys.argv = saved_argv

def replace_once(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after)

prelude = ns['prelude']
prelude = replace_once(prelude, '#define CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS 0',
                       '#ifndef CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS\n#define CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS 0\n#endif')
prelude = replace_once(prelude, 'static void mock_dev_info(const char *fmt, ...) { }',
                       'static unsigned int log_lines;\nstatic void mock_dev_info(const char *fmt, ...) { log_lines++; }')
for name, decl in [('iio_read_channel_raw', 'struct iio_channel *chan, int *v'),
                   ('iio_read_channel_scale', 'struct iio_channel *chan, int *n, int *d')]:
    prelude = replace_once(prelude, f'static int {name}({decl}) {{ assert(false); return -EIO; }}',
                           f'static int {name}({decl});')
prelude = replace_once(prelude, 'static u64 now;', 'static _Atomic u64 now;')
prelude = replace_once(prelude, 'return now * 1000;', 'return ++now * 1000;')
model = ns['model']
model = 'static unsigned int latch_count, current_words[512], car_words[512], baton_words[512];\n'+model
model = replace_once(model, 'reg == STOCK_CTRL_REG || reg == STOCK_DATA_REG);',
                       'reg == STOCK_CTRL_REG || reg == STOCK_DATA_REG || reg == MT6357_FGADC_CAR_CON0 || reg == MT6357_FGADC_CAR_CON1);')
model = replace_once(model, "int ret=record('r',reg,0);", "now++; int ret=record('r',reg,0);")
model = replace_once(model, 'regs[STOCK_DATA_REG]=live_sample;', '''assert(latch_count && latch_count<=ARRAY_SIZE(car_words));
            regs[STOCK_DATA_REG]=current_words[latch_count-1];
            regs[MT6357_FGADC_CAR_CON0]=car_words[latch_count-1]&0xffff;
            regs[MT6357_FGADC_CAR_CON1]=car_words[latch_count-1]>>16;
            regs[MT6357_BATON_ANA_CON0]=baton_words[latch_count-1];''')
model = replace_once(model, 'if (reg == STOCK_DATA_REG) {',
                       'if (reg == STOCK_DATA_REG || reg == MT6357_FGADC_CAR_CON0 || reg == MT6357_FGADC_CAR_CON1) {')
model = replace_once(model, "int ret=record('w',reg,val);", "now++; int ret=record('w',reg,val);")
model = replace_once(model, 'assert(!(val&STOCK_CLEAR_MASK)); start_pending=true;',
                       'assert(!(val&STOCK_CLEAR_MASK)); latch_count++; start_pending=true;')
model = replace_once(model, 'regs[STOCK_DATA_REG]=0xdead;',
                       'regs[MT6357_FGADC_CAR_CON0]=regs[MT6357_FGADC_CAR_CON1]=0xdead; regs[STOCK_DATA_REG]=0xdead;')
model = replace_once(model, 'static void *device_link_add(struct device *c, struct device *s, unsigned int flags)\n{ assert(s == &adc_provider && flags == DL_FLAG_AUTOREMOVE_CONSUMER); return step() ? NULL : s; }',
                       '''static unsigned int links;
static bool stale_channels;
static void *device_link_add(struct device *c, struct device *s, unsigned int flags)
{ assert(provider_locked && c == gauge.dev && s && s != c && flags == DL_FLAG_AUTOREMOVE_CONSUMER); links++; return step() ? NULL : (void *)1; }''')
model = replace_once(model, 'int ret=step(); *type=chan->type;', 'int ret=step(); assert(provider_locked); if (stale_channels) return -ENODEV; *type=chan->type;')
model = replace_once(model, 'int ret=step(); if (ret) return ERR_PTR(ret);\n    for (unsigned int i=0; i<ARRAY_SIZE(names); i++)', 'int ret=step(); assert(provider_locked); if (ret) return ERR_PTR(ret);\n    for (unsigned int i=0; i<ARRAY_SIZE(names); i++)')
# This suite observes the production probe's ordering directly; the old suite
# retains its immediate property-callback assertions in its unchanged harness.
old = ns['function']('devm_power_supply_register', model).strip()
new = '''static struct power_supply *devm_power_supply_register(struct device *dev,
    const struct power_supply_desc *desc, const struct power_supply_config *config)
{
    assert(config->drv_data == &gauge && config->fwnode == dev && desc == gauge.desc);
    assert(gauge.lock.initialized && gauge.regmap == &map && gauge.live_generation);
    assert(CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS ? log_lines>0 : !log_lines);
    int ret=step(); if (ret) return ERR_PTR(ret);
    if (fail_registration) return ERR_PTR(-EIO);
    registrations++; return &psy;
}'''
model = replace_once(model, old, new)
body = replace_once(ns['body'], ns['model'], model)
# Shared fixture setup only, not the old suite's main/test loops.
setup = ns['checks'][:ns['checks'].index('static void check_temperature')]
fixture = PROJECT/'tests/battery/mt6357-live.c'
host = prelude+ns['stock_defs']+body+ns['stock']+setup+fixture.read_text()
(out/'host.c').write_text(host)
for enabled in (0, 1):
    binary = out/f'host-{enabled}'
    subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-const-variable',
                    f'-DCONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS={enabled}', '-pthread',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                    str(out/'host.c'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)
paths = [source, runner, fixture, Path(__file__), PROJECT.parent/'drivers/iio/inkern.c',
         PROJECT.parent/'drivers/iio/industrialio-core.c', PROJECT.parent/'drivers/base/dd.c']
(out/'result.json').write_text(json.dumps({
    'passed': True, 'hardware_tested': False,
    'scope': 'Unmodified production gauge body, real polling macros, modeled PMIC/IIO/device links; both diagnostic configurations under ASan/UBSan; not physical coherency, device-core execution or SOC acceptance.',
    'sources': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
}, indent=2)+'\n')
