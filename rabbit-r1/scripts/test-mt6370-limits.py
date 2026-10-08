#!/usr/bin/env python3
"""Check charger property validation using the production range and setter code."""
import argparse
import json
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6370-limits'
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/power/supply/mt6370-charger.c')
parser.add_argument('--reproduce', action='store_true', help='demonstrate the unpatched behavior')
args = parser.parse_args()
source = args.source.resolve()
if not source.is_relative_to(ROOT):
    raise SystemExit('Source must stay under /rabbitr1')
s = source.read_text()
linear = (SRC/'lib/linear_ranges.c').read_text()
header = (SRC/'include/linux/linear_range.h').read_text()


def function(name, text=s):
    match = re.search(r'(?:static )?(?:inline )?[\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+2]+'\n'


prelude = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define dev_err(...) ((void)0)
typedef uint8_t u8;
struct regmap { int unused; };
struct reg_field { unsigned int reg, lsb, msb; };
#define REG_FIELD(r,l,h) {r,l,h}
struct regmap_field { unsigned int id; };
struct mutex { bool locked; };
struct work_struct { int unused; };
struct workqueue_struct { int unused; };
static unsigned int writes, queues;
static bool fail_write;
static unsigned int registers[0x200];
static void mutex_lock(struct mutex *m) { assert(!m->locked); m->locked=true; }
static void mutex_unlock(struct mutex *m) { assert(m->locked); m->locked=false; }
static struct mutex *lock_guard(struct mutex *m) { mutex_lock(m); return m; }
static void unlock_guard(struct mutex **m) { mutex_unlock(*m); }
#define JOIN_INNER(a,b) a##b
#define JOIN(a,b) JOIN_INNER(a,b)
#define guard(type) struct mutex *JOIN(held,__COUNTER__) __attribute__((cleanup(unlock_guard))) = lock_guard
#define lockdep_assert_held(m) assert((m)->locked)
static int regmap_write(struct regmap *map, unsigned int reg, unsigned int val)
{ assert(!"Unexpected hidden access on unvalidated variant"); return -EIO; }
static int regmap_bulk_write(struct regmap *map, unsigned int reg, const void *buf, size_t n)
{ assert(!"Unexpected hidden access on unvalidated variant"); return -EIO; }
static int regmap_update_bits(struct regmap *map, unsigned int reg, unsigned int mask, unsigned int val)
{ assert(!"Unexpected hidden access on unvalidated variant"); return -EIO; }
static bool queue_work(struct workqueue_struct *q, struct work_struct *w)
{ queues++; return true; }
union power_supply_propval { int intval; };
enum { POWER_SUPPLY_USB_TYPE_UNKNOWN };
enum power_supply_property {
    POWER_SUPPLY_PROP_ONLINE, POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,
    POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE, POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,
    POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT, POWER_SUPPLY_PROP_PRECHARGE_CURRENT,
    POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT, POWER_SUPPLY_PROP_STATUS, POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,
    POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT_MAX, POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE_MAX,
};
enum { POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO, POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE };
struct power_supply { void *drvdata; };
static void power_supply_changed(struct power_supply *psy) { }
static void usleep_range(unsigned int lo, unsigned int hi) { assert(false); }
static int regmap_field_read(struct regmap_field *f, unsigned int *out) { assert(false); return -EIO; }
static void *power_supply_get_drvdata(struct power_supply *psy) { return psy->drvdata; }
'''
body = header[header.index('struct linear_range {'):header.index('unsigned int linear_range_values_in_range')]
for name in ('linear_range_get_max_value', 'linear_range_get_value',
             'linear_range_get_selector_high', 'linear_range_get_selector_within'):
    body += function(name, linear)
body += s[s.index('#define MT6370_REG_'):s.index('struct mt6370_priv {')]
body += r'''
struct delayed_work { int unused; };
#define HZ 100
static unsigned long jiffies;
static void *system_highpri_wq;
static bool mod_delayed_work(void *q, struct delayed_work *w, unsigned long d) { assert(false); return false; }
struct mt6370_priv {
    bool managed_charging; unsigned long policy_deadline; struct delayed_work policy_watchdog;
    struct regmap_field *rmap_fields[F_MAX];
    struct mutex attach_lock;
    struct mutex ichg_lock;
    struct mutex psy_lock;
    bool stopping;
    struct regmap *regmap;
    bool ichg_workaround;
    unsigned int ichg_min, ichg_request;
    bool ichg_valid, input_suspended;
    int attach, psy_usb_type;
    struct workqueue_struct *wq;
    struct work_struct bc12_work;
};
'''
body += s[s.index('struct mt6370_chg_field {'):s.index('static inline int mt6370_chg_field_get')]
body += r'''
static int regmap_field_write(struct regmap_field *field, unsigned int selector)
{
    const struct reg_field *r=&mt6370_chg_fields[field->id].field;
    assert(r->reg < ARRAY_SIZE(registers));
    assert(selector <= GENMASK(r->msb-r->lsb,0));
    writes++;
    if (fail_write) return -EIO;
    unsigned int mask=GENMASK(r->msb,r->lsb);
    registers[r->reg]=(registers[r->reg]&~mask)|(selector << r->lsb);
    return 0;
}
'''
body += function('mt6370_chg_field_set')
if 'static int mt6370_chg_set_ichg(' in s:
    for name in ('mt6370_chg_field_get', 'mt6370_chg_stop', 'mt6370_chg_program_ichg',
                 'mt6370_chg_set_ichg', 'mt6370_chg_set_behaviour'):
        body += function(name)
body += r'''
static int mt6370_chg_suspend_input(struct mt6370_priv *p) { assert(false); return -EIO; }
static int mt6370_chg_set_input(struct mt6370_priv *p, unsigned int ua) { assert(false); return -EIO; }
static int mt6370_chg_set_mivr(struct mt6370_priv *p, unsigned int uv) { assert(false); return -EIO; }
'''
body += function('mt6370_chg_set_online')
body += function('mt6370_chg_set_property')
body += function('mt6370_chg_property_is_writeable')
checks = r'''
static struct mt6370_priv priv;
static struct power_supply psy={.drvdata=&priv};
static struct regmap_field fields[F_MAX];
struct limits {
    enum power_supply_property property;
    enum mt6370_chg_reg_field field;
    unsigned int reg, lsb, msb, first, last, min, max, step;
    bool round_up;
};
static const struct limits limits[] = {
    {POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,F_ICHG,0x117,2,7,8,49,900000,5000000,100000,false},
    {POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE,F_VOREG,0x114,1,7,0,81,3900000,4710000,10000,false},
    {POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,F_IAICR,0x113,2,7,0,63,100000,3250000,50000,false},
    {POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT,F_VMIVR,0x116,1,7,0,95,3900000,13400000,100000,true},
    {POWER_SUPPLY_PROP_PRECHARGE_CURRENT,F_IPREC,0x118,0,3,0,15,100000,850000,50000,false},
    {POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT,F_IEOC,0x119,4,7,0,15,100000,850000,50000,false},
};
static void setup(unsigned int seed)
{
    assert(!priv.attach_lock.locked);
    assert(!priv.ichg_lock.locked);
    priv.ichg_min=900000; priv.ichg_workaround=false;
    for (unsigned int i=0; i<ARRAY_SIZE(registers); i++) registers[i]=seed;
    for (unsigned int i=0; i<F_MAX; i++) { fields[i].id=i; priv.rmap_fields[i]=&fields[i]; }
    queues=writes=0; fail_write=false; priv.attach=0;
}
static int set(enum power_supply_property property, int value)
{
    union power_supply_propval val={.intval=value};
    int ret=mt6370_chg_set_property(&psy,property,&val);
    assert(val.intval == value && !priv.attach_lock.locked);
    return ret;
}
int main(void)
{
#ifdef REPRODUCE
    for (unsigned int i=0; i<ARRAY_SIZE(limits); i++) {
        const struct limits *r=&limits[i];
        setup(0);
        assert(set(r->property,-1) == 0 && writes == 1);
        unsigned int selector=(registers[r->reg] >> r->lsb) & GENMASK(r->msb-r->lsb,0);
        assert(selector == r->last);
        printf("REPRODUCED: property %u request -1 writes selector %u, value %u\n",r->property,selector,r->max);
    }
    setup(0); assert(set(POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,500000) == 0);
    assert((registers[0x117] >> 2) == 8);
    puts("REPRODUCED: 500000 uA request silently becomes 900000 uA");
    setup(0); assert(set(POWER_SUPPLY_PROP_ONLINE,-1) == 0 && priv.attach == 1 && queues == 1);
    puts("REPRODUCED: ONLINE=-1 becomes attached");
#else
    unsigned int valid=0, rejected=0, online=0;
    for (unsigned int i=0; i<ARRAY_SIZE(limits); i++) {
        const struct limits *r=&limits[i];
        assert(mt6370_chg_property_is_writeable(&psy,r->property) == 1);
        const struct reg_field *field=&mt6370_chg_fields[r->field].field;
        assert(field->reg == r->reg && field->lsb == r->lsb && field->msb == r->msb);
        const int invalid[]={INT_MIN,-1000000,-1,0,(int)r->min/2,(int)r->min-1,(int)r->max+1,INT_MAX};
        for (unsigned int v=0; v<ARRAY_SIZE(invalid); v++) {
            setup(0xa5);
            assert(set(r->property,invalid[v]) == (invalid[v] < 0 ? -EINVAL : -ERANGE));
            assert(writes == 0 && queues == 0 && priv.attach == 0);
            for (unsigned int j=0; j<ARRAY_SIZE(registers); j++) assert(registers[j] == 0xa5);
            rejected++;
        }
        for (unsigned int selector=r->first; selector<=r->last; selector++)
        for (int offset=-1; offset<=1; offset++) {
            int value=(int)(r->min+(selector-r->first)*r->step)+offset;
            if (value < (int)r->min || value > (int)r->max) continue;
            /* Choose from enumerated settings, independently of the range helpers. */
            unsigned int expected=r->first;
            for (unsigned int candidate=r->first; candidate<=r->last; candidate++) {
                unsigned int setting=r->min+(candidate-r->first)*r->step;
                if (r->round_up) { if (setting >= (unsigned int)value) { expected=candidate; break; } }
                else if (setting <= (unsigned int)value) expected=candidate;
            }
            for (unsigned int seed=0; seed<2; seed++)
            for (unsigned int error=0; error<2; error++) {
                unsigned int initial=seed ? 0xff : 0;
                setup(initial); fail_write=error;
                assert(set(r->property,value) == (error ? -EIO : 0));
                assert(writes == 1 && queues == 0);
                unsigned int mask=GENMASK(r->msb,r->lsb);
                unsigned int updated=(initial&~mask)|(expected << r->lsb);
                for (unsigned int reg=0; reg<ARRAY_SIZE(registers); reg++)
                    assert(registers[reg] == (reg == r->reg && !error ? updated : initial));
                valid++;
            }
        }
    }
    setup(0xa5);
    assert(set(POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,500000) == -ERANGE);
    assert(writes == 0 && queues == 0);
    rejected++;
    const int bad_online[]={INT_MIN,-1,2,INT_MAX};
    for (unsigned int i=0; i<ARRAY_SIZE(bad_online); i++) {
        setup(0xa5);
        assert(set(POWER_SUPPLY_PROP_ONLINE,bad_online[i]) == -EINVAL);
        assert(writes == 0 && queues == 0 && priv.attach == 0);
        rejected++;
    }
    for (int initial=0; initial<3; initial++)
    for (int requested=0; requested<2; requested++) {
        setup(0xa5); priv.attach=initial;
        assert(set(POWER_SUPPLY_PROP_ONLINE,requested) == 0);
        bool change=(!!initial != requested);
        assert(priv.attach == (change ? requested : initial));
        assert(queues == (unsigned int)change && writes == 0);
        online++;
    }
    const enum power_supply_property readonly[]={POWER_SUPPLY_PROP_STATUS,
        POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT_MAX,POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE_MAX,999};
    for (unsigned int i=0; i<ARRAY_SIZE(readonly); i++) {
        setup(0xa5);
        assert(mt6370_chg_property_is_writeable(&psy,readonly[i]) == 0);
        assert(set(readonly[i],1000000) == -EINVAL && writes == 0 && queues == 0);
        rejected++;
    }
    printf("PASS: %u limit writes/bus failures, %u rejected requests and %u ONLINE transitions; production ranges, rounding and neighboring bits checked\n",valid,rejected,online);
#endif
    return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+body+checks)
command = ['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
           '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-const-variable',
           '-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
if args.reproduce:
    command.append('-DREPRODUCE')
subprocess.run(command+[str(path),'-o',str(OUT/'host')],check=True)
subprocess.run([str(OUT/'host')],check=True)
report = dict(hardware_tested=False, reproduce=args.reproduce,
              scope='compiled charger setter, online handler, tables and linear-range helpers; modeled regmap/locks/workqueue; no battery-policy or electrical validation')
(ROOT/'out/mt6370-limits-audit.json').write_text(json.dumps(report,indent=2)+'\n')
