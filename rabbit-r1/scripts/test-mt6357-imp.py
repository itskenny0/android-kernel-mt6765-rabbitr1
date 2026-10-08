#!/usr/bin/env python3
"""Exercise the production MT6357 impedance path against pinned Rabbit definitions."""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6357-imp'
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = SRC/'drivers/iio/adc/mt6359-auxadc.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must stay under /rabbitr1')
s = source.read_text()
lock = json.loads((SRC/'rabbit-r1/sources.lock.json').read_text())
for name in ('src/kernel/drivers/iio/adc/mt635x-auxadc.c',
             'src/kernel/include/linux/mfd/mt6357/registers.h'):
    data = (ROOT/name).read_bytes()
    assert len(data) == lock['ci_files'][name]['bytes']
    assert hashlib.sha256(data).hexdigest() == lock['ci_files'][name]['sha256']
registers = (ROOT/'src/kernel/include/linux/mfd/mt6357/registers.h').read_text().replace('\\\n', '')
defines = dict(re.findall(r'^#define\s+(\w+)\s+([^\n]+)', registers, re.M))


def value(name):
    try:
        return int(defines[name].strip(), 0)
    except ValueError:
        return value(defines[name].strip())


def field(name):
    return value(name+'_ADDR'), value(name+'_MASK') << value(name+'_SHIFT')


