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
reset_table = vendor.split('static const unsigned int mt6357_rst_setting[][3] = {', 1)[1].split('};', 1)[0]
reset_sequence = [(value(reg), int(mask, 0), int(val, 0)) for reg, mask, val in re.findall(
    r'(MT6357_\w+), (0x[0-9a-f]+), (0x[0-9a-f]+|0),', reset_table)]
assert len(reset_sequence) == 4

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
struct device { struct device *parent; };
struct platform_device { struct device dev; };
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
struct iio_dev {
    void *priv;
    const char *name;
    const void *info;
    int modes, num_channels;
    const struct iio_chan_spec *channels;
};
static void *iio_priv(struct iio_dev *dev) { return dev->priv; }
struct regmap { unsigned int regs[0x2000]; struct mutex *lock; };
struct event { unsigned int op, reg, mask, val; };
static struct event events[64];
static unsigned int num_events, fail_at, second_fail_at, sample, polls, ready_after;
static bool timeout, mutate_mux, external, write_on_error, probing;
static unsigned int want_request, want_mask, want_output, mux_reg, mux_mask, mux_select;
static int record(struct regmap *map, unsigned int op, unsigned int reg,
                  unsigned int mask, unsigned int val)
{
    assert((map->lock->locked || probing) && num_events < ARRAY_SIZE(events));
    events[num_events++] = (struct event){op, reg, mask, val};
    if (second_fail_at == num_events) return -ENXIO;
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
    assert((reg == want_request && (val == want_mask || val == 0)) ||
           (reg == 0xfb4 && (val == 0x6359 || val == 0)));
    if (!ret || write_on_error) map->regs[reg] = val;
    return ret;
}
static int regmap_update_bits(struct regmap *map, unsigned int reg,
                              unsigned int mask, unsigned int val)
{
    int ret = record(map, 2, reg, mask, val & mask);
    assert((reg == mux_reg && (mask == mux_mask || (external && mask == 0x18))) ||
           (reg == 0xf90 && mask == 9) ||
           (reg == 0x110e && mask == 0x80) || (reg == 0x111a && mask == 0x400));
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
for name in ('mt6357_auxadc_restore_requests', 'mt6359_auxadc_reset', 'mt6359_auxadc_sample_adc_val',
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
body += r'''
/* Probe runs before registration, so no user can race its initialization. */
#define INDIO_DIRECT_MODE 1
#define dev_err_probe(dev,err,...) (err)
static const int mt6359_auxadc_iio_info;
static struct regmap probe_map;
static struct mt6359_auxadc probe_adc;
static struct iio_dev probe_dev;
static unsigned int registrations;
static const void *device_get_match_data(struct device *dev) { return &chip; }
static struct regmap *dev_get_regmap(struct device *dev, const char *name) { return &probe_map; }
static void mutex_init(struct mutex *lock) { lock->locked=false; }
static struct iio_dev *devm_iio_device_alloc(struct device *dev, size_t size)
{
    assert(size == sizeof(probe_adc));
    memset(&probe_adc,0,sizeof(probe_adc));
    memset(&probe_dev,0,sizeof(probe_dev));
    probe_dev.priv=&probe_adc; probe_map.lock=&probe_adc.lock;
    return &probe_dev;
}
static int devm_iio_device_register(struct device *dev, struct iio_dev *iio)
{
    assert(!probe_adc.needs_reset);
    registrations++;
    return 0;
}
'''
body += function('mt6359_auxadc_probe')
body += 'static const struct event stock_reset[] = {\n'
for reg, mask, val in reset_sequence:
    body += f'{{2, {reg}, {mask}, {val}}},\n'
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
    adc.timed_out = adc.needs_reset = false;
    num_events=fail_at=second_fail_at=polls=ready_after=0; timeout=mutate_mux=external=false;
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
            assert(events[num_events-4].reg == 0xf90 && events[num_events-4].val == 9);
            assert(events[num_events-3].reg == 0xf90 && events[num_events-3].val == 0);
            assert(events[num_events-2].reg == 0x110e && events[num_events-2].val == 0x80);
            assert(events[num_events-1].reg == 0x111a && events[num_events-1].val == 0x400);
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

    /* Reset failure must not register usable conversions or publish a value.
     * A keyed generic PMIC exercises the common protection-key cleanup too. */
    unsigned int resets=0;
    const struct expected *battery = &expected[0];
    const struct iio_chan_spec *bat = channel(MT6357_AUXADC_BATADC);
    for (unsigned int keyed=0; keyed<2; keyed++) {
        generic=chip;
        memcpy(regs,mt6357_auxadc_regs,sizeof(mt6357_auxadc_regs));
        if (keyed) {
            generic.sec_unlock_key=0x6359;
            regs[PMIC_HK_TOP_WKEY]=0xfb4;
            generic.restore_requests=NULL;
        }
        generic.regs=regs; adc.chip_info=&generic;
        for (unsigned int mode=0; mode<2; mode++)
        for (unsigned int fail=0; fail<=4; fail++) {
            setup(battery,0); write_on_error=mode; fail_at=fail;
            map.regs[0xf90]=0xa500;
            map.regs[0x110e]=1; map.regs[0x111a]=2;
            adc.lock.locked=true;
            int result=mt6359_auxadc_reset(&adc);
            adc.lock.locked=false;
            assert(result == (fail ? -EIO : 0));
            assert(adc.needs_reset == (fail != 0));
            assert((map.regs[0xf90] & ~9U) == 0xa500);
            if (keyed) {
                assert(events[0].reg == 0xfb4 && events[0].val == 0x6359);
                assert(events[num_events-1].reg == 0xfb4 && events[num_events-1].val == 0);
                assert(num_events == (fail == 1 ? 2U : 4U));
                if (fail != 1) {
                    assert(events[1].reg == 0xf90 && events[1].val == 9);
                    assert(events[2].reg == 0xf90 && events[2].val == 0);
                }
                assert(map.regs[0x110e] == 1 && map.regs[0x111a] == 2);
            } else {
                assert(num_events == (fail == 1 || fail == 2 ? 2U : 4U));
                for (unsigned int j=0; j<num_events; j++)
                    assert(!memcmp(&events[j],&stock_reset[j],sizeof(events[j])));
                if (!fail) assert(map.regs[0x110e] == 0x81 && map.regs[0x111a] == 0x402);
            }
            if (fail) {
                /* Another failed reset must prevent even the request write. */
                num_events=0; fail_at=1;
                int raw=-7;
                assert(mt6359_auxadc_read_raw(&dev,bat,&raw,NULL,IIO_CHAN_INFO_RAW) == -EIO);
                assert(raw == -7 && adc.needs_reset && !adc.lock.locked);
                assert(num_events == 2 && events[0].op != 0 && events[1].op != 0);
                /* Once recovery succeeds, the same read may sample again. */
                num_events=fail_at=polls=0;
                assert(mt6359_auxadc_read_raw(&dev,bat,&raw,NULL,IIO_CHAN_INFO_RAW) == IIO_VAL_INT);
                assert(raw == (int)sample && !adc.needs_reset && !adc.lock.locked);
                assert(num_events == 7 && events[4].reg == want_request);
            }
            resets++;
        }
        /* Report the original error even when release/relock fails as well. */
        setup(battery,0); fail_at=1; second_fail_at=2;
        adc.lock.locked=true;
        assert(mt6359_auxadc_reset(&adc) == -EIO);
        adc.lock.locked=false;
        assert(adc.needs_reset && num_events == 2);
        resets++;
    }
    /* The actual repeated-timeout path must propagate a failed reset. */
    adc.chip_info=&chip;
    for (unsigned int fail=1; fail<=4; fail++) {
        setup(battery,0); timeout=true; adc.timed_out=true; fail_at=6+fail;
        int raw=-7;
        assert(mt6359_auxadc_read_raw(&dev,bat,&raw,NULL,IIO_CHAN_INFO_RAW) == -EIO);
        assert(raw == -7 && adc.needs_reset && !adc.lock.locked);
        resets++;
    }
    generic=chip; generic.no_reset=true; adc.chip_info=&generic;
    setup(battery,0); adc.lock.locked=true;
    assert(mt6359_auxadc_reset(&adc) == 0);
    adc.lock.locked=false;
    assert(num_events == 0 && !adc.needs_reset);
    resets++;
    struct device wrapper={0}, mfd={.parent=&wrapper};
    struct platform_device platform={.dev={.parent=&mfd}};
    for (unsigned int mode=0; mode<2; mode++)
    for (unsigned int fail=0; fail<=4; fail++) {
        setup(battery,0); write_on_error=mode; fail_at=fail;
        memset(&probe_map,0,sizeof(probe_map));
        registrations=0; probing=true;
        assert(mt6359_auxadc_probe(&platform) == (fail ? -EIO : 0));
        probing=false;
        assert(registrations == (fail ? 0U : 1U));
        if (!fail) {
            assert(probe_dev.channels == chip.channels && probe_dev.num_channels == chip.num_channels);
            assert(probe_map.regs[0x110e] == 0x80 && probe_map.regs[0x111a] == 0x400);
        }
        resets++;
    }
    printf("PASS: %u ADC cases and %u injected mux-bus errors; 12 MT6357 mappings, voltage units, scale, mux restoration, external-input cleanup and locked timeout recovery\n",cases,errors);
    printf("PASS: %u reset/probe scenarios; stock restart sequence, keyed cleanup, repeated failures, conversion gating, timeout errors and failed-probe registration refusal\n",resets);
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
          'stock_reset_sequence': reset_sequence,
          'scope': 'compiled production tables/conversion code; modeled regmap, locks, polling and bus failures; no battery temperature conversion or electrical validation'}
(ROOT/'out/mt6357-auxadc-audit.json').write_text(json.dumps(report, indent=2)+'\n')
print('No PMIC or device was accessed; charger and MT6357 ADC remain disabled in the diagnostic config.')
