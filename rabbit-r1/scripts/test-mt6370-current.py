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
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define FIELD_GET(mask,v) (((v)&(mask)) >> __builtin_ctz(mask))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define dev_err(...) ((void)0)
#define dev_err_probe(d,e,...) (e)
#define GFP_KERNEL 0
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define IS_ERR(p) ((intptr_t)(p) < 0 && (intptr_t)(p) > -4096)
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define ERR_PTR(e) ((void *)(intptr_t)(e))
struct device { struct device *parent; void *data; };
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
    POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT, POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,
    POWER_SUPPLY_PROP_STATUS, POWER_SUPPLY_PROP_CHARGE_TYPE, POWER_SUPPLY_PROP_USB_TYPE,
    POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT_MAX, POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE_MAX,
};
enum { POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO, POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE,
    POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE_AWAKE, POWER_SUPPLY_CHARGE_BEHAVIOUR_FORCE_DISCHARGE };
enum { POWER_SUPPLY_TYPE_USB, POWER_SUPPLY_USB_TYPE_SDP, POWER_SUPPLY_USB_TYPE_CDP,
    POWER_SUPPLY_USB_TYPE_DCP, POWER_SUPPLY_USB_TYPE_UNKNOWN };
struct power_supply { void *drvdata; };
struct power_supply_desc {
    const char *name; int type; unsigned int charge_behaviours, usb_types;
    const enum power_supply_property *properties; size_t num_properties;
    int (*get_property)(struct power_supply *, enum power_supply_property, union power_supply_propval *);
    int (*set_property)(struct power_supply *, enum power_supply_property, const union power_supply_propval *);
    int (*property_is_writeable)(struct power_supply *, enum power_supply_property);
};
struct power_supply_config { void *drv_data, *fwnode; };
static unsigned int notifications;
static void power_supply_changed(struct power_supply *supply);
static void *dev_fwnode(struct device *dev) { return dev; }
#define PTR_ERR_OR_ZERO(p) (IS_ERR(p) ? PTR_ERR(p) : 0)
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
#define lockdep_assert_held(m) assert((m)->owner == thread_id)
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
usb_start = s.index('enum mt6370_usb_status {')
body += s[usb_start:s.index('};', usb_start)+2]+'\n'
attach_start = s.index('enum {\n\tMT6370_ATTACH_STAT_DETACH')
body += s[attach_start:s.index('};', attach_start)+2]+'\n'
body += s[s.index('struct mt6370_chg_field {'):s.index('static inline int mt6370_chg_field_get')]
body += r'''
static struct mt6370_priv priv;
static struct power_supply psy={.drvdata=&priv};
static void power_supply_changed(struct power_supply *supply)
{ assert(supply == &psy); __atomic_add_fetch(&notifications,1,__ATOMIC_RELAXED); }
static struct regmap map;
static struct regmap_field fields[F_MAX];
static struct workqueue_struct wq;
static unsigned int regs[0x200], errors[256], nops, unlocks, closes, stops;
static bool gate, apply_failed_write, settings_done, published, irq_active;
static unsigned int partial_key, slept_us;
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
    *val=ret ? UINT_MAX : regs[reg];
    return ret;
}
static int regmap_write(struct regmap *m, unsigned int reg, unsigned int val)
{
    assert(val <= 255);
    int ret=record('w',reg,val);
    if (reg == STOCK_KEY_REG) { assert(val == 0); closes++; }
    if (reg == STOCK_HIDDEN_REG) assert(gate);
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
static void usleep_range(unsigned int lo, unsigned int hi)
{
    assert(lo && hi == lo+1000 && priv.ichg_lock.owner == thread_id);
    record('d',0,lo); slept_us+=lo;
}
static int regmap_field_read(struct regmap_field *f, unsigned int *val)
{
    const struct reg_field *r=&mt6370_chg_fields[f->id].field;
    unsigned int reg; int ret=regmap_read(&map,r->reg,&reg);
    *val=ret ? UINT_MAX : (reg >> r->lsb)&GENMASK(r->msb-r->lsb,0);
    return ret;
}
static int regmap_field_write(struct regmap_field *f, unsigned int val)
{
    const struct reg_field *r=&mt6370_chg_fields[f->id].field;
    assert(val <= GENMASK(r->msb-r->lsb,0));
    return regmap_update_bits(&map,r->reg,GENMASK(r->msb,r->lsb),val << r->lsb);
}
'''
for name in ('mt6370_chg_field_get', 'mt6370_chg_field_set', 'mt6370_chg_init_ichg',
             'mt6370_chg_stop', 'mt6370_chg_program_ichg', 'mt6370_chg_set_ichg',
             'mt6370_chg_set_behaviour', 'mt6370_chg_get_behaviour',
             'mt6370_chg_set_online', 'mt6370_chg_set_property', 'mt6370_chg_init_setting',
             'mt6370_chg_bc12_work_func'):
    body += function(name)
