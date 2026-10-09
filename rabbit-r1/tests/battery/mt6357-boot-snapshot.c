/* Host boundaries for production MFD probe/capture/getter, decoder and parser. */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
typedef int64_t s64;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define GFP_KERNEL 0
#define PLATFORM_DEVID_NONE (-1)
#define EXPORT_SYMBOL_GPL(x)
#define dev_err(...) ((void)0)
#define dev_info(...) (logs++)
struct mutex { pthread_mutex_t raw; };
struct notifier_block { int unused; };
struct irq_domain { int unused; };
struct regmap { int unused; };
struct device_driver { int unused; };
struct device { struct mutex lock; struct device_driver *driver; void *drvdata; struct device *parent; };
struct platform_device { struct device dev; };
struct platform_driver { struct device_driver driver; };
struct mfd_cell { int unused; };
/* INSERT TYPES HERE */
static struct platform_driver mt6397_driver;
static const struct chip_data mt6323_core,mt6328_core,mt6331_mt6332_core,mt6357_core,mt6358_core,mt6359_core,mt6397_core;
static const struct mfd_cell mt6323_devs[1],mt6328_devs[1],mt6331_mt6332_devs[1],mt6357_devs[1],mt6358_devs[1],mt6359_devs[1],mt6397_devs[1];
static const struct chip_data *match;
static struct device transport;
static struct platform_device pdev={.dev={.parent=&transport}};
static struct regmap map;
static struct mt6397_chip *allocation;
static unsigned int regs[0x10000],reads,writes,logs,children,irqs,domain_removals;
static unsigned int fail_reads;
static bool no_alloc,no_map,no_match,child_reset;
static int irq_result,irq_init_result,child_result;
static u64 now;
static struct mt6357_boot_snapshot at_child_registration;
static unsigned int read_addresses[8];
static pthread_mutex_t control=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t condition=PTHREAD_COND_INITIALIZER;
static bool pause_getter,getter_entered,release_getter;
static atomic_bool removal_started,removed;
static atomic_uint measurement_changes;
static _Thread_local bool getter_thread;
static int mt6359_auxadc_reset(struct mt6359_auxadc *adc);
static int mt6357_auxadc_restore_requests(struct mt6359_auxadc *adc);
static const struct mtk_pmic_auxadc_info adc_info;
static u64 ktime_get_boottime_ns(void) { return ++now; }
static int device_trylock(struct device *dev)
{ int n=pthread_mutex_trylock(&dev->lock.raw); assert(!n || n==EBUSY); return !n; }
static void device_unlock(struct device *dev) { assert(!pthread_mutex_unlock(&dev->lock.raw)); }
static void *dev_get_drvdata(struct device *dev)
{
    assert(!pthread_mutex_lock(&control));
    if (getter_thread && pause_getter) {
        getter_entered=true; pthread_cond_broadcast(&condition);
        while (!release_getter) pthread_cond_wait(&condition,&control);
    }
    assert(!pthread_mutex_unlock(&control));
    return dev->drvdata;
}
static void platform_set_drvdata(struct platform_device *p,void *value) { p->dev.drvdata=value; }
static void *devm_kzalloc(struct device *dev,size_t len,int flags)
{ (void)dev;(void)flags; assert(!allocation); if (no_alloc) return NULL; allocation=calloc(1,len); assert(allocation); return allocation; }
static struct regmap *dev_get_regmap(struct device *dev,const void *name)
{ assert(dev==&transport && !name); return no_map?NULL:&map; }
static const void *of_device_get_match_data(struct device *dev)
{ assert(dev==&pdev.dev); return no_match?NULL:match; }
static int regmap_read(struct regmap *m,unsigned int reg,unsigned int *value)
{
    assert(m==&map && reg<ARRAY_SIZE(regs)); assert(reads<ARRAY_SIZE(read_addresses));
    read_addresses[reads++]=reg;now+=10;
    if (fail_reads & BIT(reads)) { *value=0xfeedface;return -EREMOTEIO; }
    *value=regs[reg];return 0;
}
static int regmap_write(struct regmap *m,unsigned int reg,unsigned int value)
{ assert(m==&map && reg<ARRAY_SIZE(regs));writes++;regs[reg]=value;return 0; }
static int regmap_set_bits(struct regmap *m,unsigned int reg,unsigned int mask)
{
    /* Model possible destruction of captures by an ADC reset; not a silicon claim. */
    if (reg==0xf90) { regs[0x10ac]=0;regs[0x10c0]=0; }
    return regmap_write(m,reg,regs[reg]|mask);
}
static int regmap_clear_bits(struct regmap *m,unsigned int reg,unsigned int mask)
{ return regmap_write(m,reg,regs[reg]&~mask); }
static int platform_get_irq(struct platform_device *p,unsigned int index)
{ assert(p==&pdev && !index);return irq_result; }
static int mt6358_irq_init(struct mt6397_chip *p)
{ assert(p==allocation);irqs++;return irq_init_result; }
static int mt6397_irq_init(struct mt6397_chip *p) { return mt6358_irq_init(p); }
static void irq_domain_remove(struct irq_domain *d) { (void)d;domain_removals++; }
static int devm_mfd_add_devices(struct device *dev,int id,const struct mfd_cell *cells,
                              int count,void *mem,int offset,struct irq_domain *domain)
{
    struct mt6397_chip *p=dev->drvdata;
    struct mt6357_boot_snapshot untouched;
    assert(dev==&pdev.dev && id==PLATFORM_DEVID_NONE && cells==match->cells);
    assert(count==match->cell_size && !mem && !offset && domain==p->irq_domain);
    assert(irqs==1 && !writes);children++;
    if (match==&mt6357_core && p->chip_id==0x57) {
        assert(reads==6 && p->boot_snapshot.observed && logs==1);
        at_child_registration=p->boot_snapshot;
        memset(&untouched,0xa5,sizeof(untouched));
        struct mt6357_boot_snapshot previous=untouched;
        assert(mt6357_boot_snapshot_get(dev,&untouched)==-EAGAIN);
        assert(!memcmp(&untouched,&previous,sizeof(previous)));
        if (child_reset) {
            struct mt6359_auxadc adc={.regmap=&map,.chip_info=&adc_info};
            assert(!mt6359_auxadc_reset(&adc));assert(!adc.needs_reset);
            assert(!regs[0x10ac] && !regs[0x10c0] && writes==4);
        }
        assert(!memcmp(&p->boot_snapshot,&at_child_registration,sizeof(at_child_registration)));
    } else { assert(reads==1 && !logs && !p->boot_snapshot.observed); }
    return child_result;
}
/* INSERT PRODUCTION HERE */
static const struct mtk_pmic_auxadc_info adc_info={.regs=mt6357_auxadc_regs,.restore_requests=mt6357_auxadc_restore_requests};
static void release_resources(void)
{
    /* Core removal owns the device lock until the parent driver is unbound. */
    assert(!pthread_mutex_lock(&pdev.dev.lock.raw));
    pdev.dev.driver=NULL;
    free(allocation);allocation=NULL;
    /* Deliberately leave a stale pointer, proving the driver gate precedes drvdata. */
    pdev.dev.drvdata=(void *)(uintptr_t)1;
    assert(!pthread_mutex_unlock(&pdev.dev.lock.raw));
}
static void reset_fixture(void)
{
    if (allocation) release_resources();
    memset(regs,0,sizeof(regs));memset(read_addresses,0,sizeof(read_addresses));
    reads=writes=logs=children=irqs=domain_removals=fail_reads=0;
    no_alloc=no_map=no_match=child_reset=false;irq_result=7;irq_init_result=child_result=0;
    match=&mt6357_core;regs[0xa]=0x5712;
    for (unsigned int i=0;i<5;i++) regs[stock_registers[i]]=0x8123+i*7;
    pdev.dev.driver=&mt6397_driver.driver;pdev.dev.drvdata=NULL;
}
static int probe(void)
{
    assert(!pthread_mutex_lock(&pdev.dev.lock.raw));
    int ret=mt6397_probe(&pdev);
    if (ret) { pdev.dev.driver=NULL;pdev.dev.drvdata=NULL; }
    assert(!pthread_mutex_unlock(&pdev.dev.lock.raw));
    return ret;
}
static void check_probe(void)
{
    unsigned int cases=0;
    reset_fixture();child_reset=true;assert(!probe());cases++;
    assert(read_addresses[0]==0xa);
    assert(!memcmp(read_addresses+1,stock_registers,sizeof(stock_registers)));
    struct mt6357_boot_snapshot copy;
    assert(!mt6357_boot_snapshot_get(&pdev.dev,&copy));
    assert(copy.swcid==0x5712 && copy.started_ns<copy.finished_ns);
    assert(copy.finished_ns-copy.started_ns==51 && copy.words[0].raw==0x8123);
    copy.words[0].raw=0xffff;
    assert(allocation->boot_snapshot.words[0].raw==0x8123);
    for (unsigned int i=0;i<100;i++) {
        mt6357_boot_capture(allocation,0xffff);
        assert(!mt6357_boot_snapshot_get(&pdev.dev,&copy));
        assert(!memcmp(&copy,&at_child_registration,sizeof(copy)));
    }
    assert(reads==6 && logs==1 && writes==4);
    /* Child reprobes rerun the production ADC reset, never the parent capture. */
    for (unsigned int i=0;i<5;i++) {
        struct mt6359_auxadc rebound={.regmap=&map,.chip_info=&adc_info};
        assert(!mt6359_auxadc_reset(&rebound));
        assert(!mt6357_boot_snapshot_get(&pdev.dev,&copy));
        assert(!memcmp(&copy,&at_child_registration,sizeof(copy)));
    }
    assert(reads==6 && logs==1 && writes==24);
    /* Ordinary telemetry failures preserve each errno, unrelated children and interval. */
    for (unsigned int mask=1;mask<32;mask++) {
        reset_fixture();fail_reads=mask<<2;assert(!probe());cases++;
        assert(reads==6 && children==1 && !writes);
        assert(!mt6357_boot_snapshot_get(&pdev.dev,&copy));
        for (unsigned int i=0;i<5;i++) {
            assert(copy.words[i].error==((mask&BIT(i))?-EREMOTEIO:0));
            assert(copy.words[i].raw==((mask&BIT(i))?0:0x8123+i*7));
        }
    }
    for (unsigned int kind=0;kind<7;kind++) {
        reset_fixture();
        const struct chip_data *cores[]={&mt6323_core,&mt6328_core,&mt6331_mt6332_core,&mt6358_core,&mt6359_core,&mt6397_core,&mt6357_core};
        match=cores[kind];regs[match->cid_addr]=(kind==6?0x5801:0x5701);
        assert(!probe());assert(reads==1 && !logs && children==1);cases++;
        assert(mt6357_boot_snapshot_get(&pdev.dev,&copy)==-ENODEV);
    }
    for (unsigned int which=0;which<7;which++) {
        reset_fixture();
        int expected;
        switch (which) {
        case 0:no_alloc=true;expected=-ENOMEM;break;
        case 1:no_map=true;expected=-ENODEV;break;
        case 2:no_match=true;expected=-ENODEV;break;
        case 3:fail_reads=BIT(1);expected=-EREMOTEIO;break;
        case 4:irq_result=-ENXIO;expected=-ENXIO;break;
        case 5:irq_init_result=-EIO;expected=-EIO;break;
        default:child_result=-ENOMEM;expected=-ENOMEM;break;
        }
        assert(probe()==expected);cases++;
        assert(reads==(which<3?0:which==3?1:6));
        assert(!writes && domain_removals==(which==6));
        assert(mt6357_boot_snapshot_get(&pdev.dev,&copy)==-ENODEV);
    }
    printf("PASS: %u full production probe cases; identity gating, all 31 error masks, pre-child capture and actual ADC reset\n",cases);
}
static void check_decode(void)
{
    struct mt6357_boot_ocv value,old;
    for (unsigned int raw=0;raw<=65535;raw++) {
        struct mt6357_boot_word word={.raw=raw};memset(&value,0xa5,sizeof(value));old=value;
        int ret=mt6357_boot_decode_ocv(&word,&value);
        if (!(raw&0x8000)) { assert(ret==-ENODATA && !memcmp(&value,&old,sizeof(value))); }
        else { assert(!ret && value.code==(raw&0x7fff) && value.microvolts==stock_conversion[raw&0x7fff]*100); }
        word.error=-EREMOTEIO;value=old;
        assert(mt6357_boot_decode_ocv(&word,&value)==-EREMOTEIO && !memcmp(&value,&old,sizeof(value)));
    }
    struct mt6357_boot_snapshot s={0};struct mt6357_boot_metadata v,prior;
    memset(&v,0xa5,sizeof(v));prior=v;
    assert(mt6357_boot_decode_metadata(&s,&v)==-ENODATA && !memcmp(&v,&prior,sizeof(v)));
    s.observed=true;
    for (unsigned int raw=0;raw<=65535;raw++) {
        s.words[MT6357_BOOT_SYSTEM_INFO].raw=raw;
        s.words[MT6357_BOOT_STARTUP].raw=raw;
        s.words[MT6357_BOOT_BATTERY_STATUS].raw=raw;
        assert(!mt6357_boot_decode_metadata(&s,&v));
        assert(v.startup_select==!!(raw&4) && v.reboot_2sec==!!(raw&1));
        assert(v.preloader_charging==!!(raw&2) && v.monitor_preloader_charging==!!(raw&4));
        assert(v.battery_plug==!!(raw&8) && v.nvram_fail==!!(raw&16));
        assert(v.monitor_shutdown_valid_time==!!(raw&32) && v.stored_soc==((raw>>9)&127));
        assert(v.battery_present==!(raw&2));
    }
    for (unsigned int field=2;field<5;field++) {
        s.words[field].error=-EIO;memset(&v,0xa5,sizeof(v));prior=v;
        assert(mt6357_boot_decode_metadata(&s,&v)==-EIO && !memcmp(&v,&prior,sizeof(v)));
        s.words[field].error=0;
    }
    printf("PASS: 65536 capture words against all 32768 stock instruction vectors, 65536 metadata words, error outputs preserved\n");
}
static void check_parser(void)
{
    unsigned int count=0;
    for (unsigned int i=0;i<ARRAY_SIZE(lk_vectors);i++) {
        const struct lk_vector *v=&lk_vectors[i];s32 out=123;
        assert(mt6357_boot_parse_lk_decimal(v->data,v->len,&out)==v->error);
        assert(out==(v->error?123:v->expected));count++;
    }
    assert(mt6357_boot_parse_lk_decimal(NULL,1,&(s32){0})==-EINVAL);
    assert(mt6357_boot_parse_lk_decimal("0",1,NULL)==-EINVAL);
    /* Exact-size allocations expose overreads at every accepted/rejected length. */
    for (unsigned int len=1;len<=16;len++) {
        u8 *bytes=malloc(len);assert(bytes);memset(bytes,'0',len);s32 out=99;
        int ret=mt6357_boot_parse_lk_decimal(bytes,len,&out);
        assert(ret==(len<=11?0:-EINVAL));assert(out==(ret?99:0));free(bytes);count++;
    }
    printf("PASS: %u decimal parser vectors including 24 actual LK property payloads, bounds, signs, malformed bytes and overflow\n",count);
}
static struct mt6357_boot_snapshot raced_copy;
static void *read_paused(void *arg)
{ (void)arg;getter_thread=true;assert(!mt6357_boot_snapshot_get(&pdev.dev,&raced_copy));return NULL; }
static void *remove_parent(void *arg)
{ (void)arg;atomic_store(&removal_started,true);release_resources();atomic_store(&removed,true);return NULL; }
static void *change_live_measurements(void *arg)
{
    (void)arg;
    for (unsigned int i=0;i<20000;i++) { regs[0x10ac]=i;regs[0x10c0]=~i;atomic_fetch_add(&measurement_changes,1); }
    return NULL;
}
static void check_lifetime(void)
{
    struct mt6357_boot_snapshot before,copy;
    reset_fixture();assert(!probe());assert(!mt6357_boot_snapshot_get(&pdev.dev,&before));
    pthread_t reader,remover,measurements;
    assert(!pthread_create(&measurements,NULL,change_live_measurements,NULL));
    for (unsigned int i=0;i<20000;i++) {
        assert(!mt6357_boot_snapshot_get(&pdev.dev,&copy));assert(!memcmp(&copy,&before,sizeof(copy)));
    }
    assert(!pthread_join(measurements,NULL));assert(reads==6 && !writes && atomic_load(&measurement_changes)==20000);
    pause_getter=true;getter_entered=release_getter=false;atomic_store(&removed,false);atomic_store(&removal_started,false);
    assert(!pthread_create(&reader,NULL,read_paused,NULL));
    pthread_mutex_lock(&control);
    while (!getter_entered) pthread_cond_wait(&condition,&control);
    pthread_mutex_unlock(&control);
    assert(!pthread_create(&remover,NULL,remove_parent,NULL));
    while (!atomic_load(&removal_started)) sched_yield();
    assert(!atomic_load(&removed));
    /* A separate caller during the entered callback cannot wait on removal. */
    memset(&copy,0xa5,sizeof(copy));struct mt6357_boot_snapshot poison=copy;
    assert(mt6357_boot_snapshot_get(&pdev.dev,&copy)==-EAGAIN && !memcmp(&copy,&poison,sizeof(copy)));
    pthread_mutex_lock(&control);release_getter=true;pthread_cond_broadcast(&condition);pthread_mutex_unlock(&control);
    assert(!pthread_join(reader,NULL));assert(!pthread_join(remover,NULL));
    assert(atomic_load(&removed) && !memcmp(&raced_copy,&before,sizeof(before)));
    for (unsigned int i=0;i<100;i++) assert(mt6357_boot_snapshot_get(&pdev.dev,&copy)==-ENODEV);
    static struct device_driver foreign;
    pdev.dev.driver=&foreign;assert(mt6357_boot_snapshot_get(&pdev.dev,&copy)==-ENODEV);
    reset_fixture();regs[0x10ac]=0xabcd;assert(!probe());assert(!mt6357_boot_snapshot_get(&pdev.dev,&copy));
    assert(copy.started_ns>before.finished_ns && copy.words[0].raw==0xabcd && before.words[0].raw==0x8123);
    assert(mt6357_boot_snapshot_get(NULL,&copy)==-EINVAL && mt6357_boot_snapshot_get(&pdev.dev,NULL)==-EINVAL);
    printf("PASS: 20000 immutable copies during live measurement changes, entered getter/removal race, 100 late calls, foreign bind and new observation on rebind\n");
}
int main(void)
{
    assert(!pthread_mutex_init(&pdev.dev.lock.raw,NULL));
    check_decode();check_parser();check_probe();check_lifetime();
    release_resources();assert(!pthread_mutex_destroy(&pdev.dev.lock.raw));
    return 0;
}
