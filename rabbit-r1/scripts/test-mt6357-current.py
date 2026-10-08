#!/usr/bin/env python3
"""Check MT6357 battery measurements against stock conversion and latch definitions."""
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
             'src/kernel/drivers/power/supply/mtk_battery_table.h',
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
    match = re.search(r'(?:static )?[\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
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
typedef int32_t s32;
typedef int64_t s64;
typedef uint64_t u64;
typedef int64_t ktime_t;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define FIELD_GET(mask,v) (((v)&(mask)) >> __builtin_ctz(mask))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define div_s64(n,d) ((s64)(n)/(d))
#define div64_s64(n,d) ((s64)(n)/(s64)(d))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define PTR_ERR(p) ((intptr_t)(p))
#define EPROBE_DEFER 517
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
enum power_supply_property { POWER_SUPPLY_PROP_CURRENT_NOW, POWER_SUPPLY_PROP_VOLTAGE_NOW, POWER_SUPPLY_PROP_TEMP, POWER_SUPPLY_PROP_CAPACITY };
#define POWER_SUPPLY_TYPE_BATTERY 1
struct power_supply_desc {
    const char *name; int type;
    const enum power_supply_property *properties;
    size_t num_properties;
    int (*get_property)(struct power_supply *, enum power_supply_property, union power_supply_propval *);
};
enum iio_chan_type { IIO_VOLTAGE, IIO_CURRENT, IIO_TEMP };
struct iio_channel { unsigned int id; enum iio_chan_type type; };
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
static bool adc_present;
static unsigned int property_pullup=16900, property_series=10000;
static int table_count=42, table_count_error, table_read_error;
static u32 property_table[128];
static struct mt6357_thermistor_point table_storage[64];
static struct iio_channel channels[3];
static int channel_order[3]={0,1,2}, channel_get_error[3], channel_type_error[3];
static int adc_values[3]={3800,669,1800}, adc_errors[3], adc_success;
static unsigned int adc_reads[3];
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
    } else if (!strcmp(name,"mediatek,thermistor-pullup-ohms")) {
        *out=property_pullup;
    } else if (!strcmp(name,"mediatek,thermistor-series-micro-ohms")) {
        *out=property_series;
    } else {
        assert(!strcmp(name,"mediatek,current-gain-permille") && gain_present);
        if (malformed_gain) return -EOVERFLOW;
        *out=property_gain;
    }
    return 0;
}
static bool device_property_present(struct device *d, const char *name)
{
    if (!strcmp(name,"io-channels")) return adc_present;
    assert(!strcmp(name,"mediatek,current-gain-permille")); return gain_present;
}
static int device_property_count_u32(struct device *d, const char *name)
{
    assert(!strcmp(name,"mediatek,thermistor-resistance-table"));
    int ret=step(); return ret ? ret : table_count_error ? table_count_error : table_count;
}
static void *devm_kmalloc_array(struct device *d, size_t count, size_t size, int flags)
{
    int ret=step(); if (ret) return NULL;
    assert(count>=2 && count<=64 && size == sizeof(table_storage[0]));
    memset(table_storage,0,sizeof(table_storage)); return table_storage;
}
static int device_property_read_u32_array(struct device *d, const char *name, u32 *out, size_t count)
{
    assert(!strcmp(name,"mediatek,thermistor-resistance-table") && count == (unsigned int)table_count);
    int ret=step(); memcpy(out,property_table,count*sizeof(u32));
    return ret ? ret : table_read_error;
}
static struct iio_channel *devm_iio_channel_get(struct device *d, const char *name)
{
    const char *names[]={"battery-voltage","battery-thermistor","thermistor-reference"};
    int ret=step(); if (ret) return ERR_PTR(ret);
    for (unsigned int i=0; i<ARRAY_SIZE(names); i++)
        if (!strcmp(name,names[i])) return channel_get_error[i] ? ERR_PTR(channel_get_error[i]) : &channels[channel_order[i]];
    assert(false); return NULL;
}
static int iio_get_channel_type(struct iio_channel *chan, enum iio_chan_type *type)
{
    int ret=step(); *type=chan->type;
    return ret ? ret : channel_type_error[chan->id];
}
static int iio_read_channel_processed(struct iio_channel *chan, int *value)
{
    assert(chan >= channels && chan < channels+3);
    unsigned int id=chan->id; adc_reads[id]++;
    *value=adc_errors[id] ? INT_MIN : adc_values[id];
    return adc_errors[id] ? adc_errors[id] : adc_success;
}
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
    assert(desc == &gauge.desc);
    assert(desc->num_properties == (adc_present ? 3U : 1U) && desc->properties[0] == POWER_SUPPLY_PROP_CURRENT_NOW);
    if (adc_present) {
        assert(desc->properties[1] == POWER_SUPPLY_PROP_VOLTAGE_NOW && desc->properties[2] == POWER_SUPPLY_PROP_TEMP);
        assert(gauge.voltage == &channels[channel_order[0]] && gauge.thermistor == &channels[channel_order[1]]);
        assert(gauge.reference == &channels[channel_order[2]] && gauge.table == table_storage);
        assert(gauge.num_points == (unsigned int)table_count/2 && gauge.pullup_ohms);
    }
    assert(gauge.lock.initialized && gauge.needs_release && gauge.shunt_uohms && gauge.gain_permille);
    assert(gauge.regmap == &map);
    int ret=step(); if (ret) return ERR_PTR(ret);
    if (fail_registration) return ERR_PTR(-EIO);
    union power_supply_propval val={.intval=123};
    /* The framework can invoke callbacks immediately, even before probe returns. */
    assert(desc->get_property(&psy,POWER_SUPPLY_PROP_CURRENT_NOW,&val) == -EAGAIN && val.intval == 123);
    if (adc_present) {
        assert(!desc->get_property(&psy,POWER_SUPPLY_PROP_VOLTAGE_NOW,&val));
        assert(val.intval == adc_values[channel_order[0]]*1000);
        val.intval=123;
        assert(desc->get_property(&psy,POWER_SUPPLY_PROP_TEMP,&val) == -EAGAIN && val.intval == 123);
    }
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
battery = (ROOT/'src/kernel/drivers/power/supply/mtk_battery.c').read_text()
table_header = (ROOT/'src/kernel/drivers/power/supply/mtk_battery_table.h').read_text()
assert re.search(r'^#define BAT_NTC_10\s+1$', table_header, re.M)
table_source = re.search(r'struct fuelgauge_temperature Fg_Temperature_Table\[21\] = \{(.*?)\n};', table_header, re.S)[1]
points = [tuple(map(int, row)) for row in re.findall(r'\{(-?\d+),\s*(\d+)\}', table_source)]
assert len(points) == 21
stock += r'''
#define IS_ENABLED(x) (x)
#define ENOTSUPP 524
#define GAUGE_PROP_BIF_VOLTAGE 1
struct fuelgauge_temperature { int BatteryTemp, TemperatureR; };
struct mtk_battery {
    struct fuelgauge_temperature *tmp_table;
    struct { int rbat_pull_up_r, rbat_pull_up_volt; } rbat;
};
static struct fuelgauge_temperature Fg_Temperature_Table[21] = {
'''+table_source+'\n};\n'
stock += 'static const struct mt6357_thermistor_point stock_table[] = {\n'
stock += ''.join(f'    {{{temp*1000}, {ohms}}},\n' for temp, ohms in points)+'};\n'
stock += r'''
static int stock_reference;
static int gauge_get_property(int prop, int *out)
{ assert(prop == GAUGE_PROP_BIF_VOLTAGE); *out=stock_reference; return 0; }
'''
stock += function('BattThermistorConverTemp', battery)
stock += function('BattVoltToTemp', battery)
start = battery.index('\t\t\tif (fg_current_temp > 0)', battery.index('int force_get_tbat_internal'))
end = battery.index('\n\t\t}', start)
stock += r'''
static int stock_temperature(int mv, int reference, int ua)
{
    struct mtk_battery battery={.tmp_table=Fg_Temperature_Table, .rbat={16900,1800}};
    struct mtk_battery *gm=&battery;
    int bat_temperature_volt=mv, bat_temperature_val=123;
    int fg_current_temp=ua/100, fg_r_value=0, fg_meter_res_value=100;
    int bat_temperature_volt_temp=0, vol_cali=0;
    bool fg_current_state=false;
    stock_reference=reference;
'''+battery[start:end]+r'''
    (void)bat_temperature_volt_temp;
    return bat_temperature_val;
}
'''
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