def function(name, text=s):
    match = re.search(r'static [\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+2]+'\n'


mode = field('MT6357_RG_AUXADC_IMP_CK_SW_MODE')
enable = field('MT6357_RG_AUXADC_IMP_CK_SW_EN')
auto = field('MT6357_AUXADC_IMP_AUTORPT_EN')
irq_clear = field('MT6357_AUXADC_IMPEDANCE_IRQ_CLR')
counter_clear = field('MT6357_AUXADC_CLR_IMP_CNT_STOP')
ready = field('MT6357_AUXADC_IMPEDANCE_IRQ_STATUS')
output = field('MT6357_AUXADC_ADC_OUT_IMP')
assert mode[0] == enable[0] and irq_clear[0] == counter_clear[0] == ready[0]
clear = irq_clear[1] | counter_clear[1]
start_sequence = [(reg, mask, mask) for reg, mask in (mode, enable, auto)]
stop_sequence = [(irq_clear[0], clear, clear), (irq_clear[0], clear, 0),
                 (auto[0], auto[1], 0), (mode[0], mode[1], 0), (enable[0], enable[1], enable[1])]
vendor = (ROOT/'src/kernel/drivers/iio/adc/mt635x-auxadc.c').read_text()
vendor_masks = dict(re.findall(r'^#define\s+(MT6357_IMP_\w+)\s+([^\n]+)', vendor, re.M))


def vendor_mask(name):
    if name == '0':
        return 0
    expr = vendor_masks[name]
    assert not re.sub(r'BIT\(\d+\)|[|()\s]', '', expr), expr
    mask = 0
    for bit in re.findall(r'BIT\((\d+)\)', expr):
        mask |= 1 << int(bit)
    return mask


def vendor_sequence(name):
    calls = re.findall(r'regmap_update_bits\(adc_dev->regmap,\s*(MT6357_\w+),\s*'
                       r'(MT6357_\w+),\s*(MT6357_\w+|0)\)', function(name, vendor))
    return [(value(reg), vendor_mask(mask), vendor_mask(val)) for reg, mask, val in calls]


assert start_sequence == vendor_sequence('mt6357_imp_conv')
assert stop_sequence == vendor_sequence('mt6357_imp_stop')

prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef int8_t s8;
typedef uint16_t u16;
typedef uint32_t u32;
struct u8_fract { u8 numerator, denominator; };
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define __stringify(s) #s
#define IIO_RESISTANCE 0
#define IIO_CURRENT 1
#define IIO_VOLTAGE 2
#define IIO_TEMP 3
#define IIO_CPU 0
#define IIO_CHAN_INFO_RAW 0
#define IIO_CHAN_INFO_SCALE 1
#define IIO_VAL_INT 1
#define IIO_VAL_FRACTIONAL 10
#define dev_warn(...) ((void)0)
struct device { int unused; };
struct mutex { bool locked; };
struct lock_scope { struct mutex *m; };
static struct lock_scope lock_acquire(struct mutex *m)
{
    assert(!m->locked); m->locked=true; return (struct lock_scope){m};
}
static void lock_drop(struct lock_scope *s) { assert(s->m->locked); s->m->locked=false; }
#define guard(kind) struct lock_scope held __attribute__((cleanup(lock_drop))) = lock_acquire
struct iio_chan_spec {
    int type, channel, scan_index, indexed;
    unsigned long address, info_mask_separate;
    const char *datasheet_name;
    struct { char sign; int realbits, storagebits, endianness; } scan_type;
};
struct iio_dev { void *priv; };
static void *iio_priv(struct iio_dev *dev) { return dev->priv; }
struct regmap { unsigned int regs[0x2000]; struct mutex *lock; };
struct event { unsigned int op, reg, mask, val; };
static struct event events[128];
static unsigned int num_events, fail_at, second_fail_at, sample, polls, ready_after;
static bool timeout, write_on_error, saw_ready, saw_data, clobber_data_on_stop;
#define POLL_BUDGET 34
'''
for name, val in dict(CLOCK_REG=mode[0], CLOCK_MODE=mode[1], CLOCK_ENABLE=enable[1],
                      AUTO_REG=auto[0], AUTO_MASK=auto[1], READY_REG=ready[0], READY_MASK=ready[1],
                      CLEAR_MASK=clear, DATA_REG=output[0], DATA_MASK=output[1]).items():
    prelude += f'#define {name} {val}U\n'
prelude += r'''
static int record(struct regmap *map, unsigned int op, unsigned int reg,
                  unsigned int mask, unsigned int val)
{
    assert(map->lock->locked && num_events < ARRAY_SIZE(events));
    events[num_events++] = (struct event){op, reg, mask, val & mask};
    if (num_events == fail_at) return -EIO;
    if (num_events == second_fail_at) return -ENXIO;
    return 0;
}
static int regmap_update_bits(struct regmap *map, unsigned int reg,
                              unsigned int mask, unsigned int val)
{
    int ret=record(map,2,reg,mask,val);
    assert((reg == CLOCK_REG && (mask == CLOCK_MODE || mask == CLOCK_ENABLE)) ||
           (reg == AUTO_REG && mask == AUTO_MASK) || (reg == READY_REG && mask == CLEAR_MASK) ||
           (reg == 0xf90 && mask == 9) || (reg == 0x110e && mask == 0x80) ||
           (reg == 0x111a && mask == 0x400));
    if (!ret || write_on_error) {
        map->regs[reg] = (map->regs[reg] & ~mask) | (val & mask);
        if (reg == READY_REG && (val & CLEAR_MASK) && clobber_data_on_stop)
            sample=0; /* A value must be read before the conversion is cleared. */
    }
    return ret;
}
static int regmap_write(struct regmap *map, unsigned int reg, unsigned int val)
{
    assert(!"MT6357 impedance path must not overwrite a shared register");
    return -EIO;
}
static int regmap_read(struct regmap *map, unsigned int reg, unsigned int *val)
{
    int ret=record(map,0,reg,0,0);
    if (ret) return ret;
    assert((map->regs[CLOCK_REG] & (CLOCK_MODE|CLOCK_ENABLE)) == (CLOCK_MODE|CLOCK_ENABLE));
    assert(map->regs[AUTO_REG] & AUTO_MASK);
    if (reg == READY_REG) {
        saw_ready=!timeout && polls++ >= ready_after;
        *val=(map->regs[reg] & ~READY_MASK) | (saw_ready ? READY_MASK : 0);
    } else {
        assert(reg == DATA_REG && saw_ready);
        saw_data=true; *val=sample;
    }
    return 0;
}
#define regmap_set_bits(m,r,b) regmap_update_bits(m,r,b,b)
#define regmap_clear_bits(m,r,b) regmap_update_bits(m,r,b,0)
#define regmap_read_poll_timeout(m,r,v,cond,delay_us,timeout_us) ({ \
    int rc=-ETIMEDOUT; \
    assert((delay_us) == 1000 && (timeout_us) == 32000); \
    for (int poll=0; poll<POLL_BUDGET; poll++) { \
        rc=regmap_read(m,r,&v); if (rc || (cond)) break; rc=-ETIMEDOUT; \
    } rc; })
