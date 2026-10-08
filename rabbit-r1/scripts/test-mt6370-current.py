#!/usr/bin/env python3
"""Exercise MT6370 current transitions, bus faults and probe lifetime with models."""
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
OUT = ROOT/'out/mt6370-current'
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/power/supply/mt6370-charger.c')
args = parser.parse_args()
source = args.source.resolve()
if not source.is_relative_to(ROOT):
    raise SystemExit('Source must stay under /rabbitr1')
s = source.read_text()
lock = json.loads((SRC/'rabbit-r1/sources.lock.json').read_text())
base = 'src/kernel/drivers/misc/mediatek/pmic/mt6370/'
for name in ('mt6370_pmu_charger.c', 'inc/mt6370_pmu.h', 'inc/mt6370_pmu_charger.h'):
    data = (ROOT/base/name).read_bytes()
    pin = lock['ci_files'][base+name]
    assert len(data) == pin['bytes'] and hashlib.sha256(data).hexdigest() == pin['sha256']
vendor = (ROOT/base/'mt6370_pmu_charger.c').read_text()
headers = ''.join((ROOT/base/n).read_text() for n in ('inc/mt6370_pmu.h', 'inc/mt6370_pmu_charger.h'))
defines = dict(re.findall(r'^#define\s+(\w+)\s+([^\n]+)', headers, re.M))


def value(name):
    return int(defines[name].strip().strip('()'), 0)