body += r'''
static unsigned int stage, fail_stage, resources[16], nr_resources;
static unsigned int publication_calls, publication_io_start, drains;
enum { RES_MEMORY, RES_MUTEX_ATTACH, RES_MUTEX_CURRENT, RES_QUEUE, RES_INHIBIT, RES_OTG, RES_PSY, RES_CANCEL, RES_IRQ };
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
static struct power_supply *devm_power_supply_register(struct device *dev,
    const struct power_supply_desc *desc, const struct power_supply_config *cfg)
{
    struct mt6370_priv *p=cfg->drv_data;
    assert(p == &priv && cfg->fwnode == dev && desc == &p->psy_desc);
    assert(!strcmp(desc->name,"mt6370-charger") && desc->type == POWER_SUPPLY_TYPE_USB);
    assert(desc->num_properties == (p->ichg_workaround ? 13U : 12U));
    assert(desc->charge_behaviours == (p->ichg_workaround ? 3U : 0U));
    bool found=false;
    for (unsigned int i=0; i<desc->num_properties; i++) found |= desc->properties[i] == POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR;
    assert(found == p->ichg_workaround);
    assert(!!desc->property_is_writeable(&psy,POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR) == p->ichg_workaround);
    int ret=step(); if (ret) return ERR_PTR(ret);
    assert(settings_done && !published && p->wq->active);
    assert(p->bc12_work.initialized && p->mivr_dwork.work.initialized);
    assert(p->ichg_valid);
    if (p->ichg_workaround) assert(!(regs[0x112]&1));
    published=true; add(RES_PSY);
    publication_io_start=nops;
    assert(!p->psy);
    unsigned int before=notifications;
    mt6370_chg_bc12_work_func(&p->bc12_work);
    assert(notifications == before); /* No NULL-handle notification before registration returns. */
    /* Emulate userspace calling both setters as soon as registration exposes them. */
    assert(set_current(500000) == (p->ichg_workaround ? 0 : -ERANGE));
    union power_supply_propval val={.intval=1};
    assert(!mt6370_chg_set_property(&psy,POWER_SUPPLY_PROP_ONLINE,&val));
    assert(p->bc12_work.pending);
    if (p->ichg_workaround) {
        val.intval=POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO;
        assert(!desc->set_property(&psy,POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,&val));
        assert(!desc->get_property(&psy,POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,&val));
        assert(val.intval == POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO && (regs[0x112]&1));
    }
    publication_calls++; return &psy;
}
static void cancel_delayed_work_sync(struct delayed_work *w)
{ assert(published && wq.active && w->work.initialized); w->work.pending=false; drains++; }
static void cancel_work_sync(struct work_struct *w)
{ assert(published && wq.active && w->initialized); w->pending=false; drains++; }
static void mt6370_chg_cancel_work(void *data);
static void mt6370_chg_inhibit(void *data);
static int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *arg)
{
    assert((fn == mt6370_chg_cancel_work || fn == mt6370_chg_inhibit) && arg == &priv);
    int ret=step(); if (ret) fn(arg); else add(fn == mt6370_chg_cancel_work ? RES_CANCEL : RES_INHIBIT); return ret;
}
static int mt6370_chg_init_irq(struct mt6370_priv *p)
{ int ret=step(); if (!ret) { add(RES_IRQ); irq_active=true; } return ret; }
static void mt6370_chg_pwr_rdy_check(struct mt6370_priv *p)
{ assert(irq_active && published); }
'''
body += r'''
static int mt6370_chg_get_online(struct mt6370_priv *p, union power_supply_propval *v) { return -EOPNOTSUPP; }
static int mt6370_chg_get_status(struct mt6370_priv *p, union power_supply_propval *v) { return -EOPNOTSUPP; }
static int mt6370_chg_get_charge_type(struct mt6370_priv *p, union power_supply_propval *v) { return -EOPNOTSUPP; }
'''
body += function('mt6370_chg_get_property')
body += function('mt6370_chg_property_is_writeable')
body += s[s.index('static enum power_supply_property mt6370_chg_properties'):s.index('static const struct regulator_ops')]
body += function('mt6370_chg_init_psy')
body += function('mt6370_chg_cancel_work')
body += function('mt6370_chg_inhibit')
body += function('mt6370_chg_probe')
# Compile the stock stop routine as an independent write/delay oracle. Its
# cache is initialized from each test's hardware setting, not the new driver's
# request cache. Error recovery intentionally differs from the stock routine.
body += r'''
struct mt6370_pmu_chip { int unused; };
struct charger_device { struct device dev; };
struct mt6370_pmu_charger_data {
    struct device *dev; struct mt6370_pmu_chip *chip;
    struct mutex ichg_access_lock; unsigned int ichg, ichg_dis_chg;
};
struct stock_event { char op; unsigned int reg, mask, value; };
static struct stock_event stock_trace[4];
static unsigned int stock_count;
static void *dev_get_drvdata(struct device *dev) { return dev->data; }
#define mt_dbg(...) ((void)0)
#define dev_notice(...) ((void)0)
static int mt6370_pmu_reg_update_bits(struct mt6370_pmu_chip *c, unsigned int reg,
    unsigned int mask, unsigned int value)
{
    assert(stock_count<ARRAY_SIZE(stock_trace));
    stock_trace[stock_count++]=(struct stock_event){'w',reg+0x100,mask,value}; return 0;
}
static int mt6370_pmu_reg_clr_bit(struct mt6370_pmu_chip *c, unsigned int reg, unsigned int mask)
{ return mt6370_pmu_reg_update_bits(c,reg,mask,0); }
static int mt6370_pmu_reg_set_bit(struct mt6370_pmu_chip *c, unsigned int reg, unsigned int mask)
{ return mt6370_pmu_reg_update_bits(c,reg,mask,mask); }
static int __mt6370_set_ichg(struct mt6370_pmu_charger_data *c, unsigned int ua)
{ assert(false); return 0; }
static void mdelay(unsigned int ms)
{
    assert(stock_count<ARRAY_SIZE(stock_trace));
    stock_trace[stock_count++]=(struct stock_event){'d',0,0,ms*1000};
}
static void mt6370_power_supply_changed(struct mt6370_pmu_charger_data *c) { }
'''
for name in ('MT6370_PMU_REG_CHGCTRL7','MT6370_PMU_REG_CHGCTRL2','MT6370_MASK_ICHG','MT6370_SHIFT_ICHG'):
    body += f'#define {name} {value(name)}U\n'