'''
prelude += f'#include "{SRC}/include/dt-bindings/iio/adc/mediatek,mt6357-auxadc.h"\n'
body = s[s.index('#define AUXADC_AVG_TIME_US'):s.index('static const struct iio_chan_spec mt6358_auxadc_channels')]
for name in ('mt6357_stop_imp_conv', 'mt6357_read_imp',
             'mt6358_stop_imp_conv', 'mt6358_start_imp_conv', 'mt6358_read_imp',
             'mt6357_auxadc_restore_requests', 'mt6359_auxadc_reset'):
    body += function(name)
body += r'''
/* Regular conversions are covered separately by test-mt6357-auxadc.py. */
static int mt6359_auxadc_read_adc(struct mt6359_auxadc *a, const struct iio_chan_spec *c, int *out)
{ assert(!"unexpected regular conversion"); return -EOPNOTSUPP; }
static int mt6357_auxadc_read_adc(struct mt6359_auxadc *a, const struct iio_chan_spec *c, int *out)
{ assert(!"unexpected regular conversion"); return -EOPNOTSUPP; }
'''
body += function('mt6359_auxadc_read_raw')
start = s.index('static const struct mtk_pmic_auxadc_info mt6357_chip_info = {')
body += s[start:s.index('\n};', start)+3]+'\n#define chip mt6357_chip_info\n'
for name, sequence in (('start', start_sequence), ('stop', stop_sequence)):
    body += f'static const struct event stock_{name}[] = {{\n'
    body += ''.join(f'{{2,{reg},{mask},{val}}},\n' for reg,mask,val in sequence)
    body += '};\n'
checks = r'''
static struct regmap map;
static struct mt6359_auxadc adc={.regmap=&map,.chip_info=&chip};
static struct iio_dev dev={.priv=&adc};
static const struct iio_chan_spec *bat;
static void setup(unsigned int seed)
{
    assert(!adc.lock.locked);
    memset(&map,0,sizeof(map)); map.lock=&adc.lock;
    map.regs[CLOCK_REG]=map.regs[AUTO_REG]=map.regs[READY_REG]=seed;
    adc.needs_reset=adc.timed_out=false;
    num_events=fail_at=second_fail_at=polls=ready_after=0;
    timeout=saw_ready=saw_data=false; write_on_error=true; clobber_data_on_stop=true;
    sample=0xc321;
}
static void check_cleanup(unsigned int offset)
{
    assert(num_events == offset+ARRAY_SIZE(stock_stop));
    for (unsigned int n=0; n<ARRAY_SIZE(stock_stop); n++)
        assert(!memcmp(&events[offset+n],&stock_stop[n],sizeof(events[0])));
}
static void check_final(unsigned int seed)
{
    assert(map.regs[CLOCK_REG] == ((seed & ~CLOCK_MODE) | CLOCK_ENABLE));
    assert(map.regs[AUTO_REG] == (seed & ~AUTO_MASK));
    assert(map.regs[READY_REG] == (seed & ~CLEAR_MASK));
}
static int read_raw(int *out)
{
    int ret=mt6359_auxadc_read_raw(&dev,bat,out,NULL,IIO_CHAN_INFO_RAW);
    assert(!adc.lock.locked);
    return ret;
}
int main(void)
{
    unsigned int good=0, failures=0, recovery=0;
    for (unsigned int i=0; i<ARRAY_SIZE(mt6357_auxadc_channels); i++) {
        const struct iio_chan_spec *c=&mt6357_auxadc_channels[i];
        assert(c->scan_index != PMIC_AUXADC_CHAN_IBAT);
        if (c->channel == MT6357_AUXADC_VBAT) bat=c;
    }
    assert(bat && bat->scan_type.realbits == 15 && bat->type == IIO_VOLTAGE);
    assert(chip.regs[PMIC_AUXADC_ADC0]+2*chip.imp_adc_num == DATA_REG);
    assert(chip.regs[chip.desc[bat->scan_index].rdy_idx] == READY_REG);
    assert(chip.desc[bat->scan_index].rdy_mask == READY_MASK);
    const unsigned int seeds[]={0,0xffff};
    const unsigned int values[]={0,1,0x4000,0x7fff,0x8000,0x8001,0xc000,0xffff};
    const unsigned int delays[]={0,2,32};
    for (unsigned int seed=0; seed<ARRAY_SIZE(seeds); seed++)
    for (unsigned int value=0; value<ARRAY_SIZE(values); value++)
    for (unsigned int delay=0; delay<ARRAY_SIZE(delays); delay++) {
        setup(seeds[seed]); sample=values[value]; ready_after=delays[delay];
        int out=-7;
        assert(read_raw(&out) == IIO_VAL_INT && out == (int)(values[value]&DATA_MASK));
        assert(saw_data && !adc.needs_reset && !adc.timed_out);
        for (unsigned int i=0; i<ARRAY_SIZE(stock_start); i++)
            assert(!memcmp(&events[i],&stock_start[i],sizeof(events[i])));
        assert(events[4+delays[delay]].reg == DATA_REG);
        check_cleanup(5+delays[delay]); check_final(seeds[seed]);
        int numerator=-7,denominator=-7;
        unsigned int before=num_events;
        assert(mt6359_auxadc_read_raw(&dev,bat,&numerator,&denominator,IIO_CHAN_INFO_SCALE) == IIO_VAL_FRACTIONAL);
        assert(numerator == 5400 && denominator == 32768 && num_events == before);
        good++;
    }
    for (unsigned int seed=0; seed<ARRAY_SIZE(seeds); seed++)
    for (unsigned int mode=0; mode<2; mode++)
    for (unsigned int fail=1; fail<=10; fail++) {
        setup(seeds[seed]); write_on_error=mode; fail_at=fail;
        int out=-7;
        assert(read_raw(&out) == -EIO && out == -7);
        assert(adc.needs_reset == (fail >= 6));
        check_cleanup(fail <= 5 ? fail : 5);
        if (fail <= 5 || write_on_error) check_final(seeds[seed]);
        failures++;
        if (adc.needs_reset) {
            /* A failed recovery must not make another impedance request. */
            num_events=0; fail_at=1;
            assert(read_raw(&out) == -EIO && out == -7 && adc.needs_reset);
            assert(num_events == 2 && events[0].reg == 0xf90 && events[1].reg == 0xf90);
            num_events=fail_at=polls=0; sample=0xc321;
            assert(read_raw(&out) == IIO_VAL_INT && out == 0x4321 && !adc.needs_reset);
            assert(events[0].reg == 0xf90 && events[2].reg == 0x110e && events[3].reg == 0x111a);
            check_cleanup(9); recovery++;
        }
    }
    /* Keep the first failure, but mark hardware uncertain if cleanup also fails. */
    for (unsigned int primary=1; primary<=5; primary++)
    for (unsigned int cleanup=1; cleanup<=5; cleanup++) {
        setup(0xffff); fail_at=primary; second_fail_at=primary+cleanup;
        int out=-7;
        assert(read_raw(&out) == -EIO && out == -7 && adc.needs_reset);
        check_cleanup(primary); failures++;
    }
    /* Timeout never reads data; every cleanup failure remains visible in state. */
    for (unsigned int fail=0; fail<=5; fail++) {
        setup(0xffff); timeout=true;
        fail_at=fail ? 3+POLL_BUDGET+fail : 0;
        int out=-7;
        assert(read_raw(&out) == -ETIMEDOUT && out == -7 && !saw_data);
        assert(adc.needs_reset == (fail != 0) && adc.timed_out);
        check_cleanup(3+POLL_BUDGET); failures++;
    }
    setup(0); timeout=true; adc.timed_out=true;
    int out=-7;
    assert(read_raw(&out) == -ETIMEDOUT && out == -7 && !adc.needs_reset);
    assert(num_events == 3+POLL_BUDGET+5+4 && events[num_events-4].reg == 0xf90);
    recovery++;
    setup(0); adc.lock.locked=true;
    int current=-9;
    assert(chip.read_imp(&adc,bat,&out,&current) == -EOPNOTSUPP);
    assert(chip.read_imp(&adc,bat,NULL,&current) == -EOPNOTSUPP);
    assert(chip.read_imp(&adc,bat,NULL,NULL) == -EINVAL);
    assert(out == -7 && current == -9 && num_events == 0);
    adc.lock.locked=false;
    printf("PASS: %u impedance conversions, %u fault/timeout cases and %u recovery cases; stock MT6357 sequencing, 15-bit data, cleanup, error propagation and unsupported-current rejection\n",good,failures,recovery);
    return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+body+checks)
subprocess.run(['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
                '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-const-variable',
                '-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie',
                str(path),'-o',str(OUT/'host')],check=True)
subprocess.run([str(OUT/'host')],check=True)
report = dict(start_sequence=start_sequence, stop_sequence=stop_sequence,
              data_register=output[0], data_mask=output[1], hardware_tested=False,
              scope='actual MT6357 impedance/reset/read_raw code; modeled regmap, locks and readiness; no physical timing, current or electrical validation')
(ROOT/'out/mt6357-imp-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print('No device was accessed. Charging and MT6357 ADC activation remain separate integration work.')
