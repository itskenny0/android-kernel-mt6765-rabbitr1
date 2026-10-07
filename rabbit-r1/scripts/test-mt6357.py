#!/usr/bin/env python3
"""Host checks of production MT6357 tables/helpers against Rabbit's vendor header.

This compiles production tables and new helpers with a read-only regmap stub.
It checks software mappings and error propagation, not electrical behavior.
"""
import os
from pathlib import Path
import re
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
out = ROOT/'out/mt6357-tests'
out.mkdir(parents=True, exist_ok=True)
source = ROOT/'src/mainline/drivers/regulator/mt6357-regulator.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must be under /rabbitr1')
s = source.read_text()
vendor = (ROOT/'src/kernel/include/linux/mfd/mt6357/registers.h').read_text().replace('\\\n', '')
defines = dict(re.findall(r'^#define\s+(\w+)\s+([^\n]+)', vendor, re.M))

def value(name):
    expression = defines[name].strip()
    try:
        return int(expression, 0)
    except ValueError:
        return value(expression)

registers = ROOT/'src/mainline/include/linux/mfd/mt6357/registers.h'
ids = ROOT/'src/mainline/include/linux/regulator/mt6357-regulator.h'
prelude = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <errno.h>
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define of_match_ptr(a) (a)
#define THIS_MODULE NULL
#define REGULATOR_VOLTAGE 0
#define REGULATOR_STATUS_ON 1
#define REGULATOR_STATUS_OFF 0
#define REGULATOR_LINEAR_RANGE(a,b,c,d) {a,b,c,d}
#define dev_err(...) ((void)0)
#define swap(a,b) do { __typeof__(a) tmp = (a); (a) = (b); (b) = tmp; } while (0)
struct linear_range { unsigned int min, min_sel, max_sel, step; };
struct regulator_desc {
    const char *name, *of_match, *regulators_node;
    const void *ops, *owner;
    unsigned int type, id, n_voltages, n_linear_ranges;
    const struct linear_range *linear_ranges;
    const int *volt_table;
    unsigned int vsel_reg, vsel_mask, enable_reg, enable_mask, min_uV;
};
struct regmap { unsigned int value, last_reg; int error; };
struct regulator_dev { struct regmap *regmap; void *data; };
static void *rdev_get_drvdata(struct regulator_dev *rdev) { return rdev->data; }
static int regmap_read(struct regmap *map, unsigned int reg, unsigned int *value)
{
    map->last_reg = reg;
    if (map->error) return map->error;
    *value = map->value;
    return 0;
}
'''
prelude += f'\n#include "{registers}"\n#include "{ids}"\n'
body = s[s.index('struct mt6357_regulator_info {'):s.index('static int mt6357_regulator_probe(')]
body = re.sub(r'static const struct regulator_ops (\w+) = \{.*?\n\};', r'static const int \1;', body, flags=re.S)
# The older helper uses signed storage with regmap_read; the real kernel build
# already checks its ABI. Keep this host test focused on the new code.
body = body.replace('int ret, regval;', 'int ret; unsigned int regval;')
checks = ['int main(void) {', 'struct mt6357_regulator_info regs[MT6357_MAX_REGULATOR];',
          'memcpy(regs, mt6357_regulators, sizeof(regs));',
          'struct regmap map = {0};', 'struct regulator_dev rdev = {.regmap = &map};']
rails = re.findall(r'\bMT6357_ID_(\w+)\s*,', ids.read_text())
rails = [r for r in rails if r != 'RG_MAX']
assert len(rails) == 32
for rail in rails:
    status_rail = 'VCN33' if rail.startswith('VCN33_') else rail
    addr = value(f'MT6357_DA_{status_rail}_EN_ADDR')
    mask = value(f'MT6357_DA_{status_rail}_EN_MASK') << value(f'MT6357_DA_{status_rail}_EN_SHIFT')
    checks += [f'assert(regs[MT6357_ID_{rail}].status_reg == {addr});',
               f'assert(regs[MT6357_ID_{rail}].status_mask == {mask});']
for rail in ['VSRAM_PROC', 'VSRAM_OTHERS']:
    prefix = f'MT6357_RG_LDO_{rail}_VOSEL'
    for field, expected in [('vsel_reg',value(prefix+'_ADDR')),
                            ('vsel_mask',value(prefix+'_MASK') << value(prefix+'_SHIFT'))]:
        checks.append(f'assert(regs[MT6357_ID_{rail}].desc.{field} == {expected});')
    checks.append(f'assert(regs[MT6357_ID_{rail}].da_vsel_mask == 0x7f00);')
checks += [r'''
for (unsigned int i = 0; i < MT6357_MAX_REGULATOR; i++) {
    rdev.data = &regs[i];
    map.value = regs[i].status_mask;
    assert(mt6357_get_status(&rdev) == REGULATOR_STATUS_ON);
    assert(map.last_reg == regs[i].status_reg);
    map.value = 0xffffU & ~regs[i].status_mask;
    assert(mt6357_get_status(&rdev) == REGULATOR_STATUS_OFF);
    map.error = -EIO;
    assert(mt6357_get_status(&rdev) == -EIO);
    map.error = 0;
}
map.value = 0x7fff;
assert(mt6357_apply_mrv_mapping(&map, regs) == 0);
assert(map.last_reg == MT6357_TOP2_ELR0);
assert(memcmp(regs, mt6357_regulators, sizeof(regs)) == 0);
map.error = -EIO;
assert(mt6357_apply_mrv_mapping(&map, regs) == -EIO);
assert(memcmp(regs, mt6357_regulators, sizeof(regs)) == 0);
map.error = 0;
map.value = BIT(15);
assert(mt6357_apply_mrv_mapping(&map, regs) == 0);
for (unsigned int i = 0; i < MT6357_MAX_REGULATOR; i++) {
    if (i != MT6357_ID_VSRAM_PROC && i != MT6357_ID_VSRAM_OTHERS) {
        assert(memcmp(&regs[i], &mt6357_regulators[i], sizeof(regs[i])) == 0);
        continue;
    }
    unsigned int other = i == MT6357_ID_VSRAM_PROC ? MT6357_ID_VSRAM_OTHERS : MT6357_ID_VSRAM_PROC;
    struct mt6357_regulator_info expected = mt6357_regulators[i];
    expected.desc.enable_reg = mt6357_regulators[other].desc.enable_reg;
    expected.desc.vsel_reg = mt6357_regulators[other].desc.vsel_reg;
    expected.da_vsel_reg = mt6357_regulators[other].da_vsel_reg;
    expected.status_reg = mt6357_regulators[other].status_reg;
    assert(memcmp(&regs[i], &expected, sizeof(expected)) == 0);
    rdev.data = &regs[i];
    map.value = 0x2a00;
    assert(mt6357_get_buck_voltage_sel(&rdev) == 0x2a);
    assert(map.last_reg == mt6357_regulators[other].da_vsel_reg);
}
puts("PASS: 32 status register/mask pairs match Rabbit vendor definitions; status/error paths; SRAM write/read masks; normal/MRV mappings and read failures");
return 0;
}
''']
test = out/'mt6357-host-test.c'
test.write_text(prelude+body+'\n'+'\n'.join(checks))
exe = out/'mt6357-host-test'
subprocess.run(['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O2',str(test),'-o',str(exe)],check=True)
subprocess.run([str(exe)],check=True)
