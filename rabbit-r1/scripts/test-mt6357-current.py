#!/usr/bin/env python3
"""Check MT6357 current sensing against stock arithmetic and latch definitions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6357-current'
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/power/supply/mt6357-gauge.c')
args = parser.parse_args()
source = args.source.resolve()
if not source.is_relative_to(ROOT):
    raise SystemExit('Source must stay under /rabbitr1')
s = source.read_text()
lock = json.loads((SRC/'rabbit-r1/sources.lock.json').read_text())
for name in ('src/kernel/drivers/power/supply/mt6357-gauge.c',
             'src/kernel/drivers/power/supply/mtk_battery.c',
             'src/kernel/drivers/power/supply/Makefile',
             'src/kernel/include/linux/mfd/mt6357/registers.h'):
    data = (ROOT/name).read_bytes()
    assert len(data) == lock['ci_files'][name]['bytes']
    assert hashlib.sha256(data).hexdigest() == lock['ci_files'][name]['sha256']
vendor = (ROOT/'src/kernel/drivers/power/supply/mt6357-gauge.c').read_text()
headers = (ROOT/'src/kernel/include/linux/mfd/mt6357/registers.h').read_text().replace('\\\n', '')
defines = dict(re.findall(r'^#define\s+(\w+)\s+([^\n]+)', headers, re.M))


def value(name):
    text = defines[name].strip()
    try:
        return int(text, 0)
    except ValueError:
        return value(text)


def function(name, text):
    match = re.search(r'static [\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+2]+'\n'


def macro(name, text):
    match = re.search(r'^#define '+name+r'\(', text, re.M)
    assert match, name
    end = text.index('\n', match.end())
    while text[end-1] == '\\':
        end = text.index('\n', end+1)
    return text[match.start():end+1]


stock_defs = {}
for label, name in (('CTRL', 'MT6357_FG_SW_READ_PRE'), ('CLEAR', 'MT6357_FG_SW_CLEAR'),
                    ('READY', 'MT6357_FG_LATCHDATA_ST'), ('ON', 'MT6357_FG_ON'),
                    ('DIG_PD', 'MT6357_RG_FGADC_DIG_CK_PDN'),
                    ('ANA_PD', 'MT6357_RG_FGADC_ANA_CK_PDN'),
                    ('DATA', 'MT6357_FG_CURRENT_OUT')):
    stock_defs[label+'_REG'] = value(name+'_ADDR')
    stock_defs[label+'_MASK'] = value(name+'_MASK') << value(name+'_SHIFT')
assert stock_defs['CTRL_REG'] == stock_defs['CLEAR_REG'] == stock_defs['READY_REG']
assert stock_defs['DIG_PD_REG'] == stock_defs['ANA_PD_REG']
stock_defs = ''.join(f'#define STOCK_{name} {v}U\n' for name, v in stock_defs.items())
prelude = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
typedef int64_t ktime_t;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define FIELD_GET(mask,v) (((v)&(mask)) >> __builtin_ctz(mask))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define div_u64(n,d) ((u64)(n)/(d))
#define do_div(n,d) ((n)/=(d))
#define bm_debug(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
#define dev_err_probe(d,e,...) (e)
#define GFP_KERNEL 0
#define PTR_ERR_OR_ZERO(p) ((intptr_t)(p) < 0 ? (int)(intptr_t)(p) : 0)
#define ERR_PTR(e) ((void *)(intptr_t)(e))
struct device { struct device *parent; void *driver_data; struct regmap *regmap; };
struct mt6397_chip { struct regmap *regmap; };
struct platform_device { struct device dev; };
struct mutex { pthread_mutex_t raw; unsigned int owner; bool initialized; };
struct regmap { int unused; };
struct power_supply { void *drvdata; };
union power_supply_propval { int intval; };
enum power_supply_property { POWER_SUPPLY_PROP_CURRENT_NOW, POWER_SUPPLY_PROP_CAPACITY };
#define POWER_SUPPLY_TYPE_BATTERY 1
struct power_supply_desc {
    const char *name; int type;
    const enum power_supply_property *properties;
    size_t num_properties;
    int (*get_property)(struct power_supply *, enum power_supply_property, union power_supply_propval *);
};
struct power_supply_config { void *drv_data; void *fwnode; };
static void *power_supply_get_drvdata(struct power_supply *p) { return p->drvdata; }
static _Thread_local unsigned int thread_id=1;
static _Thread_local bool holding;
static void mutex_lock(struct mutex *m)
{ assert(m->initialized && !holding); assert(!pthread_mutex_lock(&m->raw)); assert(!m->owner); m->owner=thread_id; holding=true; }
static void mutex_unlock(struct mutex *m)
{ assert(m->owner == thread_id && holding); m->owner=0; holding=false; assert(!pthread_mutex_unlock(&m->raw)); }
static struct mutex *lock_guard(struct mutex *m) { mutex_lock(m); return m; }
static void unlock_guard(struct mutex **m) { mutex_unlock(*m); }
#define guard(kind) struct mutex *held __attribute__((cleanup(unlock_guard))) = lock_guard
static u64 now;
static ktime_t ktime_get(void) { return now; }
static ktime_t ktime_add_us(ktime_t t, u64 n) { return t+n; }
static int ktime_compare(ktime_t a, ktime_t b) { return (a>b)-(a<b); }
static void usleep_range(unsigned int lo, unsigned int hi) { assert(lo && lo<=hi); now+=hi; }
#define might_sleep_if(x) ((void)0)
#define barrier() asm volatile("" ::: "memory")
#define cpu_relax() sched_yield()
'''
prelude += f'\n#include "{SRC}/include/linux/mfd/mt6357/registers.h"\n'
for name in ('poll_timeout_us', 'read_poll_timeout'):
    prelude += macro(name, (SRC/'include/linux/iopoll.h').read_text())
prelude += macro('regmap_read_poll_timeout', (SRC/'include/linux/regmap.h').read_text())
body = s[s.index('#define MT6357_FG_ON'):s.index('static int mt6357_gauge_convert')]
model = r'''
static struct mt6357_gauge gauge;
static struct power_supply psy={.drvdata=&gauge};
static struct regmap map;
static struct mt6397_chip pmic_chip={.regmap=&map};
static struct device wrapper_dev={.regmap=&map};
static struct device pmic_dev={.parent=&wrapper_dev,.driver_data=&pmic_chip};
static unsigned int regs[0x1000], errors[20000], nops, data_reads;
static unsigned int ready_delay, clear_delay, live_sample;
static u64 latch_at, release_at;
static bool start_pending, stop_pending, stall_start, stall_stop, effect_on_error;
struct event { char op; unsigned int reg, value, thread; };
static struct event trace[20000];
static int record(char op, unsigned int reg, unsigned int val)
{
    assert(gauge.lock.owner == thread_id && nops<ARRAY_SIZE(trace));
    trace[nops]=(struct event){op,reg,val,thread_id};
    int ret=-(int)errors[nops++]; sched_yield(); return ret;
}
static int regmap_read(struct regmap *m, unsigned int reg, unsigned int *val)
{
    assert(m == &map);
    assert(reg == STOCK_ON_REG || reg == STOCK_DIG_PD_REG || reg == STOCK_CTRL_REG || reg == STOCK_DATA_REG);
    int ret=record('r',reg,0);
    if (reg == STOCK_CTRL_REG) {
        if (start_pending && !stall_start && now>=latch_at) {
            regs[reg]|=STOCK_READY_MASK; start_pending=false;
            regs[STOCK_DATA_REG]=live_sample;
        }
        if (stop_pending && !stall_stop && now>=release_at) {
            regs[reg]&=~STOCK_READY_MASK; stop_pending=false;
        }
    }
    if (reg == STOCK_DATA_REG) {
        assert((regs[STOCK_CTRL_REG]&(STOCK_READY_MASK|STOCK_CTRL_MASK|STOCK_CLEAR_MASK)) == (STOCK_READY_MASK|STOCK_CTRL_MASK));
        data_reads++;
    }
    *val=ret ? 0xfefefefe : regs[reg]; /* A failed read may scribble its destination. */
    return ret;
}
static int regmap_write(struct regmap *m, unsigned int reg, unsigned int val)
{
    assert(m == &map);
    assert(reg == STOCK_CTRL_REG);
    int ret=record('w',reg,val);
    if (!ret || effect_on_error) {
        unsigned int old=regs[reg];
        assert((old&~(STOCK_CTRL_MASK|STOCK_CLEAR_MASK|STOCK_READY_MASK)) == (val&~(STOCK_CTRL_MASK|STOCK_CLEAR_MASK|STOCK_READY_MASK)));
        regs[reg]=(val&~STOCK_READY_MASK)|(old&STOCK_READY_MASK);
        if (!(old&STOCK_CTRL_MASK) && (val&STOCK_CTRL_MASK)) {
            assert(!(val&STOCK_CLEAR_MASK)); start_pending=true; latch_at=now+ready_delay;
        }
        if ((val&STOCK_CLEAR_MASK) && !(val&STOCK_CTRL_MASK)) {
            start_pending=false; stop_pending=true; release_at=now+clear_delay;
            regs[STOCK_DATA_REG]=0xdead; /* The captured sample must survive release. */
        }
    }
    return ret;
}
static int regmap_update_bits(struct regmap *m, unsigned int reg, unsigned int mask, unsigned int val)
{
    assert(reg == STOCK_CTRL_REG && (mask == STOCK_CTRL_MASK || mask == STOCK_CLEAR_MASK));
    unsigned int old; int ret=regmap_read(m,reg,&old); if (ret) return ret;
    unsigned int next=(old&~mask)|(val&mask);
    return old == next ? 0 : regmap_write(m,reg,next);
}
static unsigned int property_shunt=10000, property_gain=1000, stage, fail_stage, registrations;
static bool gain_present, shunt_present=true, malformed_gain, fail_registration;
static int step(void) { return ++stage == fail_stage ? -ENOMEM : 0; }
static void *devm_kzalloc(struct device *d, size_t size, int flags)
{ if (step()) return NULL; assert(size == sizeof(gauge)); memset(&gauge,0,size); return &gauge; }
static void *dev_get_drvdata(struct device *d) { return step() ? NULL : d->driver_data; }
static struct regmap *dev_get_regmap(struct device *d, void *unused) { return d->regmap; }
static int device_property_read_u32(struct device *d, const char *name, u32 *out)
{
    int ret=step(); if (ret) return ret;
    if (!strcmp(name,"shunt-resistor-micro-ohms")) {
        if (!shunt_present) return -EINVAL;
        *out=property_shunt;
    } else {
        assert(!strcmp(name,"mediatek,current-gain-permille") && gain_present);
        if (malformed_gain) return -EOVERFLOW;
        *out=property_gain;
    }
    return 0;
}
static bool device_property_present(struct device *d, const char *name)
{ assert(!strcmp(name,"mediatek,current-gain-permille")); return gain_present; }
static int devm_mutex_init(struct device *d, struct mutex *m)
{
    int ret=step(); if (ret) return ret;
    assert(!m->initialized); assert(!pthread_mutex_init(&m->raw,NULL)); m->initialized=true; return 0;
}
static void *dev_fwnode(struct device *dev) { return dev; }
static struct power_supply *devm_power_supply_register(struct device *dev,
    const struct power_supply_desc *desc, const struct power_supply_config *config)
{
    assert(config->drv_data == &gauge && config->fwnode == dev);
    assert(!strcmp(desc->name,"mt6357-battery") && desc->type == POWER_SUPPLY_TYPE_BATTERY);
    assert(desc->num_properties == 1 && desc->properties[0] == POWER_SUPPLY_PROP_CURRENT_NOW);
    assert(gauge.lock.initialized && gauge.needs_release && gauge.shunt_uohms && gauge.gain_permille);
    assert(gauge.regmap == &map);
    int ret=step(); if (ret) return ERR_PTR(ret);
    if (fail_registration) return ERR_PTR(-EIO);
    union power_supply_propval val={.intval=123};
    /* The framework can invoke callbacks immediately, even before probe returns. */
    assert(desc->get_property(&psy,POWER_SUPPLY_PROP_CURRENT_NOW,&val) == -EAGAIN && val.intval == 123);
    registrations++; return &psy;
}
'''
body += model
body += s[s.index('static int mt6357_gauge_convert'):s.index('static const struct of_device_id')]
stock = r'''
struct mtk_gauge { struct regmap *regmap; struct { int r_fg_value, car_tune_value; } hw_status; };
static unsigned int stock_sample;
static void pre_gauge_update(struct mtk_gauge *g) { }
static void post_gauge_update(struct mtk_gauge *g) { }
static int stock_regmap_read(struct regmap *m, unsigned int reg, unsigned int *val)
{ assert(reg == STOCK_DATA_REG); *val=stock_sample; return 0; }
'''
for name in ('UNIT_FGCURRENT', 'DEFAULT_R_FG', 'PMIC_FG_CURRENT_OUT_ADDR',
             'PMIC_FG_CURRENT_OUT_MASK', 'PMIC_FG_CURRENT_OUT_SHIFT'):
    stock += re.search(r'^#define '+name+r'\s+[^\n]+', vendor, re.M)[0]+'\n'
stock += function('reg_to_current', vendor)
stock += function('instant_current', vendor).replace('regmap_read(', 'stock_regmap_read(')
checks = r'''
static void init_lock(void)
{ assert(!gauge.lock.initialized); assert(!pthread_mutex_init(&gauge.lock.raw,NULL)); gauge.lock.initialized=true; }
static void destroy_lock(void)
{ if (gauge.lock.initialized) { assert(!gauge.lock.owner); assert(!pthread_mutex_destroy(&gauge.lock.raw)); gauge.lock.initialized=false; } }
static void setup(bool inherited, unsigned int sample)
{
    assert(!gauge.lock.owner);
    memset(regs,0,sizeof(regs)); memset(errors,0,sizeof(errors)); nops=data_reads=0; now=0;
    regs[STOCK_ON_REG]=STOCK_ON_MASK;
    regs[STOCK_CTRL_REG]=0x5360|(inherited ? (STOCK_READY_MASK|STOCK_CTRL_MASK) : 0);
    regs[STOCK_DATA_REG]=sample;
    live_sample=sample;
    gauge.regmap=&map; gauge.shunt_uohms=10000; gauge.gain_permille=1000; gauge.needs_release=inherited;
    ready_delay=clear_delay=0; start_pending=stop_pending=stall_start=stall_stop=effect_on_error=false;
}
static int read_current(int *result)
{
    union power_supply_propval val={.intval=*result};
    int ret=mt6357_gauge_desc.get_property(&psy,POWER_SUPPLY_PROP_CURRENT_NOW,&val);
    *result=val.intval; assert(!holding); return ret;
}
static int expected(unsigned int raw)
{
    unsigned int low=raw&0xffff, magnitude=(low&0x8000) ? low^0xffff : low;
    unsigned int current=(uint64_t)magnitude*314331/100000*100;
    return low&0x8000 ? -(int)current : (int)current;
}
static void retry(void)
{
    memset(errors,0,sizeof(errors)); nops=data_reads=0; stall_start=stall_stop=false; effect_on_error=false;
    ready_delay=clear_delay=0;
    /* Release can invalidate the old register; a new request latches a new sample. */
    regs[STOCK_DATA_REG]=0xdead;
    int value=123; assert(!read_current(&value)); assert(!gauge.needs_release && !gauge.lock.owner && data_reads == 1);
    assert(value == expected(live_sample));
}
static void *worker(void *arg)
{
    thread_id=(uintptr_t)arg;
    for (unsigned int i=0; i<50; i++) { int value=123; assert(!read_current(&value)); }
    return NULL;
}
int main(void)
{
    unsigned int conversions=0, samples=0, failures=0, probes=0;
    init_lock(); setup(false,0);
    const unsigned int shunts[]={10000,5000,20000,100000};
    const unsigned int gains[]={1000,900,1100};
    struct mtk_gauge stock_gauge={.regmap=&map};
    for (unsigned int r=0; r<ARRAY_SIZE(shunts); r++)
    for (unsigned int g=0; g<ARRAY_SIZE(gains); g++) {
        gauge.shunt_uohms=shunts[r]; gauge.gain_permille=gains[g];
        stock_gauge.hw_status.r_fg_value=shunts[r]/100; stock_gauge.hw_status.car_tune_value=gains[g];
        for (stock_sample=0; stock_sample<65536; stock_sample++) {
            int got=123; assert(!mt6357_gauge_convert(&gauge,stock_sample|0xabcd0000,&got));
            assert(got == instant_current(&stock_gauge)*100); conversions++;
        }
    }
    gauge.shunt_uohms=1; gauge.gain_permille=UINT_MAX;
    int value=123; assert(mt6357_gauge_convert(&gauge,0x7fff,&value) == -ERANGE && value == 123);
    const unsigned int raw[]={0,1,32767,32768,65534,65535,0xabcd0001};
    for (unsigned int i=0; i<ARRAY_SIZE(raw); i++)
    for (unsigned int inherited=0; inherited<2; inherited++)
    for (unsigned int delay=0; delay<4; delay++) {
        setup(inherited,raw[i]); ready_delay=delay*100; clear_delay=(3-delay)*100;
        value=123; assert(!read_current(&value)); assert(value == expected(raw[i]) && data_reads == 1);
        assert(regs[STOCK_CTRL_REG] == 0x5360 && !gauge.needs_release);
        assert(now <= 900); samples++;
    }
    /* Clock-gated/off engines and all unsupported properties must not write the gauge. */
    for (unsigned int off=0; off<4; off++) {
        setup(true,123); if (!off) regs[STOCK_ON_REG]=0; else regs[STOCK_DIG_PD_REG]=off << 3;
        value=123; assert(read_current(&value) == -EAGAIN && value == 123 && gauge.needs_release);
        for (unsigned int i=0; i<nops; i++) assert(trace[i].op == 'r' && trace[i].reg != STOCK_DATA_REG);
        samples++;
    }
    setup(false,1); union power_supply_propval val={.intval=123};
    assert(mt6357_gauge_desc.get_property(&psy,POWER_SUPPLY_PROP_CAPACITY,&val) == -EINVAL && val.intval == 123 && !nops);
    for (unsigned int inherited=0; inherited<2; inherited++) {
        setup(inherited,1); assert(!read_current(&value)); unsigned int count=nops;
        for (unsigned int failed=0; failed<count; failed++)
        for (unsigned int effect=0; effect<2; effect++) {
            setup(inherited,1); errors[failed]=EIO; effect_on_error=effect;
            value=123; assert(read_current(&value) == -EIO && value == 123);
            assert(!gauge.lock.owner && now <= 40200);
            bool dirty=gauge.needs_release;
            unsigned int completed=nops;
            /* Every later cleanup bus operation is also allowed to fail. */
            for (unsigned int second=failed+1; second<completed; second++) {
                setup(inherited,1); errors[failed]=EIO; errors[second]=ENXIO; effect_on_error=effect;
                value=123; assert(read_current(&value) == -EIO && value == 123 && !gauge.lock.owner);
                retry(); failures++;
            }
            setup(inherited,1); errors[failed]=EIO; effect_on_error=effect;
            value=123; assert(read_current(&value) == -EIO && gauge.needs_release == dirty);
            retry(); failures++;
        }
    }
    /* A stuck latch times out against the actual kernel polling macro's clock. */
    for (unsigned int which=0; which<3; which++) {
        setup(which == 2,1); stall_start=which == 0; stall_stop=which != 0;
        value=123; assert(read_current(&value) == -ETIMEDOUT && value == 123);
        assert(now >= 20000 && now <= 20200);
        assert(gauge.needs_release == (which != 0));
        retry(); failures++;
    }
    for (unsigned int clear=0; clear<2; clear++)
    for (unsigned int delay=19900; delay<=20200; delay+=100) {
        setup(false,1);
        if (clear) clear_delay=delay; else ready_delay=delay;
        value=123;
        int ret=read_current(&value);
        /* The polling helper evaluates one last read after its deadline. */
        assert(ret == (delay <= 20100 ? 0 : -ETIMEDOUT));
        assert(value == (ret ? 123 : expected(1)));
        assert(now <= 20100 && gauge.needs_release == (ret && clear));
        retry(); failures++;
    }
    /* Repeated failed recovery never reaches a new request or the sample register. */
    setup(true,1); stall_stop=true;
    for (unsigned int i=0; i<3; i++) {
        value=123; assert(read_current(&value) == -ETIMEDOUT && value == 123 && gauge.needs_release);
        assert(!data_reads); failures++;
    }
    retry();
    setup(true,1); pthread_t threads[4];
    for (uintptr_t i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_create(&threads[i],NULL,worker,(void *)(i+2)));
    for (unsigned int i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_join(threads[i],NULL));
    assert(!gauge.lock.owner && !gauge.needs_release && data_reads == 200);
    unsigned int transactions=0;
    for (unsigned int i=0; i<nops;) {
        assert(trace[i].op == 'r' && trace[i].reg == STOCK_ON_REG);
        unsigned int owner=trace[i++].thread;
        while (i<nops && trace[i].reg != STOCK_ON_REG) { assert(trace[i].thread == owner); i++; }
        transactions++;
    }
    assert(transactions == 200); destroy_lock();
    struct platform_device pdev={.dev={.parent=&pmic_dev}};
    setup(false,0); regs[STOCK_ON_REG]=0; stage=0; gain_present=true;
    assert(!mt6357_gauge_probe(&pdev)); unsigned int stages=stage; destroy_lock(); probes++;
    for (unsigned int f=1; f<=stages; f++) {
        setup(false,0); regs[STOCK_ON_REG]=0; stage=0; fail_stage=f;
        unsigned int before=registrations; assert(mt6357_gauge_probe(&pdev) < 0 && before == registrations);
        destroy_lock(); probes++;
    }
    fail_stage=0;
    setup(false,0); stage=0; pmic_chip.regmap=NULL;
    unsigned int before=registrations;
    assert(mt6357_gauge_probe(&pdev) == -ENODEV && registrations == before && !nops);
    pmic_chip.regmap=&map; probes++;
    for (unsigned int i=0; i<7; i++) {
        setup(false,0); regs[STOCK_ON_REG]=0; stage=0;
        shunt_present=i != 0; property_shunt=i == 1 ? 0 : i == 4 ? 1 : i == 5 ? UINT_MAX : 10000;
        gain_present=true; property_gain=i == 2 ? 0 : i == 4 ? UINT_MAX : i == 5 ? 1 : 1000;
        malformed_gain=i == 3; fail_registration=i == 6;
        unsigned int before=registrations;
        assert(mt6357_gauge_probe(&pdev) < 0 && before == registrations && !nops);
        destroy_lock(); probes++;
    }
    setup(false,0); regs[STOCK_ON_REG]=0; stage=0; property_shunt=10000;
    shunt_present=true; malformed_gain=fail_registration=gain_present=false;
    assert(!mt6357_gauge_probe(&pdev) && gauge.gain_permille == 1000); destroy_lock(); probes++;
    printf("PASS: %u stock conversion comparisons, %u sample/readiness cases, %u fault/recovery/timeout cases, 200 threaded reads and %u probe cases\n",conversions,samples,failures,probes);
    return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+stock_defs+body+stock+checks)
subprocess.run(['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
                '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-const-variable',
                '-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie',
                str(path),'-o',str(OUT/'host')],check=True)
subprocess.run([str(OUT/'host')],check=True,timeout=30)
(ROOT/'out/mt6357-current-audit.json').write_text(json.dumps(dict(
    hardware_tested=False, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    scope='production conversion/read/release/property/probe with stock conversion oracle and actual kernel polling macros; modeled bus, time and registration, pthread locks; no electrical or fuel-capacity validation'
),indent=2)+'\n')