def function(name, text=s):
    match = re.search(r'(?:static )?(?:inline )?[\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+2]+'\n'


# The independent oracle uses the active Rabbit driver, not the gm20 variant.
workaround = function('mt6370_ichg_workaround', vendor)
updates = re.findall(r'MT6370_PMU_REG_CHGHIDDENCTRL7, (0x\w+), (0x\w+)', workaround)
assert updates == [('0x60', '0x00'), ('0x60', '0x40')]
assert 'uA < 900000' in workaround and 'uA >= 900000' in workaround
setter = function('__mt6370_set_ichg', vendor)
assert 'uA = (uA < 500000) ? 500000 : uA;' in setter
assert 'chip_vid == RT5081_VENDOR_ID || chip_vid == MT6370_VENDOR_ID' in setter
assert setter.index('#if 0') < setter.index('/* Workaround to make IEOC accurate */')
keys = re.search(r'mt6370_val_en_hidden_mode\[\] = \{([^}]+)', vendor)[1]
keys = [int(v, 16) for v in re.findall(r'0x[0-9a-fA-F]+', keys)]
assert keys == [0x96, 0x69, 0xc3, 0x3c]
expected = f'''
#define STOCK_KEY_REG {0x100+value('MT6370_PMU_REG_HIDDENPASCODE1')}
#define STOCK_HIDDEN_REG {0x100+value('MT6370_PMU_REG_CHGHIDDENCTRL7')}
#define STOCK_CURRENT_REG {0x100+value('MT6370_PMU_REG_CHGCTRL7')}
#define STOCK_CURRENT_MIN {value('MT6370_ICHG_MIN')}
#define STOCK_CURRENT_STEP {value('MT6370_ICHG_STEP')}
#define STOCK_CURRENT_SHIFT {value('MT6370_SHIFT_ICHG')}
#define STOCK_CURRENT_MASK {value('MT6370_MASK_ICHG')}
static const unsigned char stock_keys[] = {{{','.join(map(str, keys))}}};
'''
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
typedef uint8_t u8;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define FIELD_GET(mask,v) (((v)&(mask)) >> __builtin_ctz(mask))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define dev_err(...) ((void)0)
#define dev_err_probe(d,e,...) (e)
#define GFP_KERNEL 0
#define IS_ERR(p) ((intptr_t)(p) < 0 && (intptr_t)(p) > -4096)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define ERR_PTR(e) ((void *)(intptr_t)(e))
struct device { struct device *parent; };
struct platform_device { struct device dev; void *data; };
struct mutex { pthread_mutex_t raw; bool initialized; unsigned int owner; };
struct regmap { int unused; };
struct reg_field { unsigned int reg, lsb, msb; };
#define REG_FIELD(r,l,h) {r,l,h}
struct regmap_field { unsigned int id; };
struct iio_channel { int unused; };
struct work_struct { bool initialized, pending; };
struct delayed_work { struct work_struct work; };
struct workqueue_struct { bool active; };
#define INIT_WORK(w,f) ((w)->initialized=true)
#define INIT_DELAYED_WORK(w,f) INIT_WORK(&(w)->work,f)
union power_supply_propval { int intval; };
enum power_supply_property {
    POWER_SUPPLY_PROP_ONLINE, POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,
    POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE, POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,
    POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT, POWER_SUPPLY_PROP_PRECHARGE_CURRENT,
    POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT,
};
struct power_supply { void *drvdata; };
static void *power_supply_get_drvdata(struct power_supply *psy) { return psy->drvdata; }
static _Thread_local unsigned int thread_id=1;
static void mutex_lock(struct mutex *m)
{
    assert(m->initialized); assert(!pthread_mutex_lock(&m->raw));
    assert(!m->owner); m->owner=thread_id;
}
static void mutex_unlock(struct mutex *m)
{ assert(m->owner == thread_id); m->owner=0; assert(!pthread_mutex_unlock(&m->raw)); }
static struct mutex *lock_guard(struct mutex *m) { mutex_lock(m); return m; }
static void unlock_guard(struct mutex **m) { mutex_unlock(*m); }
#define guard(type) struct mutex *held __attribute__((cleanup(unlock_guard))) = lock_guard
static bool queue_work(struct workqueue_struct *q, struct work_struct *w)
{ assert(q->active && w->initialized); bool old=w->pending; w->pending=true; return !old; }
'''
linear = (SRC/'lib/linear_ranges.c').read_text()
header = (SRC/'include/linux/linear_range.h').read_text()
body = header[header.index('struct linear_range {'):header.index('unsigned int linear_range_values_in_range')]
for name in ('linear_range_get_max_value', 'linear_range_get_value',
             'linear_range_get_selector_high', 'linear_range_get_selector_within'):
    body += function(name, linear)
body += s[s.index('#define MT6370_REG_'):s.index('enum mt6370_usb_status')]
attach_start = s.index('enum {\n\tMT6370_ATTACH_STAT_DETACH')
body += s[attach_start:s.index('};', attach_start)+2]+'\n'
body += s[s.index('struct mt6370_chg_field {'):s.index('static inline int mt6370_chg_field_get')]
body += r'''
static struct mt6370_priv priv;
static struct power_supply psy={.drvdata=&priv};
static struct regmap map;
static struct regmap_field fields[F_MAX];
static struct workqueue_struct wq;
static unsigned int regs[0x200], errors[32], nops, unlocks, closes, stops;
static bool gate, apply_failed_write, settings_done, published, irq_active;
static unsigned int partial_key;
struct bus_op { char type; unsigned int reg, value, thread; };
static struct bus_op trace[20000];
static int record(char type, unsigned int reg, unsigned int value)
{
    assert(reg < ARRAY_SIZE(regs));
    if (reg != 0x100) assert(priv.ichg_lock.owner == thread_id || !settings_done);
    assert(nops < ARRAY_SIZE(trace));
    trace[nops]=(struct bus_op){type,reg,value,thread_id};
    int error=nops < ARRAY_SIZE(errors) ? errors[nops] : 0;
    nops++; sched_yield();
    return -error;
}
static int regmap_read(struct regmap *m, unsigned int reg, unsigned int *val)
{
    int ret=record('r',reg,0);
    if (reg == STOCK_HIDDEN_REG) assert(gate);
    if (!ret) *val=regs[reg];
    return ret;
}
static int regmap_write(struct regmap *m, unsigned int reg, unsigned int val)
{
    assert(val <= 255);
    int ret=record('w',reg,val);
    if (reg == STOCK_KEY_REG) { assert(val == 0); closes++; }
    if (reg == STOCK_HIDDEN_REG) assert(gate);
    if (reg == STOCK_CURRENT_REG) assert(!gate);
    if (reg == 0x112 && !(val&1)) stops++;
    if (!ret || apply_failed_write) {
        regs[reg]=val;
        if (reg == STOCK_KEY_REG) gate=false;
    }
    return ret;
}
static int regmap_bulk_write(struct regmap *m, unsigned int reg, const void *buf, size_t n)
{
    assert(reg == STOCK_KEY_REG && n == sizeof(stock_keys));
    assert(!gate && !memcmp(buf,stock_keys,n));
    int ret=record('b',reg,n);
    unsigned int accepted=ret ? partial_key : n;
    if (accepted == n) gate=true;
    unlocks++;
    return ret;
}
static int regmap_update_bits(struct regmap *m, unsigned int reg, unsigned int mask, unsigned int val)
{
    unsigned int old;
    int ret=regmap_read(m,reg,&old);
    if (ret) return ret;
    unsigned int next=(old&~mask)|(val&mask);
    return next == old ? 0 : regmap_write(m,reg,next);
}
static int regmap_field_write(struct regmap_field *f, unsigned int val)
{
    const struct reg_field *r=&mt6370_chg_fields[f->id].field;
    assert(val <= GENMASK(r->msb-r->lsb,0));
    return regmap_update_bits(&map,r->reg,GENMASK(r->msb,r->lsb),val << r->lsb);
}
'''
for name in ('mt6370_chg_field_set', 'mt6370_chg_init_ichg', 'mt6370_chg_set_ichg',
             'mt6370_chg_set_online', 'mt6370_chg_set_property', 'mt6370_chg_init_setting'):
    body += function(name)
body += r'''
static unsigned int stage, fail_stage, resources[16], nr_resources;
static unsigned int publication_calls, publication_io_start, drains;
enum { RES_MEMORY, RES_MUTEX_ATTACH, RES_MUTEX_CURRENT, RES_QUEUE, RES_OTG, RES_PSY, RES_CANCEL, RES_IRQ };
static int step(void) { return ++stage == fail_stage ? -ENOMEM : 0; }
static void add(unsigned int kind) { assert(nr_resources < ARRAY_SIZE(resources)); resources[nr_resources++]=kind; }
static void init_mutex(struct mutex *m)
{ assert(!m->initialized); assert(!pthread_mutex_init(&m->raw,NULL)); m->initialized=true; }
static void destroy_mutex(struct mutex *m)
{ assert(m->initialized && !m->owner); assert(!pthread_mutex_destroy(&m->raw)); m->initialized=false; }
static void *devm_kzalloc(struct device *dev, size_t size, int flags)
{ if (step()) return NULL; assert(size == sizeof(priv)); memset(&priv,0,sizeof(priv)); add(RES_MEMORY); return &priv; }
static struct regmap *dev_get_regmap(struct device *dev, void *unused)
{ return step() ? NULL : &map; }
static int mt6370_chg_init_rmap_fields(struct mt6370_priv *p)
{
    int ret=step(); if (ret) return ret;
    for (unsigned int i=0; i<F_MAX; i++) { fields[i].id=i; p->rmap_fields[i]=&fields[i]; }
    return 0;
}
static void platform_set_drvdata(struct platform_device *p, void *data) { p->data=data; }
static struct iio_channel channel;
static struct iio_channel *devm_iio_channel_get_all(struct device *dev)
{ return step() ? ERR_PTR(-ENOMEM) : &channel; }
static struct iio_channel *mt6370_chg_find_ibus(struct iio_channel *c)
{ return step() ? NULL : c; }
static int devm_mutex_init(struct device *d, struct mutex *m)
{
    int ret=step(); if (ret) return ret; init_mutex(m);
    add(m == &priv.ichg_lock ? RES_MUTEX_CURRENT : RES_MUTEX_ATTACH); return 0;
}
static const char *dev_name(struct device *d) { return "mt6370"; }
static struct workqueue_struct *devm_alloc_ordered_workqueue(struct device *d, const char *fmt, int flags, const char *name)
{ if (step()) return NULL; wq.active=true; add(RES_QUEUE); return &wq; }
static int set_current(int ua)
{
    union power_supply_propval val={.intval=ua};
    return mt6370_chg_set_property(&psy,POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,&val);
}
static int mt6370_chg_init_otg_regulator(struct mt6370_priv *p)
{
    assert(p->ichg_lock.initialized && p->attach_lock.initialized);
    assert(!gate && (regs[STOCK_CURRENT_REG]&STOCK_CURRENT_MASK) == (8U << STOCK_CURRENT_SHIFT));
    settings_done=true;
    int ret=step(); if (!ret) add(RES_OTG); return ret;
}
static int mt6370_chg_init_psy(struct mt6370_priv *p)
{
    int ret=step(); if (ret) return ret;
    assert(settings_done && !published && p->wq->active);
    assert(p->bc12_work.initialized && p->mivr_dwork.work.initialized);
    p->psy=&psy; published=true; add(RES_PSY);
    publication_io_start=nops;
    /* Emulate userspace calling both setters as soon as registration exposes them. */
    assert(set_current(500000) == (p->ichg_workaround ? 0 : -ERANGE));
    union power_supply_propval val={.intval=1};
    assert(!mt6370_chg_set_property(&psy,POWER_SUPPLY_PROP_ONLINE,&val));
    assert(p->bc12_work.pending);
    publication_calls++; return 0;
}
static void cancel_delayed_work_sync(struct delayed_work *w)
{ assert(published && wq.active && w->work.initialized); w->work.pending=false; drains++; }
static void cancel_work_sync(struct work_struct *w)
{ assert(published && wq.active && w->initialized); w->pending=false; drains++; }
static void mt6370_chg_cancel_work(void *data);
static int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *arg)
{
    assert(fn == mt6370_chg_cancel_work && arg == &priv);
    int ret=step(); if (ret) fn(arg); else add(RES_CANCEL); return ret;
}
static int mt6370_chg_init_irq(struct mt6370_priv *p)
{ int ret=step(); if (!ret) { add(RES_IRQ); irq_active=true; } return ret; }
static void mt6370_chg_pwr_rdy_check(struct mt6370_priv *p)
{ assert(irq_active && published); }
'''
body += function('mt6370_chg_cancel_work')
body += function('mt6370_chg_probe')
checks = r'''
static void reset_bus(unsigned int vendor, bool open, unsigned int current, unsigned int protection)
{
    nops=unlocks=closes=stops=0; memset(errors,0,sizeof(errors));
    apply_failed_write=false; partial_key=0;
    for (unsigned int i=0; i<ARRAY_SIZE(regs); i++) regs[i]=0xa5;
    regs[0x100]=vendor; gate=open;
    regs[STOCK_CURRENT_REG]=(current/100000-1)*4+3;
    regs[STOCK_HIDDEN_REG]=(regs[STOCK_HIDDEN_REG]&~0x60)|protection;
    regs[0x112]|=1;
}
static void setup(unsigned int vendor, bool open, unsigned int current, unsigned int protection)
{
    assert(!priv.ichg_lock.owner);
    reset_bus(vendor,open,current,protection);
    assert(!mt6370_chg_init_ichg(&priv)); nops=0;
    settings_done=true;
}
static unsigned int current_value(void)
{ return STOCK_CURRENT_MIN+((regs[STOCK_CURRENT_REG]&STOCK_CURRENT_MASK) >> STOCK_CURRENT_SHIFT)*STOCK_CURRENT_STEP; }
static void check_success(int ua, bool workaround, unsigned int old_hidden, unsigned int old_chgen)
{
    assert(!priv.ichg_lock.owner && !gate);
    assert(current_value() == (unsigned int)ua/100000*100000);
    assert((regs[STOCK_CURRENT_REG]&~STOCK_CURRENT_MASK) == 3);
    assert(regs[0x112] == old_chgen); /* Never enable charging, including after a failed transition. */
    unsigned int target=ua < 900000 ? 0 : 0x40;
    assert(regs[STOCK_HIDDEN_REG] == (workaround ? ((old_hidden&~0x60)|target) : old_hidden));
    assert(unlocks == (unsigned int)workaround && closes == (workaround ? 2U : 0U));
    assert(stops == 0);
    unsigned int at=0;
    if (workaround) {
        assert(trace[at].type == 'w' && trace[at++].reg == STOCK_KEY_REG);
        assert(trace[at].type == 'b' && trace[at++].reg == STOCK_KEY_REG);
        assert(trace[at].type == 'r' && trace[at++].reg == STOCK_HIDDEN_REG);
        if (trace[at].reg == STOCK_HIDDEN_REG) assert(trace[at++].type == 'w');
        assert(trace[at].type == 'w' && trace[at++].reg == STOCK_KEY_REG);
    }
    assert(trace[at].type == 'r' && trace[at++].reg == STOCK_CURRENT_REG);
    if (at<nops) assert(trace[at].type == 'w' && trace[at++].reg == STOCK_CURRENT_REG);
    assert(at == nops);
    for (unsigned int i=0; i<nops; i++) {
        unsigned int r=trace[i].reg;
        assert(r == STOCK_CURRENT_REG || (workaround && (r == STOCK_KEY_REG || r == STOCK_HIDDEN_REG)));
    }
}
static void release_probe(void)
{
    while (nr_resources) switch (resources[--nr_resources]) {
    case RES_IRQ: irq_active=false; break;
    case RES_CANCEL: assert(!irq_active); mt6370_chg_cancel_work(&priv); break;
    case RES_PSY:
        assert(published && !priv.bc12_work.pending && !priv.mivr_dwork.work.pending);
        published=false; break;
    case RES_QUEUE: assert(!published); wq.active=false; break;
    case RES_MUTEX_ATTACH: destroy_mutex(&priv.attach_lock); break;
    case RES_MUTEX_CURRENT: destroy_mutex(&priv.ichg_lock); break;
    default: break;
    }
    assert(!published && !irq_active && !wq.active);
}
static void *worker(void *id)
{
    thread_id=(uintptr_t)id;
    for (unsigned int i=0; i<100; i++) assert(!set_current((i+thread_id)%2 ? 500000 : 1000000));
    return NULL;
}
int main(void)
{
    unsigned int models=0, valid=0, failures=0, rejects=0, probes=0;
    priv.regmap=&map;
    init_mutex(&priv.ichg_lock); init_mutex(&priv.attach_lock);
    for (unsigned int i=0; i<F_MAX; i++) { fields[i].id=i; priv.rmap_fields[i]=&fields[i]; }
    for (unsigned int id=0; id<256; id++) {
        reset_bus(id,false,900000,0x40);
        priv.ichg_min=1; priv.ichg_workaround=true;
        unsigned int model=id >> 4;
        bool known=model == 8 || model == 14;
        bool other=model == 9 || model == 10 || model == 11 || model == 15;
        assert(mt6370_chg_init_ichg(&priv) == (known || other ? 0 : -ENODEV));
        assert(priv.ichg_workaround == known && priv.ichg_min == (known ? 500000U : 900000U));
        assert(nops == 1 && trace[0].type == 'r' && trace[0].reg == 0x100); models++;
    }
    reset_bus(0xe0,false,900000,0x40); errors[0]=EIO;
    assert(mt6370_chg_init_ichg(&priv) == -EIO && !priv.ichg_workaround && priv.ichg_min == 900000); models++;
    const unsigned int ids[]={0x80,0xe0,0x90,0xa0,0xb0,0xf0};
    for (unsigned int m=0; m<ARRAY_SIZE(ids); m++)
    for (unsigned int value=500000; value<=5000000; value+=100000)
    for (int offset=-1; offset<=1; offset++) {
        int ua=(int)value+offset;
        bool workaround=m<2;
        setup(ids[m],false,2000000,0x60);
        if (ua < (workaround ? 500000 : 900000) || ua > 5000000) {
            assert(set_current(ua) == -ERANGE && nops == 0); rejects++; continue;
        }
        unsigned int hidden=regs[STOCK_HIDDEN_REG], enable=regs[0x112];
        assert(!set_current(ua)); check_success(ua,workaround,hidden,enable); valid++;
    }
    const int bad[]={INT_MIN,-1,0,499999,5000001,INT_MAX};
    for (unsigned int i=0; i<ARRAY_SIZE(bad); i++) {
        setup(0xe0,true,2000000,0x40);
        assert(set_current(bad[i]) == (bad[i] < 0 ? -EINVAL : -ERANGE));
        assert(!nops && gate); rejects++;
    }
    for (unsigned int open=0; open<2; open++)
    for (unsigned int protect=0; protect<4; protect++)
    for (unsigned int target=500000; target<=1000000; target+=100000) {
        setup(0xe0,open,900000,protect << 5);
        unsigned int hidden=regs[STOCK_HIDDEN_REG], enable=regs[0x112];
        assert(!set_current(target)); check_success(target,true,hidden,enable); valid++;
    }
    /* Every operation in both threshold directions: no-effect, partial/full-effect errors. */
    const unsigned int target[]={500000,1000000};
    for (unsigned int t=0; t<2; t++) {
        unsigned int old=t ? 500000 : 1000000, protection=t ? 0 : 0x40;
        setup(0xe0,true,old,protection); assert(!set_current(target[t]));
        unsigned int count=nops; assert(count == 7);
        for (unsigned int failure=0; failure<count; failure++)
        for (unsigned int effects=0; effects<2; effects++)
        for (unsigned int accepted=0; accepted<=4; accepted++) {
            setup(0xe0,true,old,protection);
            errors[failure]=EIO; apply_failed_write=effects; partial_key=accepted;
            assert(set_current(target[t]) == -EIO && !priv.ichg_lock.owner);
            assert(!(regs[0x112]&1) && stops == 1);
            if (failure != 4 || effects) assert(!gate);
            if (failure < 5) assert(current_value() == old);
            /* Retry must repair stale protection and close a gate left open by the error. */
            nops=unlocks=closes=stops=0; memset(errors,0,sizeof(errors));
            unsigned int hidden=regs[STOCK_HIDDEN_REG], enable=regs[0x112];
            assert(!set_current(target[t])); check_success(target[t],true,hidden,enable); failures++;
        }
    }
    /* Fail each cleanup operation after each primary failure, including effective writes. */
    for (unsigned int first=0; first<7; first++) {
        setup(0xe0,true,1000000,0x40); errors[first]=EIO; partial_key=4;
        assert(set_current(500000) == -EIO);
        unsigned int count=nops;
        for (unsigned int second=first+1; second<count; second++)
        for (unsigned int effects=0; effects<2; effects++) {
            setup(0xe0,true,1000000,0x40);
            errors[first]=EIO; errors[second]=ETIMEDOUT; partial_key=4; apply_failed_write=effects;
            assert(set_current(500000) == -EIO && !priv.ichg_lock.owner);
            assert(closes == 2);
            for (unsigned int i=first+1; i<nops; i++)
                assert(trace[i].reg == STOCK_KEY_REG || trace[i].reg == 0x112);
            bool stop_failed=trace[second].reg == 0x112;
            bool stop_effective=trace[second].type == 'w' && effects;
            assert(!!(regs[0x112]&1) == (stop_failed && !stop_effective));
            /* Bus errors may leave charging active; a later successful request must not re-enable it. */
            nops=unlocks=closes=stops=0; memset(errors,0,sizeof(errors));
            unsigned int hidden=regs[STOCK_HIDDEN_REG], enable=regs[0x112];
            assert(!set_current(1000000)); check_success(1000000,true,hidden,enable); failures++;
        }
    }
    setup(0xe0,false,1000000,0x40);
    pthread_t threads[4];
    for (uintptr_t i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_create(&threads[i],NULL,worker,(void *)(i+2)));
    for (unsigned int i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_join(threads[i],NULL));
    assert(!gate && !priv.ichg_lock.owner && closes == 800 && unlocks == 400);
    /* Each completed update starts with close and ends with an ICHG read/write, without interleaving. */
    unsigned int transactions=0;
    for (unsigned int i=0; i<nops;) {
        unsigned int owner=trace[i].thread;
        assert(trace[i].type == 'w' && trace[i].reg == STOCK_KEY_REG); i++;
        while (i<nops && !(trace[i].type == 'w' && trace[i].reg == STOCK_KEY_REG && trace[i-1].reg == STOCK_CURRENT_REG)) {
            assert(trace[i].thread == owner); i++;
        }
        transactions++;
    }
    assert(transactions == 400);
    destroy_mutex(&priv.ichg_lock); destroy_mutex(&priv.attach_lock);
    struct platform_device pdev={0};
    settings_done=false; reset_bus(0xe0,true,500000,0);
    assert(!mt6370_chg_probe(&pdev)); unsigned int stages=stage;
    assert(publication_calls == 1 && published); release_probe(); probes++;
    for (unsigned int f=1; f<=stages; f++) {
        settings_done=false; stage=0; fail_stage=f; reset_bus(0xe0,true,500000,0);
        unsigned int previous=publication_calls;
        assert(mt6370_chg_probe(&pdev) < 0);
        assert(publication_calls <= previous+1); release_probe(); probes++;
    }
    fail_stage=0;
    /* DEV_INFO plus each bus operation during initial settings, before callback exposure. */
    settings_done=false; stage=0; reset_bus(0xe0,true,500000,0);
    assert(!mt6370_chg_probe(&pdev)); unsigned int init_ops=publication_io_start;
    release_probe();
    for (unsigned int f=0; f<init_ops; f++) {
        settings_done=false; stage=0; reset_bus(0xe0,true,500000,0); errors[f]=EIO;
        unsigned int previous=publication_calls;
        assert(mt6370_chg_probe(&pdev) == -EIO && publication_calls == previous);
        release_probe(); probes++;
    }
    for (unsigned int id=0; id<16; id++) {
        settings_done=false; stage=0; reset_bus(id << 4,false,500000,0);
        bool known=id == 8 || id == 9 || id == 10 || id == 11 || id == 14 || id == 15;
        unsigned int previous=publication_calls;
        assert(mt6370_chg_probe(&pdev) == (known ? 0 : -ENODEV));
        assert(publication_calls == previous+known);
        if (!known) assert(nops == 1);
        if (known && id != 8 && id != 14) assert(!unlocks && !closes);
        release_probe(); probes++;
    }
    printf("PASS: %u model cases, %u current transitions, %u rejected requests, %u fault/recovery cases, 400 threaded transitions and %u probe/unwind cases\n",models,valid,rejects,failures,probes);
    return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+expected+body+checks)
subprocess.run(['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
                '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-const-variable',
                '-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie',
                str(path),'-o',str(OUT/'host')],check=True)
subprocess.run([str(OUT/'host')],check=True,timeout=30)
(ROOT/'out/mt6370-current-audit.json').write_text(json.dumps(dict(
    hardware_tested=False, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    scope='production current helpers/setter/init/probe; Rabbit register/protocol oracle; modeled regmap, devres and workqueue with pthread locks; no electrical or battery-policy validation'
),indent=2)+'\n')
