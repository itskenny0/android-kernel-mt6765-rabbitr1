#!/usr/bin/env python3
"""Check production MT6357 ADC code against Rabbit's register/channel definitions."""
import json
import hashlib
import os
from pathlib import Path
import re
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6357-auxadc'
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
vendor = (ROOT/'src/kernel/drivers/iio/adc/mt635x-auxadc.c').read_text()
registers = (ROOT/'src/kernel/include/linux/mfd/mt6357/registers.h').read_text().replace('\\\n', '')
defines = dict(re.findall(r'^#define\s+(\w+)\s+([^\n]+)', registers, re.M))


def value(name):
    try:
        return int(defines[name].strip(), 0)
    except ValueError:
        return value(defines[name].strip())


def function(name):
    # The model callback also has a forward declaration; select its definition.
    match = re.search(r'static [\w *]+\b'+name+r'\([^;]*?\)\n\{', s)
    assert match, name
    return s[match.start():s.index('\n}', match.end())+2]+'\n'


table = vendor.split('static const struct auxadc_regs mt6357_auxadc_regs_tbl[] = {', 1)[1].split('};', 1)[0]
mapping = re.findall(r'MT635x_AUXADC_REG\((\w+), MT6357, (\w+), (\d+), (\w+)\)', table)
bits = {name: int(width) for name, width in re.findall(
    r'MT635x_AUXADC_CHANNEL\((\w+), \d+, (\d+), true\)', vendor)}
assert len(mapping) == 12
expected = []
for name, request, shift, output in mapping:
    expected.append((name, value('MT6357_'+request), 1 << int(shift),
                     value('MT6357_'+output), bits[name], 3 if name in ('BATADC', 'ISENSE') else 1))

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
#define FIELD_PREP(mask,v) (((v) << __builtin_ctz(mask)) & (mask))
#define __stringify(s) #s
#define IIO_RESISTANCE 0
#define IIO_CURRENT 1
#define IIO_VOLTAGE 2
#define IIO_TEMP 3
#define IIO_CPU 0
#define IIO_CHAN_INFO_RAW 0
#define IIO_CHAN_INFO_SCALE 1
#define IIO_CHAN_INFO_OFFSET 2
#define IIO_VAL_INT 1
#define IIO_VAL_FRACTIONAL 10
#define dev_dbg(...) ((void)0)
#define dev_warn(...) ((void)0)
#define dev_err(...) ((void)0)
struct device { int unused; };
struct mutex { bool locked; };
struct lock_scope { struct mutex *m; bool once; };
static struct lock_scope lock_acquire(struct mutex *m)
{
    assert(!m->locked); m->locked = true;
    return (struct lock_scope){m, true};
}
static void lock_drop(struct lock_scope *s) { assert(s->m->locked); s->m->locked = false; }
#define guard(kind) struct lock_scope held __attribute__((cleanup(lock_drop))) = lock_acquire
#define scoped_guard(kind,m) for (guard(kind)(m); held.once; held.once = false)
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
static struct event events[64];
static unsigned int num_events, fail_at, sample, polls, ready_after;
static bool timeout, mutate_mux, external, write_on_error;
static unsigned int want_request, want_mask, want_output, mux_reg, mux_mask, mux_select;
static int record(struct regmap *map, unsigned int op, unsigned int reg,
                  unsigned int mask, unsigned int val)
{
    assert(map->lock->locked && num_events < ARRAY_SIZE(events));
    events[num_events++] = (struct event){op, reg, mask, val};
    return fail_at == num_events ? -EIO : 0;
}
static int regmap_read(struct regmap *map, unsigned int reg, unsigned int *val)
{
    int ret = record(map, 0, reg, 0, 0);
    if (ret) return ret;
    assert(reg < ARRAY_SIZE(map->regs));
    if (reg == want_output) {
        assert(map->regs[want_request] == want_mask);
        if (mux_mask) assert((map->regs[mux_reg] & mux_mask) == mux_select);
        *val = sample | ((!timeout && polls++ >= ready_after) ? BIT(15) : 0);
        /* Another owner updates an unrelated field while conversion runs. */
        if (mutate_mux) map->regs[mux_reg] ^= BIT(12);
    } else {
        assert(reg == mux_reg);
        *val = map->regs[reg];
    }
    return 0;
}
static int regmap_write(struct regmap *map, unsigned int reg, unsigned int val)
{
    int ret = record(map, 1, reg, 0xffff, val);
    assert(reg == want_request && (val == want_mask || val == 0));
    if (!ret) map->regs[reg] = val;
    return ret;
}
static int regmap_update_bits(struct regmap *map, unsigned int reg,
                              unsigned int mask, unsigned int val)
{
    int ret = record(map, 2, reg, mask, val & mask);
    assert((reg == mux_reg && (mask == mux_mask || (external && mask == 0x18))) ||
           (reg == 0xf90 && mask == 9));
    /* Model an ambiguous bus error after a write reached the PMIC. */
    if (!ret || write_on_error)
        map->regs[reg] = (map->regs[reg] & ~mask) | (val & mask);
    return ret;
}
#define regmap_set_bits(m,r,b) regmap_update_bits(m,r,b,b)
#define regmap_clear_bits(m,r,b) regmap_update_bits(m,r,b,0)
#define regmap_read_poll_timeout(m,r,v,cond,delay_us,timeout_us) ({ \
    int rc = -ETIMEDOUT; \
    assert((delay_us) == 100 && (timeout_us) == 32000); \
    for (int poll = 0; poll < 4; poll++) { \
        rc = regmap_read(m,r,&v); if (rc || (cond)) break; rc = -ETIMEDOUT; \
    } rc; })