body += f"#define MT6370_MASK_CHG_EN {1 << value('MT6370_SHIFT_CHG_EN')}U\n"
body += function('mt6370_enable_charging', vendor)
checks = r'''
static void reset_bus(unsigned int vendor, bool open, unsigned int current, unsigned int protection)
{
    nops=unlocks=closes=stops=slept_us=0; notifications=0; memset(errors,0,sizeof(errors));
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
    assert(!priv.ichg_lock.owner && !gate && priv.ichg_valid && priv.ichg_request == (unsigned int)ua);
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
    case RES_INHIBIT:
        assert(!published && !irq_active); mt6370_chg_inhibit(&priv); assert(!(regs[0x112]&1)); break;
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

static int set_behaviour(int mode)
{
    union power_supply_propval val={.intval=mode};
    return mt6370_chg_psy_desc.set_property(&psy,POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,&val);
}
static int get_behaviour(int *value)
{
    union power_supply_propval val={.intval=*value};
    int ret=mt6370_chg_psy_desc.get_property(&psy,POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,&val);
    *value=val.intval; return ret;
}
static void control_setup(bool enable, unsigned int selector)
{
    setup(0xe0,false,500000,0x40);
    regs[STOCK_CURRENT_REG]=(selector << STOCK_CURRENT_SHIFT)|3;
    regs[0x112]=(regs[0x112]&~1U)|enable;
    priv.ichg_valid=true; priv.ichg_request=1000001;
}
static void stock_stop(unsigned int ua)
{
    struct mt6370_pmu_charger_data data={.ichg=ua};
    struct charger_device dev={.dev={.data=&data}};
    init_mutex(&data.ichg_access_lock); stock_count=0;
    assert(!mt6370_enable_charging(&dev,false));
    destroy_mutex(&data.ichg_access_lock);
}
static void compare_stop_trace(void)
{
    unsigned int index=0;
    for (unsigned int i=0; i<nops; i++) {
        if (trace[i].type == 'r') continue;
        assert(index<stock_count);
        struct stock_event *expected=&stock_trace[index++];
        assert(trace[i].type == expected->op && trace[i].reg == expected->reg);
        if (expected->op == 'd') assert(trace[i].value == expected->value);
        else assert((trace[i].value&expected->mask) == expected->value);
    }
    assert(index == stock_count);
}
static void *control_worker(void *id)
{
    thread_id=(uintptr_t)id;
    for (unsigned int i=0; i<50; i++) {
        assert(!set_current((i+thread_id)%2 ? 500000 : 1000000));
        assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO));
        assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE));
    }
    return NULL;
}
static void check_failed_ramp_wait(void)
{
    /* Fault fixtures start at 1 A. Even a failed 500 mA write may take effect. */
    for (unsigned int i=0; i<nops; i++)
        if (trace[i].type == 'w' && trace[i].reg == STOCK_CURRENT_REG) {
            assert((trace[i].value&STOCK_CURRENT_MASK) == (4U << STOCK_CURRENT_SHIFT));
            assert(i+1<nops && trace[i+1].type == 'd' && trace[i+1].value == 20000);
        }
}
static void check_controls(void)
{
    unsigned int stops_checked=0, resumes=0, failures=0, rejected=0, reads=0;
    init_mutex(&priv.ichg_lock); init_mutex(&priv.attach_lock); priv.regmap=&map;
    for (unsigned int i=0; i<F_MAX; i++) { fields[i].id=i; priv.rmap_fields[i]=&fields[i]; }
    for (unsigned int model=0; model<2; model++)
    for (unsigned int enabled=0; enabled<2; enabled++)
    for (unsigned int sel=0; sel<64; sel++) {
        control_setup(enabled,sel); if (model) { regs[0x100]=0x80; assert(!mt6370_chg_init_ichg(&priv)); nops=0; }
        unsigned int before[ARRAY_SIZE(regs)]; memcpy(before,regs,sizeof(before));
        unsigned int request=priv.ichg_request;
        int ret=set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE);
        assert(ret == (enabled && sel>49 ? -ERANGE : 0));
        assert(!(regs[0x112]&1) && !priv.ichg_lock.owner && notifications == 1);
        assert(priv.ichg_request == request && priv.ichg_valid == !ret);
        for (unsigned int i=0; i<ARRAY_SIZE(regs); i++)
            if (i != 0x112 && i != STOCK_CURRENT_REG) assert(regs[i] == before[i]);
        assert(regs[0x112] == (before[0x112]&~1U));
        assert(regs[STOCK_CURRENT_REG] == (enabled && sel>4 && sel<=49 ? 19U : before[STOCK_CURRENT_REG]));
        if (enabled && sel<=49) { stock_stop((sel+1)*100000); compare_stop_trace(); }
        else assert(!slept_us);
        int got=123; assert(!get_behaviour(&got) && got == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE); reads++;
        unsigned int prior_nops=nops, prior_time=slept_us;
        assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE));
        assert(nops == prior_nops+1 && slept_us == prior_time); stops_checked++;
        if (ret) {
            unsigned int start=nops;
            assert(set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) == -EAGAIN && nops == start); rejected++;
            assert(!set_current(request));
        }
        assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO));
        assert((regs[0x112]&1) && current_value() == 1000000 && !gate && priv.ichg_valid);
        assert((regs[STOCK_HIDDEN_REG]&0x60) == 0x40);
        got=123; assert(!get_behaviour(&got) && got == POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO); reads++; resumes++;
    }
    const int invalid[]={INT_MIN,-1,2,3,4,INT_MAX};
    const unsigned int ids[]={0x80,0xe0,0x90,0xa0,0xb0,0xf0};
    for (unsigned int id=0; id<ARRAY_SIZE(ids); id++) {
        setup(ids[id],false,900000,0x40); priv.ichg_valid=true; priv.ichg_request=900000;
        for (unsigned int b=0; b<ARRAY_SIZE(invalid); b++) {
            assert(set_behaviour(invalid[b]) == -EINVAL && !nops && !notifications); rejected++;
        }
        if (id>=2) {
            for (unsigned int mode=0; mode<2; mode++) {
                assert(set_behaviour(mode) == -EOPNOTSUPP && !nops && !notifications); rejected++;
            }
            int got=123; assert(get_behaviour(&got) == -EOPNOTSUPP && got == 123 && !nops); reads++;
        }
    }
    control_setup(false,4); priv.ichg_valid=false;
    assert(set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) == -EAGAIN && !nops && !(regs[0x112]&1)); rejected++;
    assert(!set_current(500000) && priv.ichg_valid);
    assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) && (regs[0x112]&1));
    assert(current_value() == 500000 && !(regs[STOCK_HIDDEN_REG]&0x60)); resumes++;
    /* Getter reflects the enable bit, not an optimistic cached mode. */
    for (unsigned int enabled=0; enabled<2; enabled++) {
        control_setup(enabled,9); int got=123;
        errors[0]=EIO;
        assert(get_behaviour(&got) == -EIO && got == 123 && priv.ichg_valid); reads++;
    }
    /* Each bus operation in both paths, then each later cleanup failure. */
    for (unsigned int mode=0; mode<2; mode++) {
        control_setup(mode == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE,9);
        assert(!set_behaviour(mode)); unsigned int count=nops;
        struct bus_op initial[32]; assert(count<=ARRAY_SIZE(initial)); memcpy(initial,trace,count*sizeof(*trace));
        for (unsigned int primary=0; primary<count; primary++) {
            if (initial[primary].type == 'd') continue;
            for (unsigned int effect=0; effect<2; effect++)
            for (unsigned int accepted=0; accepted<=4; accepted++) {
                control_setup(mode == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE,9);
                errors[primary]=EIO; apply_failed_write=effect; partial_key=accepted;
                assert(set_behaviour(mode) == -EIO && !priv.ichg_valid && !priv.ichg_lock.owner);
                check_failed_ramp_wait();
                assert(notifications == 1);
                if (regs[0x112]&1) {
                    assert(mode == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE);
                    assert(initial[primary].reg == 0x112);
                }
                unsigned int completed=nops;
                struct bus_op first_trace[32]; assert(completed<=ARRAY_SIZE(first_trace)); memcpy(first_trace,trace,completed*sizeof(*trace));
                unsigned int start=nops;
                assert(set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) == -EAGAIN && nops == start);
                /* Recovery requires a successful current-setting request; it never enables by itself. */
                memset(errors,0,sizeof(errors)); nops=0;
                assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE) && !(regs[0x112]&1));
                assert(!set_current(1000000) && !(regs[0x112]&1) && priv.ichg_valid && !gate);
                assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) && (regs[0x112]&1)); failures++;
                for (unsigned int second=primary+1; second<completed; second++) {
                    if (first_trace[second].type == 'd') continue;
                    control_setup(mode == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE,9);
                    errors[primary]=EIO; errors[second]=ETIMEDOUT; apply_failed_write=effect; partial_key=accepted;
                    assert(set_behaviour(mode) == -EIO && !priv.ichg_valid && !priv.ichg_lock.owner);
                    check_failed_ramp_wait();
                    assert(notifications == 1);
                    /* A failed stop may leave charging active. Do not claim otherwise. */
                    if (regs[0x112]&1) assert(first_trace[second].reg == 0x112);
                    unsigned int start=nops;
                    assert(set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) == -EAGAIN && nops == start);
                    memset(errors,0,sizeof(errors)); nops=0;
                    assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE) && !(regs[0x112]&1));
                    assert(!set_current(1000000) && !(regs[0x112]&1));
                    assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) && (regs[0x112]&1)); failures++;
                }
            }
        }
    }
    /* The visible current is hardware readback, including the temporary 500 mA setting. */
    control_setup(true,19);
    assert(!set_behaviour(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE));
    union power_supply_propval val={.intval=123};
    assert(!mt6370_chg_psy_desc.get_property(&psy,POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,&val) && val.intval == 500000);
    unsigned int start=nops; errors[start]=EIO; val.intval=123;
    assert(mt6370_chg_psy_desc.get_property(&psy,POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,&val) == -EIO && val.intval == 123); reads+=2;
    control_setup(true,9); pthread_t threads[4];
    for (uintptr_t i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_create(&threads[i],NULL,control_worker,(void *)(i+2)));
    for (unsigned int i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_join(threads[i],NULL));
    assert(!gate && !priv.ichg_lock.owner && !(regs[0x112]&1) && priv.ichg_valid);
    assert(notifications == 600);
    /* No bus transaction can cross into another thread while a ramp delay is pending. */
    for (unsigned int i=0; i<nops; i++) if (trace[i].type == 'd') {
        assert(i && i+2<nops);
        assert(trace[i-1].thread == trace[i].thread && trace[i+1].thread == trace[i].thread && trace[i+2].thread == trace[i].thread);
        assert(trace[i-1].reg == STOCK_CURRENT_REG && trace[i+1].reg == 0x112 && trace[i+2].reg == 0x112);
    }
    destroy_mutex(&priv.ichg_lock); destroy_mutex(&priv.attach_lock);
    printf("PASS: %u inhibit/readback cases, %u resumes, %u rejected control requests, %u control fault/recovery cases, %u control reads and 600 threaded control/current operations\n",stops_checked,resumes,rejected,failures,reads);
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
            assert(current_value() == 500000 && !priv.ichg_valid);
            /* Retry must repair stale protection and close a gate left open by the error. */
            nops=unlocks=closes=stops=slept_us=0; notifications=0; memset(errors,0,sizeof(errors));
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
                assert(trace[i].reg == STOCK_KEY_REG || trace[i].reg == STOCK_CURRENT_REG || trace[i].reg == 0x112 || trace[i].type == 'd');
            if (regs[0x112]&1) {
                assert(trace[second].reg == 0x112);
                assert(trace[second].type != 'w' || !effects);
            }
            assert(!priv.ichg_valid);
            /* Bus errors may leave charging active; a later successful request must not re-enable it. */
            nops=unlocks=closes=stops=slept_us=0; notifications=0; memset(errors,0,sizeof(errors));
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
    assert(publication_calls == 1 && published);
    priv.attach=MT6370_ATTACH_STAT_DETACH;
    unsigned int before=notifications; mt6370_chg_bc12_work_func(&priv.bc12_work);
    assert(notifications == before+1); release_probe(); probes++;
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
    check_controls();
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
    scope='production current/control helpers, properties, descriptor, init and probe; compiled Rabbit stop-sequence oracle; modeled regmap/time/devres/workqueue with pthread locks; no electrical or battery-policy validation'
),indent=2)+'\n')