static int read_property(enum power_supply_property prop, int *value)
{
    union power_supply_propval val={.intval=*value};
    int ret=mt6357_gauge_desc.get_property(&psy,prop,&val);
    *value=val.intval; return ret;
}
static void adc_setup(void)
{
    property_pullup=16900; property_series=10000; table_count=42;
    table_count_error=table_read_error=adc_success=0;
    memcpy(property_table,stock_table,sizeof(stock_table));
    memcpy(table_storage,stock_table,sizeof(stock_table));
    gauge.table=table_storage; gauge.num_points=ARRAY_SIZE(stock_table);
    gauge.pullup_ohms=16900; gauge.series_uohms=10000;
    for (unsigned int i=0; i<3; i++) {
        channels[i]=(struct iio_channel){.id=i,.type=IIO_VOLTAGE}; channel_order[i]=i;
        channel_get_error[i]=channel_type_error[i]=adc_errors[i]=0; adc_reads[i]=0;
    }
    adc_values[0]=3800; adc_values[1]=669; adc_values[2]=1800;
    gauge.voltage=&channels[0]; gauge.thermistor=&channels[1]; gauge.reference=&channels[2];
}
static void check_temperature(void)
{
    unsigned int conversions=0, samples=0, failures=0, probes=0;
    init_lock(); adc_setup();
    const int refs[]={900,1500,1700,1800,1900};
    const int currents[]={-2000100,-1999999,-1000100,-1000000,-999999,-100000,-99999,
        -1000,-999,0,999,1000,99999,100000,999999,1000000,1000100,1999999,2000100};
    /* All 12-bit NTC codes, measured references and both current directions. */
    for (unsigned int r=0; r<ARRAY_SIZE(refs); r++)
    for (unsigned int c=0; c<ARRAY_SIZE(currents); c++)
    for (unsigned int raw=0; raw<4096; raw++) {
        int mv=raw*1800/4096, got=123;
        int corrected=mv-(currents[c]/1000)/100;
        int ret=mt6357_gauge_temperature(&gauge,mv,refs[r],currents[c],&got);
        if (mv<=0 || mv>=refs[r] || corrected<=0) assert(ret == -ERANGE && got == 123);
        else assert(!ret && got == stock_temperature(mv,refs[r],currents[c]));
        conversions++;
    }
    struct mtk_battery battery={.tmp_table=Fg_Temperature_Table};
    /* Sweep every ohm across and beyond the stock table, including negative temperatures. */
    for (int ohms=1; ohms<=200000; ohms++) {
        gauge.pullup_ohms=ohms; int got=123;
        assert(!mt6357_gauge_temperature(&gauge,1,2,0,&got));
        assert(got == BattThermistorConverTemp(&battery,ohms)); conversions++;
    }
    adc_setup();
    /* Integer extremes exercise compensation and interpolation without overflow. */
    struct mt6357_thermistor_point extreme[]={{INT_MIN,INT_MAX},{INT_MAX,1}};
    gauge.table=extreme; gauge.num_points=2; gauge.pullup_ohms=INT_MAX;
    int got=123; assert(!mt6357_gauge_temperature(&gauge,1,2,0,&got) && got == INT_MIN/100);
    gauge.pullup_ohms=1;
    assert(!mt6357_gauge_temperature(&gauge,1,2,0,&got) && got == INT_MAX/100);
    gauge.pullup_ohms=1073741824;
    assert(!mt6357_gauge_temperature(&gauge,1,2,0,&got) && got == 0);
    gauge.pullup_ohms=UINT_MAX;
    assert(!mt6357_gauge_temperature(&gauge,INT_MAX-1,INT_MAX,0,&got) && got == INT_MIN/100);
    gauge.series_uohms=UINT_MAX; got=123;
    assert(mt6357_gauge_temperature(&gauge,1,2,INT_MAX,&got) == -ERANGE && got == 123);
    assert(mt6357_gauge_temperature(&gauge,1,2,INT_MIN,&got) == -ERANGE && got == 123);
    gauge.series_uohms=0; gauge.pullup_ohms=1;
    assert(!mt6357_gauge_temperature(&gauge,1,2,INT_MIN,&got) && got == INT_MAX/100);
    conversions+=7;
    const int bad[][2]={{0,1800},{-1,1800},{INT_MIN,1800},{1800,1800},{1801,1800},
        {669,0},{669,-1},{669,INT_MIN},{INT_MAX,INT_MAX},{669,668}};
    for (unsigned int i=0; i<ARRAY_SIZE(bad); i++) {
        adc_setup(); got=123;
        assert(mt6357_gauge_temperature(&gauge,bad[i][0],bad[i][1],0,&got) == -ERANGE && got == 123);
        failures++;
    }
    const unsigned int raw[]={0,1,3182,32767,32768,62353,65534,65535};
    for (unsigned int i=0; i<ARRAY_SIZE(raw); i++)
    for (unsigned int positive=0; positive<3; positive++) {
        setup(true,raw[i]); adc_setup(); adc_success=positive;
        got=123; assert(!read_property(POWER_SUPPLY_PROP_TEMP,&got));
        assert(got == stock_temperature(669,1800,expected(raw[i])));
        assert(!adc_reads[0] && adc_reads[1] == 1 && adc_reads[2] == 1 && data_reads == 1);
        got=123; assert(!read_property(POWER_SUPPLY_PROP_VOLTAGE_NOW,&got) && got == 3800000);
        assert(adc_reads[0] == 1 && data_reads == 1); samples++;
    }
    /* Missing channels and failed measurements must leave the caller's value untouched. */
    setup(false,1); adc_setup(); gauge.voltage=gauge.thermistor=NULL;
    got=123; assert(read_property(POWER_SUPPLY_PROP_VOLTAGE_NOW,&got) == -ENODATA && got == 123);
    assert(read_property(POWER_SUPPLY_PROP_TEMP,&got) == -ENODATA && got == 123 && !nops);
    failures+=2;
    const int voltages[]={INT_MIN,-1,0,1,4000,INT_MAX/1000,INT_MAX/1000+1,INT_MAX};
    for (unsigned int i=0; i<ARRAY_SIZE(voltages); i++) {
        setup(false,1); adc_setup(); adc_values[0]=voltages[i]; got=123;
        int ret=read_property(POWER_SUPPLY_PROP_VOLTAGE_NOW,&got);
        if (voltages[i]<=0 || voltages[i]>INT_MAX/1000) assert(ret == -ERANGE && got == 123);
        else assert(!ret && got == voltages[i]*1000);
        assert(!nops); samples++;
    }
    const int errors_to_inject[]={-EIO,-ETIMEDOUT,-ENODEV,-EPROBE_DEFER};
    for (unsigned int i=0; i<ARRAY_SIZE(errors_to_inject); i++)
    for (unsigned int chan=0; chan<3; chan++) {
        setup(false,3182); adc_setup(); adc_errors[chan]=errors_to_inject[i]; got=123;
        assert(read_property(chan ? POWER_SUPPLY_PROP_TEMP : POWER_SUPPLY_PROP_VOLTAGE_NOW,&got) == errors_to_inject[i]);
        assert(got == 123 && adc_reads[chan] == 1);
        if (chan != 2) assert(!nops && !adc_reads[2]);
        adc_errors[chan]=0;
        assert(!read_property(chan ? POWER_SUPPLY_PROP_TEMP : POWER_SUPPLY_PROP_VOLTAGE_NOW,&got)); failures++;
    }
    for (unsigned int which=0; which<4; which++) {
        setup(false,3182); adc_setup(); got=123;
        if (!which) regs[STOCK_ON_REG]=0;
        if (which == 1) regs[STOCK_DIG_PD_REG]=STOCK_DIG_PD_MASK;
        if (which == 2) errors[0]=EIO;
        if (which == 3) stall_start=true;
        int ret=read_property(POWER_SUPPLY_PROP_TEMP,&got);
        assert(ret == (which<2 ? -EAGAIN : which == 2 ? -EIO : -ETIMEDOUT));
        assert(got == 123 && adc_reads[1] == 1 && !adc_reads[2]); failures++;
    }
    /* A failed current-latch cleanup must not turn into a temperature sample. */
    setup(false,3182); adc_setup(); assert(!read_property(POWER_SUPPLY_PROP_TEMP,&got));
    unsigned int ops=nops;
    for (unsigned int op=0; op<ops; op++) {
        setup(false,3182); adc_setup(); errors[op]=EIO; got=123;
        assert(read_property(POWER_SUPPLY_PROP_TEMP,&got) == -EIO && got == 123 && !adc_reads[2]);
        retry(); failures++;
    }
    destroy_lock();
    struct platform_device pdev={.dev={.parent=&pmic_dev}};
    adc_present=true; gain_present=true; fail_stage=0;
    setup(false,0); adc_setup(); regs[STOCK_ON_REG]=0; stage=0;
    assert(!mt6357_gauge_probe(&pdev)); unsigned int stages=stage; destroy_lock(); probes++;
    for (unsigned int f=1; f<=stages; f++) {
        setup(false,0); adc_setup(); regs[STOCK_ON_REG]=0; stage=0; fail_stage=f;
        unsigned int before=registrations;
        assert(mt6357_gauge_probe(&pdev) < 0 && registrations == before);
        destroy_lock(); probes++;
    }
    fail_stage=0;
    for (unsigned int chan=0; chan<3; chan++)
    for (unsigned int reason=0; reason<4; reason++) {
        setup(false,0); adc_setup(); stage=0;
        int expected=reason == 0 ? -EPROBE_DEFER : reason == 1 ? -ENODEV : reason == 2 ? -EIO : -EINVAL;
        if (reason<2) channel_get_error[chan]=expected;
        else if (reason == 2) channel_type_error[chan]=expected;
        else channels[chan].type=IIO_CURRENT;
        unsigned int before=registrations;
        assert(mt6357_gauge_probe(&pdev) == expected && registrations == before && !nops);
        destroy_lock(); probes++;
    }
    for (unsigned int bad=0; bad<13; bad++) {
        setup(false,0); adc_setup(); stage=0;
        switch (bad) {
        case 0: property_pullup=0; break;
        case 1: table_count=0; break;
        case 2: table_count=2; break;
        case 3: table_count=41; break;
        case 4: table_count=130; break;
        case 5: property_table[3]=0; break;
        case 6: property_table[1]=UINT_MAX; break;
        case 7: property_table[2]=property_table[0]; break;
        case 8: property_table[2]=property_table[0]-1; break;
        case 9: property_table[3]=property_table[1]; break;
        case 10: property_table[3]=property_table[1]+1; break;
        case 11: table_count_error=-EPROTO; break;
        case 12: table_read_error=-EIO; break;
        }
        unsigned int before=registrations;
        assert(mt6357_gauge_probe(&pdev) == (bad == 11 ? -EPROTO : bad == 12 ? -EIO : -EINVAL));
        assert(registrations == before && !nops); destroy_lock(); probes++;
    }
    /* Named lookup works independently of provider/consumer array order. */
    const int permutations[][3]={{0,1,2},{0,2,1},{1,0,2},{1,2,0},{2,0,1},{2,1,0}};
    for (unsigned int p=0; p<ARRAY_SIZE(permutations); p++) {
        setup(false,0); adc_setup(); regs[STOCK_ON_REG]=0; stage=0;
        memcpy(channel_order,permutations[p],sizeof(channel_order));
        adc_values[channel_order[0]]=3800; adc_values[channel_order[1]]=669; adc_values[channel_order[2]]=1800;
        assert(!mt6357_gauge_probe(&pdev)); destroy_lock(); probes++;
    }
    /* Two-point and maximum-size tables, zero compensation, full-range integers. */
    for (unsigned int size=2; size<=64; size+=62) {
        setup(false,0); adc_setup(); regs[STOCK_ON_REG]=0; stage=0; table_count=size*2;
        property_pullup=UINT_MAX; property_series=0;
        for (unsigned int i=0; i<size; i++) { property_table[i*2]=i*1000; property_table[i*2+1]=size-i; }
        assert(!mt6357_gauge_probe(&pdev)); destroy_lock(); probes++;
    }
    adc_present=false;
    printf("PASS: %u stock temperature/boundary comparisons, %u ADC samples, %u input/fault cases and %u ADC probe cases\n",conversions,samples,failures,probes);
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
    check_temperature();
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
    scope='production current/voltage/temperature conversion, acquisition, property and probe with stock arithmetic oracles and actual kernel polling macros; modeled regmap/IIO/properties/registration, pthread locks; no electrical or capacity validation'
),indent=2)+'\n')