static void fsleep(unsigned int delay) { assert(delay > 0); }
'''
prelude += f'#include "{SRC}/include/dt-bindings/iio/adc/mediatek,mt6357-auxadc.h"\n'
body = s[s.index('#define AUXADC_AVG_TIME_US'):s.index('static const struct iio_chan_spec mt6358_auxadc_channels')]
# Compile the common production conversion and the MT6357-specific callback.
for name in ('mt6359_auxadc_reset', 'mt6359_auxadc_sample_adc_val',
             'mt6359_auxadc_read_adc', 'mt6357_auxadc_read_adc', 'mt6359_auxadc_read_raw'):
    if name != 'mt6357_auxadc_read_adc' or 'static int mt6357_auxadc_read_adc(' in s:
        body += function(name)
body += '''
/* Impedance conversion is separate and is not exercised by this harness. */
static int mt6358_read_imp(struct mt6359_auxadc *adc, const struct iio_chan_spec *c,
                          int *vbat, int *ibat) { return -EOPNOTSUPP; }
'''
start = s.index('static const struct mtk_pmic_auxadc_info mt6357_chip_info = {')
body += s[start:s.index('\n};', start)+3]+'\n#define chip mt6357_chip_info\n'
body += 'struct expected { int id; unsigned int req, mask, out, bits, ratio; };\n'
body += 'static const struct expected expected[] = {\n'
for name, req, mask, out, width, ratio in expected:
    body += f'{{MT6357_AUXADC_{name}, {req}, {mask}, {out}, {width}, {ratio}}},\n'
body += '};\n'

checks = r'''
static struct regmap map;
static struct mt6359_auxadc adc = {.regmap=&map, .chip_info=&chip};
static struct iio_dev dev = {.priv=&adc};
static const struct iio_chan_spec *channel(int id)
{
    for (unsigned int i = 0; i < ARRAY_SIZE(mt6357_auxadc_channels); i++)
        if (mt6357_auxadc_channels[i].channel == id) return &mt6357_auxadc_channels[i];
    return NULL;
}
static void setup(const struct expected *e, unsigned int initial)
{
    assert(!adc.lock.locked);
    memset(&map, 0, sizeof(map)); map.lock = &adc.lock;
    adc.timed_out = false;
    num_events=fail_at=polls=ready_after=0; timeout=mutate_mux=external=false;
    write_on_error=true;
    want_request=e->req; want_mask=e->mask; want_output=e->out;
    mux_reg=mux_mask=mux_select=0;
    if (e->id == MT6357_AUXADC_DCXO_TEMP) {
        mux_reg=0x1216; mux_mask=BIT(4); mux_select=BIT(4);
    } else if (e->id == MT6357_AUXADC_VBIF) {
        mux_reg=0x1236; mux_mask=BIT(1);
    }
    map.regs[mux_reg] = initial;
    sample = 123;
}
int main(void)
{
    unsigned int cases=0, errors=0;
    assert(channel(MT6357_AUXADC_VDCXO) == NULL);
    assert(channel(MT6357_AUXADC_VBAT)); /* Existing binding IDs stay stable. */
    assert(MT6357_AUXADC_VBAT == 12 && MT6357_AUXADC_VBIF == 13);
    for (unsigned int i=0; i<ARRAY_SIZE(expected); i++) {
        const struct expected *e = &expected[i];
        const struct iio_chan_spec *c = channel(e->id);
        assert(c && c->type == IIO_VOLTAGE && c->scan_type.realbits == (int)e->bits);
        const struct mtk_pmic_auxadc_chan *d = &chip.desc[c->scan_index];
        assert(d->ext_sel_idx == -1 && d->req_mask == e->mask);
        assert(chip.regs[d->req_idx] == e->req);
        assert(chip.regs[PMIC_AUXADC_ADC0] + (c->address << 1) == e->out);
        for (unsigned int seed=0; seed<2; seed++) {
            unsigned int initial = seed ? 0xffff : 0;
            for (unsigned int delay=0; delay<3; delay+=2) {
                unsigned int values[] = {0, 1, (1U << e->bits)/2, (1U << e->bits)-1};
                for (unsigned int n=0; n<ARRAY_SIZE(values); n++) {
                    setup(e, initial); ready_after=delay; sample=values[n];
                    int raw=-7, numerator=-7, denominator=-7;
                    assert(mt6359_auxadc_read_raw(&dev,c,&raw,NULL,IIO_CHAN_INFO_RAW) == IIO_VAL_INT);
                    assert(raw == (int)sample && map.regs[want_request] == 0);
                    assert(!adc.lock.locked && map.regs[mux_reg] == initial);
                    unsigned int before=num_events;
                    assert(mt6359_auxadc_read_raw(&dev,c,&numerator,&denominator,IIO_CHAN_INFO_SCALE) == IIO_VAL_FRACTIONAL);
                    assert(num_events == before && numerator == (int)e->ratio*1800);
                    assert(denominator == (1 << e->bits));
                    assert((long long)raw*numerator/denominator ==
                           (long long)sample*1800*e->ratio/(1 << e->bits));
                    assert(mt6359_auxadc_read_raw(&dev,c,&raw,NULL,IIO_CHAN_INFO_OFFSET) == -EINVAL);
                    assert(num_events == before && !adc.lock.locked);
                    cases++;
                }
            }
            setup(e, initial); timeout=true;
            int out=-7;
            assert(mt6359_auxadc_read_raw(&dev,c,&out,NULL,IIO_CHAN_INFO_RAW) == -ETIMEDOUT);
            assert(adc.timed_out && !adc.lock.locked && out == -7);
            assert(map.regs[mux_reg] == initial && map.regs[want_request] == 0);
            /* Recovery reset must execute under the same lock as sampling. */
            num_events=polls=0;
            assert(mt6359_auxadc_read_raw(&dev,c,&out,NULL,IIO_CHAN_INFO_RAW) == -ETIMEDOUT);
            assert(map.regs[0xf90] == 0 && !adc.lock.locked);
            assert(events[num_events-2].reg == 0xf90 && events[num_events-2].val == 9);
            assert(events[num_events-1].reg == 0xf90 && events[num_events-1].val == 0);
            timeout=false; num_events=polls=0;
            assert(mt6359_auxadc_read_raw(&dev,c,&out,NULL,IIO_CHAN_INFO_RAW) == IIO_VAL_INT);
            assert(!adc.timed_out && out == (int)sample);
            cases++;
            if (mux_mask) {
                setup(e, initial);
                assert(mt6359_auxadc_read_raw(&dev,c,&out,NULL,IIO_CHAN_INFO_RAW) == IIO_VAL_INT);
                unsigned int operations=num_events;
                for (unsigned int mode=0; mode<2; mode++)
                for (unsigned int fail=1; fail<=operations; fail++) {
                    setup(e, initial); fail_at=fail; out=-7;
                    write_on_error=mode;
                    int result=mt6359_auxadc_read_raw(&dev,c,&out,NULL,IIO_CHAN_INFO_RAW);
                    assert(result == -EIO);
                    assert(!adc.lock.locked);
                    assert(out == -7);
                    if (fail > 1) {
                        assert(events[num_events-1].op == 2 && events[num_events-1].reg == mux_reg);
                        if (fail != operations || write_on_error)
                            assert(map.regs[mux_reg] == initial);
                        else
                            assert(map.regs[mux_reg] == ((initial & ~mux_mask) | mux_select));
                    }
                    errors++;
                }
                setup(e, initial); mutate_mux=true;
                assert(mt6359_auxadc_read_raw(&dev,c,&out,NULL,IIO_CHAN_INFO_RAW) == IIO_VAL_INT);
                assert(map.regs[mux_reg] == (initial ^ BIT(12)));
                cases++;
            }
        }
    }
    /* Exercise the shared external-input cleanup without the MT6357 wrapper.
     * This is a controlled generic channel, not a complete MT6363 PMIC model. */
    struct mtk_pmic_auxadc_info generic = chip;
    struct mtk_pmic_auxadc_chan desc[PMIC_AUXADC_CHAN_MAX] = {0};
    u16 regs[PMIC_AUXADC_REGS_MAX] = {0};
    memcpy(desc,mt6357_auxadc_ch_desc,sizeof(mt6357_auxadc_ch_desc));
    memcpy(regs,mt6357_auxadc_regs,sizeof(mt6357_auxadc_regs));
    regs[PMIC_AUXADC_SDMADC_CON0]=0x1200;
    struct iio_chan_spec c = *channel(MT6357_AUXADC_BAT_TEMP);
    desc[c.scan_index].ext_sel_idx=PMIC_AUXADC_SDMADC_CON0;
    desc[c.scan_index].ext_sel_ch=2; desc[c.scan_index].ext_sel_pu=1;
    generic.desc=desc; generic.regs=regs; generic.read_adc=NULL;
    adc.chip_info=&generic;
    const struct expected e = {MT6357_AUXADC_BAT_TEMP,0x110e,8,0x108e,12,1};
    for (unsigned int fail=0; fail<=5; fail++) {
        setup(&e,0); external=true; mux_reg=0x1200; mux_mask=0x1f; mux_select=0x0a;
        map.regs[mux_reg]=0xa5a0; fail_at=fail;
        int out=-7;
        int result=mt6359_auxadc_read_raw(&dev,&c,&out,NULL,IIO_CHAN_INFO_RAW);
        assert(result == (fail ? -EIO : IIO_VAL_INT));
        assert(out == (fail ? -7 : (int)sample));
        assert(events[num_events-1].op == 2 && events[num_events-1].mask == 0x18);
        assert(map.regs[mux_reg] == 0xa5ba && !adc.lock.locked);
        cases++;
    }
    setup(&e,0); external=true; mux_reg=0x1200; mux_mask=0x1f; mux_select=0x0a;
    timeout=true; map.regs[mux_reg]=0xa5a0;
    int out=-7;
    assert(mt6359_auxadc_read_raw(&dev,&c,&out,NULL,IIO_CHAN_INFO_RAW) == -ETIMEDOUT);
    assert(out == -7 && map.regs[mux_reg] == 0xa5ba && !adc.lock.locked);
    /* The scale also handles a fractional input divider without truncation. */
    desc[c.scan_index].r_ratio=(struct u8_fract){2,3}; generic.vref_mV=1840;
    int numerator, denominator;
    assert(mt6359_auxadc_read_raw(&dev,&c,&numerator,&denominator,IIO_CHAN_INFO_SCALE) == IIO_VAL_FRACTIONAL);
    assert(numerator == 3680 && denominator == 3*4096);
    cases++;
    printf("PASS: %u ADC cases and %u injected mux-bus errors; 12 MT6357 mappings, voltage units, scale, mux restoration, external-input cleanup and locked timeout recovery\n",cases,errors);
    return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+body+checks)
subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                '-Wno-unused-parameter', '-Wno-unused-const-variable', '-Wno-unused-function',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                '-fno-pie', '-no-pie', str(path), '-o', str(OUT/'host')], check=True)
subprocess.run([str(OUT/'host')], check=True)
report = {'vendor_mappings': [dict(channel=n, request_reg=r, request_mask=m,
                                 output_reg=o, bits=b, ratio=ratio)
                              for n,r,m,o,b,ratio in expected],
          'hardware_tested': False,
          'scope': 'compiled production tables/conversion code; modeled regmap, locks, polling and bus failures; no battery temperature conversion or electrical validation'}
(ROOT/'out/mt6357-auxadc-audit.json').write_text(json.dumps(report, indent=2)+'\n')
print('No PMIC or device was accessed; charger and MT6357 ADC remain disabled in the diagnostic config.')
