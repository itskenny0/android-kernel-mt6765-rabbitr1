#!/usr/bin/env python3
"""Test the production MT6357 counter against captured stock instructions and I/O faults."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/mt6357-charge-counter'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/power/supply/mt6357-gauge.c')
parser.add_argument('--replay-stock', action='store_true',
                    help='also execute captured stock ARM64 instructions with Unicorn')
args = parser.parse_args()
source = args.source.resolve()
if not source.is_relative_to(ROOT):
    raise SystemExit('Source must stay under /rabbitr1')
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
fixture_path = Path(__file__).resolve().parent.parent/'tests/battery/mt6357-stock-coulomb.json'
fixture = json.loads(fixture_path.read_text())
provenance = fixture['provenance']
code = bytes.fromhex(provenance['function_bytes_hex'])
assert hashlib.sha256(code).hexdigest() == provenance['function_bytes_sha256']
vendor_path = ROOT/provenance['source']
assert hashlib.sha256(vendor_path.read_bytes()).hexdigest() == provenance['source_sha256']
vendor = vendor_path.read_text()
s = source.read_text()
vectors = [(int(raw, 16), shunt, gain, result)
           for raw, shunt, gain, result in fixture['vectors']]
assert len(vectors) == 2664


def replay_stock():
    """Execute the captured function; only external calls are supplied by mocks."""
    # Source CI runs before stock metadata extraction; only replay needs the DTB.
    stock_dtb = ROOT/'firmware/stock-v0.8.293/merged.dtb'
    assert hashlib.sha256(stock_dtb.read_bytes()).hexdigest() == provenance['stock_merged_dtb_sha256']
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn.arm64_const import (UC_ARM64_REG_X0, UC_ARM64_REG_X1,
                                    UC_ARM64_REG_X2, UC_ARM64_REG_X30,
                                    UC_ARM64_REG_SP, UC_ARM64_REG_PC)
    original = int(provenance['function_address'], 16)
    address, data, stack, stop = 0x100618, 0x200000, 0x308000, 0x400000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    for start, size in ((0x100000, 0x2000), (data, 0x10000),
                        (0x300000, 0x10000), (stop, 0x1000)):
        uc.mem_map(start, size)
    uc.mem_write(address, code)
    canary = 0xffffff800973db08-original+address
    uc.mem_map(canary & ~0xfff, 0x1000)
    uc.mem_write(canary, bytes(8))
    events = []
    raw = 0

    def hook(uc, pc, size, opaque):
        insn = struct.unpack_from('<I', code, pc-address)[0]
        if insn >> 26 != 0x25:  # BL; arithmetic and branches run unchanged.
            return
        displacement = insn & 0x3ffffff
        if displacement & 0x2000000:
            displacement -= 0x4000000
        target = original+pc-address+displacement*4
        if target == 0xffffff8008661cac:  # regmap_read
            reg = uc.reg_read(UC_ARM64_REG_X1)
            assert reg in (0xd12, 0xd14)
            events.append(reg)
            value = (raw if reg == 0xd12 else raw >> 16) & 0xffff
            uc.mem_write(uc.reg_read(UC_ARM64_REG_X2), struct.pack('<I', value))
            uc.reg_write(UC_ARM64_REG_X0, 0)
        elif target == 0xffffff8008ae00dc:
            events.append('pre')
        elif target == 0xffffff8008ae01a4:
            events.append('post')
        elif target == 0xffffff8008ad6288:  # debug_log_level
            uc.reg_write(UC_ARM64_REG_X0, 0)
        else:
            assert target == 0xffffff800809f01c, hex(target)  # _mcount
        uc.reg_write(UC_ARM64_REG_PC, pc+4)

    uc.hook_add(UC_HOOK_CODE, hook, begin=address, end=address+len(code)-1)
    for raw, shunt, gain, expected in vectors:
        assert shunt % 100 == 0
        # Verified layout of stock struct mtk_gauge, not a mainline ABI.
        uc.mem_write(data+272, struct.pack('<ii', shunt//100, gain))
        uc.mem_write(data+8, struct.pack('<Q', data+0x1000))
        uc.mem_write(data+0x4000, struct.pack('<i', 0x12345678))
        events.clear()
        for reg, value in ((UC_ARM64_REG_X0, data), (UC_ARM64_REG_X1, 0),
                           (UC_ARM64_REG_X2, data+0x4000), (UC_ARM64_REG_X30, stop),
                           (UC_ARM64_REG_SP, stack)):
            uc.reg_write(reg, value)
        uc.emu_start(address, stop, count=1000)
        assert uc.reg_read(UC_ARM64_REG_PC) == stop
        assert uc.reg_read(UC_ARM64_REG_X0) == 0
        actual = struct.unpack('<i', uc.mem_read(data+0x4000, 4))[0]*100
        assert actual == expected, (hex(raw), shunt, gain, actual, expected)
        assert events == ['pre', 0xd12, 0xd14, 'post']
    print(f'PASS: {len(vectors)} captured stock ARM64 instruction replays', flush=True)


if args.replay_stock:
    replay_stock()


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


prelude = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef int32_t s32;
typedef uint64_t u64;
#define U64_MAX UINT64_MAX
typedef struct { int64_t value; } atomic64_t;
#define ATOMIC64_INIT(x) { (x) }
static int64_t atomic64_inc_return(atomic64_t *v) { return __atomic_add_fetch(&v->value,1,__ATOMIC_SEQ_CST); }
#define lockdep_assert_held(m) assert((m)->owner == thread_id)

typedef int64_t ktime_t;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define FIELD_GET(mask,v) (((v)&(mask)) >> __builtin_ctz(mask))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define div_u64(n,d) ((u64)(n)/(d))
#define do_div(n,d) ((n)/=(d))
#define bm_debug(...) ((void)0)
#define dev_err_ratelimited(...) ((void)0)
struct device { struct device *parent; void *driver_data; void *of_node; };
struct work_struct { void (*fn)(struct work_struct *); };
struct delayed_work { struct work_struct work; };
struct regmap { int unused; };
struct mutex { pthread_mutex_t raw; unsigned int owner; };
struct power_supply { void *drvdata; struct device dev; };
struct notifier_block { int (*notifier_call)(struct notifier_block *, unsigned long, void *); };
union power_supply_propval { int intval; };
enum power_supply_property {
    POWER_SUPPLY_PROP_PRESENT, POWER_SUPPLY_PROP_CURRENT_NOW,
    POWER_SUPPLY_PROP_CHARGE_COUNTER, POWER_SUPPLY_PROP_VOLTAGE_NOW,
    POWER_SUPPLY_PROP_TEMP, POWER_SUPPLY_PROP_CAPACITY, POWER_SUPPLY_PROP_STATUS,
};
struct power_supply_desc {
    void (*external_power_changed)(struct power_supply *);
    const char *name; int type;
    const enum power_supply_property *properties;
    unsigned int num_properties;
    int (*get_property)(struct power_supply *, enum power_supply_property, union power_supply_propval *);
};
#define POWER_SUPPLY_TYPE_BATTERY 1
static void *power_supply_get_drvdata(struct power_supply *p) { return p->drvdata; }
static _Thread_local unsigned int thread_id=1;
static _Thread_local bool holding;
static void mutex_lock(struct mutex *m)
{ assert(!holding); assert(!pthread_mutex_lock(&m->raw)); assert(!m->owner); m->owner=thread_id; holding=true; }
static void mutex_unlock(struct mutex *m)
{ assert(holding && m->owner == thread_id); m->owner=0; holding=false; assert(!pthread_mutex_unlock(&m->raw)); }
static struct mutex *lock_guard(struct mutex *m) { mutex_lock(m); return m; }
static void unlock_guard(struct mutex **m) { mutex_unlock(*m); }
#define guard(kind) struct mutex *held __attribute__((cleanup(unlock_guard))) = lock_guard
static u64 now;
static ktime_t ktime_get(void) { return now; }
static u64 ktime_get_boottime_ns(void) { return now * 1000; }
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
/* Literal register/bit values captured from the verified stock driver, independent
 * of production masks. Readiness snapshots both counter halves together. */
#define STOCK_ON 0xd08U
#define STOCK_CTRL 0xd0aU
#define STOCK_CLOCK 0xc0cU
#define STOCK_LOW 0xd12U
#define STOCK_HIGH 0xd14U
#define STOCK_CURRENT 0xd8aU
#define STOCK_PRE 1U
#define STOCK_CLEAR 8U
#define STOCK_READY 0x8000U
static struct mt6357_gauge gauge;
static struct regmap map;
static struct power_supply psy={.drvdata=&gauge};
static unsigned int regs[0x1000], errors[20000], nops, low_reads, high_reads, current_reads;
static unsigned int ready_delay, clear_delay, live_counter, live_current;
static u64 latch_at, release_at;
static bool start_pending, stop_pending, stall_start, stall_stop, effect_on_error, advance_live;
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
    assert(reg == STOCK_ON || reg == STOCK_CLOCK || reg == STOCK_CTRL ||
           reg == STOCK_LOW || reg == STOCK_HIGH || reg == STOCK_CURRENT);
    int ret=record('r',reg,0);
    if (reg == STOCK_CTRL) {
        if (start_pending && !stall_start && now>=latch_at) {
            regs[reg]|=STOCK_READY; start_pending=false;
            regs[STOCK_LOW]=live_counter&0xffff; regs[STOCK_HIGH]=live_counter>>16;
            regs[STOCK_CURRENT]=live_current;
        }
        if (stop_pending && !stall_stop && now>=release_at) {
            regs[reg]&=~STOCK_READY; stop_pending=false;
        }
    }
    if (reg == STOCK_LOW || reg == STOCK_HIGH || reg == STOCK_CURRENT) {
        assert((regs[STOCK_CTRL]&(STOCK_READY|STOCK_PRE|STOCK_CLEAR)) == (STOCK_READY|STOCK_PRE));
        if (reg == STOCK_LOW) { low_reads++; if (advance_live) live_counter+=0x10000; }
        else if (reg == STOCK_HIGH) high_reads++;
        else current_reads++;
    }
    *val=ret ? 0xfefefefe : regs[reg]; /* Failed reads may scribble their destination. */
    return ret;
}
static int regmap_write(struct regmap *m, unsigned int reg, unsigned int val)
{
    assert(m == &map && reg == STOCK_CTRL); /* Never reset the counter or configure charging. */
    int ret=record('w',reg,val);
    if (!ret || effect_on_error) {
        unsigned int old=regs[reg];
        assert((old&~(STOCK_PRE|STOCK_CLEAR|STOCK_READY)) == (val&~(STOCK_PRE|STOCK_CLEAR|STOCK_READY)));
        regs[reg]=(val&~STOCK_READY)|(old&STOCK_READY);
        if (!(old&STOCK_PRE) && (val&STOCK_PRE)) {
            assert(!(val&STOCK_CLEAR)); start_pending=true; latch_at=now+ready_delay;
        }
        if ((val&STOCK_CLEAR) && !(val&STOCK_PRE)) {
            start_pending=false; stop_pending=true; release_at=now+clear_delay;
            regs[STOCK_LOW]=regs[STOCK_HIGH]=regs[STOCK_CURRENT]=0xdead;
        }
    }
    return ret;
}
static int regmap_update_bits(struct regmap *m, unsigned int reg, unsigned int mask, unsigned int val)
{
    assert(reg == STOCK_CTRL && (mask == STOCK_PRE || mask == STOCK_CLEAR));
    unsigned int old; int ret=regmap_read(m,reg,&old); if (ret) return ret;
    unsigned int next=(old&~mask)|(val&mask);
    return old == next ? 0 : regmap_write(m,reg,next);
}
/* Other properties are exercised by test-mt6357-current.py, not these stubs. */
static int mt6357_gauge_read_status(struct mt6357_gauge *g, int *out) { return -ENODATA; }
static int mt6357_gauge_read_present(struct mt6357_gauge *g, int *out) { return -ENODATA; }
static int mt6357_gauge_read_voltage(struct mt6357_gauge *g, int *out) { return -ENODATA; }
static int mt6357_gauge_read_temperature(struct mt6357_gauge *g, int *out) { return -ENODATA; }
'''
body += model
for name in ('mt6357_gauge_convert', 'mt6357_gauge_convert_charge', 'mt6357_gauge_release',
             'mt6357_gauge_live_word', 'mt6357_gauge_init_fg', 'mt6357_gauge_latch_locked',
             'mt6357_gauge_read_raw', 'mt6357_gauge_read_current', 'mt6357_gauge_read_charge',
             'mt6357_gauge_get_property'):
    body += function(name, s)
body += s[s.index('static const enum power_supply_property mt6357_gauge_properties'):s.index('static int mt6357_gauge_get_channel')]
stock = r'''
struct mtk_gauge { struct regmap *regmap; struct { int r_fg_value, car_tune_value; } hw_status; };
struct mtk_gauge_sysfs_field_info { int unused; };
static unsigned int stock_sample;
static void pre_gauge_update(struct mtk_gauge *g) { }
static void post_gauge_update(struct mtk_gauge *g) { }
static int stock_regmap_read(struct regmap *m, unsigned int reg, unsigned int *val)
{
    assert(reg == STOCK_LOW || reg == STOCK_HIGH);
    *val=(reg == STOCK_LOW ? stock_sample : stock_sample>>16)&0xffff;
    return 0;
}
'''
for name in ('UNIT_FGCAR', 'PMIC_FG_CAR_15_00_ADDR', 'PMIC_FG_CAR_15_00_MASK',
             'PMIC_FG_CAR_15_00_SHIFT', 'PMIC_FG_CAR_31_16_ADDR',
             'PMIC_FG_CAR_31_16_MASK', 'PMIC_FG_CAR_31_16_SHIFT'):
    stock += re.search(r'^#define '+name+r'\s+[^\n]+', vendor, re.M)[0]+'\n'
stock += function('coulomb_get', vendor).replace('regmap_read(', 'stock_regmap_read(')
stock += 'struct vector { unsigned int raw, shunt, gain; int expected; };\n'
stock += 'static const struct vector vectors[] = {\n'
stock += ''.join(f'    {{{raw:#x}U, {shunt}U, {gain}U, {value}}},\n'
                 for raw, shunt, gain, value in vectors)+'};\n'
checks = r'''
static void setup(bool inherited, unsigned int sample)
{
    assert(!gauge.lock.owner && !holding);
    memset(regs,0,sizeof(regs)); memset(errors,0,sizeof(errors));
    nops=low_reads=high_reads=current_reads=0; now=0;
    regs[STOCK_ON]=1;
    regs[STOCK_CTRL]=0x5360|(inherited ? (STOCK_READY|STOCK_PRE) : 0);
    gauge.regmap=&map; gauge.shunt_uohms=10000; gauge.gain_permille=1000;
    gauge.needs_release=inherited; live_counter=sample; live_current=1;
    ready_delay=clear_delay=0;
    start_pending=stop_pending=stall_start=stall_stop=effect_on_error=advance_live=false;
}
static int read_charge(int *result)
{
    union power_supply_propval val={.intval=*result};
    int ret=mt6357_gauge_desc.get_property(&psy,POWER_SUPPLY_PROP_CHARGE_COUNTER,&val);
    *result=val.intval; assert(!holding); return ret;
}
static int expected(unsigned int sample, unsigned int shunt, unsigned int gain)
{
    struct mtk_gauge g={.hw_status={shunt/100,gain}};
    stock_sample=sample; int out=0x12345678;
    assert(!coulomb_get(&g,NULL,&out)); return out*100;
}
static void retry(void)
{
    memset(errors,0,sizeof(errors)); stall_start=stall_stop=effect_on_error=false;
    ready_delay=clear_delay=0; latch_at=release_at=now; nops=0;
    live_counter=0x24680000; advance_live=false;
    int value=123;
    assert(!read_charge(&value) && value == expected(live_counter,10000,1000));
    assert(!gauge.needs_release && regs[STOCK_CTRL] == 0x5360);
}
static void *worker(void *arg)
{
    thread_id=(uintptr_t)arg;
    for (unsigned int i=0; i<50; i++) {
        union power_supply_propval val={.intval=123};
        enum power_supply_property prop=i&1 ? POWER_SUPPLY_PROP_CURRENT_NOW : POWER_SUPPLY_PROP_CHARGE_COUNTER;
        assert(!mt6357_gauge_desc.get_property(&psy,prop,&val));
        assert(val.intval == (i&1 ? 300 : 100));
    }
    return NULL;
}
int main(void)
{
    (void)mt6357_live_generation; /* Used by the probe extension in the STATUS suite. */
    unsigned int comparisons=0, samples=0, failures=0;
    assert(!pthread_mutex_init(&gauge.lock.raw,NULL));
    setup(false,0);
    assert(mt6357_gauge_desc.num_properties == 3);
    assert(mt6357_gauge_desc.properties[2] == POWER_SUPPLY_PROP_CHARGE_COUNTER);
    for (unsigned int i=0; i<ARRAY_SIZE(vectors); i++) {
        const struct vector *v=&vectors[i];
        setup(i&1,v->raw); gauge.shunt_uohms=v->shunt; gauge.gain_permille=v->gain;
        int value=123;
        assert(!mt6357_gauge_convert_charge(&gauge,v->raw,&value) && value == v->expected);
        assert(expected(v->raw,v->shunt,v->gain) == v->expected);
        value=123; assert(!read_charge(&value) && value == v->expected);
        assert(low_reads == 1 && high_reads == 1 && !current_reads && !gauge.needs_release);
        samples++;
    }
    /* Every packed magnitude and sign, including zero codes and ignored low bits.
     * This oracle is the unmodified arithmetic from the hash-verified stock C. */
    setup(false,0);
    for (unsigned int field=0; field<0x200000; field++) {
        unsigned int raw=(field<<11)|(field&0x7ff);
        int value=123; assert(!mt6357_gauge_convert_charge(&gauge,raw,&value));
        assert(value == expected(raw,10000,1000)); comparisons++;
    }
    gauge.shunt_uohms=1; gauge.gain_permille=UINT_MAX;
    for (unsigned int sign=0; sign<2; sign++) {
        int value=123;
        unsigned int raw=sign ? 0x80000800 : 0x7ffff000;
        assert(mt6357_gauge_convert_charge(&gauge,raw,&value) == -ERANGE && value == 123);
        failures++;
    }
    /* A counter change between bus reads must not tear the latched pair. */
    const unsigned int raw[]={0,0x80000000,0xfffff800,0x7ffff800,0x1234ffff,0xffff0001};
    for (unsigned int i=0; i<ARRAY_SIZE(raw); i++)
    for (unsigned int inherited=0; inherited<2; inherited++)
    for (unsigned int delay=0; delay<4; delay++) {
        setup(inherited,raw[i]); ready_delay=delay*100; clear_delay=(3-delay)*100;
        advance_live=true; int value=123;
        assert(!read_charge(&value) && value == expected(raw[i],10000,1000));
        assert(live_counter == raw[i]+0x10000 && low_reads == 1 && high_reads == 1);
        assert(!gauge.needs_release && regs[STOCK_CTRL] == 0x5360); samples++;
    }
    for (unsigned int off=0; off<4; off++) {
        setup(true,123); if (!off) regs[STOCK_ON]=0; else regs[STOCK_CLOCK]=off<<3;
        int value=123;
        assert(read_charge(&value) == -EAGAIN && value == 123 && gauge.needs_release);
        assert(!low_reads && !high_reads);
        for (unsigned int i=0; i<nops; i++) assert(trace[i].op == 'r');
        samples++;
    }
    /* Inject failures at every normal bus operation, and any subsequent cleanup
     * operation. Failed writes can either take effect or leave hardware alone. */
    for (unsigned int inherited=0; inherited<2; inherited++) {
        setup(inherited,0x1234ffff); int value=123; assert(!read_charge(&value));
        unsigned int count=nops;
        for (unsigned int failed=0; failed<count; failed++)
        for (unsigned int effect=0; effect<2; effect++) {
            setup(inherited,0x1234ffff); errors[failed]=EIO; effect_on_error=effect;
            value=123; assert(read_charge(&value) == -EIO && value == 123);
            assert(!gauge.lock.owner && now <= 40200);
            unsigned int completed=nops;
            for (unsigned int second=failed+1; second<completed; second++) {
                setup(inherited,0x1234ffff);
                errors[failed]=EIO; errors[second]=ENXIO; effect_on_error=effect;
                value=123; assert(read_charge(&value) == -EIO && value == 123 && !gauge.lock.owner);
                retry(); failures++;
            }
            setup(inherited,0x1234ffff); errors[failed]=EIO; effect_on_error=effect;
            value=123; assert(read_charge(&value) == -EIO && value == 123);
            retry(); failures++;
        }
    }
    for (unsigned int which=0; which<3; which++) {
        setup(which == 2,0x1234ffff); stall_start=which == 0; stall_stop=which != 0;
        int value=123; assert(read_charge(&value) == -ETIMEDOUT && value == 123);
        assert(now >= 20000 && now <= 20200 && gauge.needs_release == (which != 0));
        retry(); failures++;
    }
    for (unsigned int clear=0; clear<2; clear++)
    for (unsigned int delay=19900; delay<=20200; delay+=100) {
        setup(false,0x1234ffff);
        if (clear) clear_delay=delay; else ready_delay=delay;
        int value=123, ret=read_charge(&value);
        assert(ret == (delay <= 20100 ? 0 : -ETIMEDOUT));
        assert(value == (ret ? 123 : expected(0x1234ffff,10000,1000)));
        assert(gauge.needs_release == (ret && clear)); retry(); failures++;
    }
    setup(true,0x1234ffff); stall_stop=true;
    for (unsigned int i=0; i<3; i++) {
        int value=123; assert(read_charge(&value) == -ETIMEDOUT && value == 123 && gauge.needs_release);
        assert(!low_reads && !high_reads); failures++;
    }
    retry();
    /* A failed high-half read cannot contaminate a following current transaction. */
    setup(false,0x1234ffff); int value=123; assert(!read_charge(&value));
    unsigned int high_op=0;
    for (unsigned int i=0; i<nops; i++) if (trace[i].reg == STOCK_HIGH) high_op=i;
    assert(high_op);
    setup(false,0x1234ffff); errors[high_op]=EIO;
    value=123; assert(read_charge(&value) == -EIO && value == 123);
    assert(low_reads == 1 && high_reads == 1 && !gauge.needs_release);
    memset(errors,0,sizeof(errors));
    assert(!mt6357_gauge_read_current(&gauge,&value) && value == 300); failures++;
    setup(false,1); union power_supply_propval val={.intval=123};
    assert(mt6357_gauge_desc.get_property(&psy,POWER_SUPPLY_PROP_CAPACITY,&val) == -EINVAL);
    assert(val.intval == 123 && !nops);
    /* Current and counter readers must serialize the same measurement latch. */
    setup(true,0x4800); pthread_t threads[4];
    for (uintptr_t i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_create(&threads[i],NULL,worker,(void *)(i+2)));
    for (unsigned int i=0; i<ARRAY_SIZE(threads); i++) assert(!pthread_join(threads[i],NULL));
    assert(!gauge.lock.owner && !gauge.needs_release);
    assert(low_reads == 100 && high_reads == 100 && current_reads == 100);
    unsigned int transactions=0;
    for (unsigned int i=0; i<nops;) {
        assert(trace[i].op == 'r' && trace[i].reg == STOCK_ON);
        unsigned int owner=trace[i++].thread;
        while (i<nops && trace[i].reg != STOCK_ON) { assert(trace[i].thread == owner); i++; }
        transactions++;
    }
    assert(transactions == 200 && !pthread_mutex_destroy(&gauge.lock.raw));
    printf("PASS: %zu captured stock vectors, %u exhaustive stock-C comparisons, %u counter acquisitions, %u fault/range/recovery cases, 200 mixed threaded reads\n",
           ARRAY_SIZE(vectors),comparisons,samples,failures);
    return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+body+stock+checks)
command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
           '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-const-variable',
           '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
           '-fno-pie', '-no-pie', str(path), '-o', str(OUT/'host')]
subprocess.run(command, check=True)
result = subprocess.run([str(OUT/'host')], check=True, timeout=45, text=True, capture_output=True)
print(result.stdout, end='')
(OUT/'audit.json').write_text(json.dumps(dict(
    hardware_tested=False, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    fixture_sha256=hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
    host_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    compiler_command=command, test_command=[str(OUT/'host')],
    captured_stdout=result.stdout, captured_stderr=result.stderr, exit_code=result.returncode,
    stock_instruction_replays=len(vectors) if args.replay_stock else 0,
    scope='Actual production counter/current conversion and shared acquisition/property functions; captured stock ARM64 arithmetic oracle, exhaustive stock-C arithmetic, modeled PMIC regmap and pthread locks, actual kernel polling macros. No electrical, reset/rollover tracking or capacity validation.'
), indent=2)+'\n')
