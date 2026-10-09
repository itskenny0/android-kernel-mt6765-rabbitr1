/* Host boundary model for actual RTC driver functions. */
#include <assert.h>
#include <errno.h>
#include <limits.h>
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
typedef int64_t ktime_t;
#define BIT(n) (1U << (n))
#define HZ 100
#define jiffies_to_usecs(n) ((n)*10000U)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define PTR_ERR(p) ((intptr_t)(p))
#define IS_ENABLED(x) (x)
#define GFP_KERNEL 0
#define IORESOURCE_MEM 1
#define IRQF_ONESHOT 1
#define IRQF_TRIGGER_HIGH 2
#define IRQ_HANDLED 1
#define IRQ_NONE 0
#define RTC_IRQF 0x80
#define RTC_AF 0x20
#define RTC_TIMESTAMP_BEGIN_1900 (-2208988800LL)
#define dev_err(...) ((void)0)
typedef int irqreturn_t;
struct mutex { pthread_mutex_t raw; unsigned int owner; };
struct device_driver { int unused; };
struct device { struct mutex mutex; struct device *parent; struct device_driver *driver; void *drvdata; };
struct platform_device { struct device dev; };
struct platform_driver { struct device_driver driver; };
struct regmap { int unused; };
struct mt6397_chip { struct regmap *regmap; };
struct resource { unsigned int start; };
struct rtc_time { int tm_sec,tm_min,tm_hour,tm_mday,tm_wday,tm_mon,tm_year; };
struct rtc_wkalrm { struct rtc_time time; bool enabled,pending; };
struct rtc_class_ops {
    int (*read_time)(struct device *,struct rtc_time *);
    int (*set_time)(struct device *,struct rtc_time *);
    int (*read_alarm)(struct device *,struct rtc_wkalrm *);
    int (*set_alarm)(struct device *,struct rtc_wkalrm *);
};
struct rtc_device { struct device dev; const struct rtc_class_ops *ops; int64_t range_min,range_max,start_secs; bool set_start_time; };
struct nvmem_config { int size,word_size,stride; bool read_only,ignore_wp;
    int (*reg_read)(void *,unsigned int,void *,size_t);
    int (*reg_write)(void *,unsigned int,void *,size_t); void *priv; };
static _Thread_local unsigned int thread_id=1;
static u64 now;
static ktime_t ktime_get(void) { return now; }
static ktime_t ktime_add_us(ktime_t t, u64 n) { return t+n; }
static int ktime_compare(ktime_t a, ktime_t b) { return (a>b)-(a<b); }
static void usleep_range(unsigned int lo, unsigned int hi) { assert(lo && lo<=hi); now+=hi; }
#define might_sleep_if(x) ((void)0)
#define barrier() asm volatile("" ::: "memory")
#define cpu_relax() sched_yield()
/* INSERT PRODUCTION HERE */
static struct platform_driver mtk_rtc_driver;
static const struct mtk_rtc_data mt6357_rtc_data,mt6358_rtc_data,mt6397_rtc_data;
static struct regmap map;
static struct mt6397_chip pmic={.regmap=&map};
static struct device pmic_dev={.drvdata=&pmic};
static struct platform_device pdev={.dev={.parent=&pmic_dev}};
static struct mt6397_rtc *rtc;
static struct rtc_device rtc_dev;
static struct resource resource={.start=STOCK_BASE};
static struct nvmem_config provider;
static bool rtc_mutex_live, rtc_registered, nvmem_registered, irq_registered;
static bool no_resource,no_match,alloc_fail,rtc_alloc_fail;
static int match_kind, irq_error, request_error,rtc_register_error,nvmem_register_error;
static unsigned int nvmem_registrations,rtc_registrations;
static bool watch_transactions;
static _Thread_local bool reading_nvmem;
static unsigned int regs[0x1000],shadow[0x1000],reads,writes,fail_read,fail_write;
static int read_error=-EIO,write_error=-EIO;
static bool dirty[0x1000];
static pthread_mutex_t control=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t condition=PTHREAD_COND_INITIALIZER;
static bool block_nvmem, nvmem_entered, release_nvmem;
static bool block_alarm, alarm_entered, release_alarm, nvmem_waiting_rtc;
static atomic_bool removal_started,removed;
struct event { unsigned int thread,reg; bool write; };
static struct event events[40000];
static unsigned int nevents;
static void mutex_init(struct mutex *m)
{ assert(!pthread_mutex_init(&m->raw,NULL)); m->owner=0; if (rtc && m==&rtc->lock) rtc_mutex_live=true; }
static void mutex_lock(struct mutex *m)
{
    if (rtc && m==&rtc->lock && reading_nvmem) {
        assert(!pthread_mutex_lock(&control));
        nvmem_waiting_rtc=true; pthread_cond_broadcast(&condition);
        assert(!pthread_mutex_unlock(&control));
    }
    assert(!pthread_mutex_lock(&m->raw)); assert(!m->owner); m->owner=thread_id;
}
static void mutex_unlock(struct mutex *m)
{ assert(m->owner==thread_id); m->owner=0; assert(!pthread_mutex_unlock(&m->raw)); }
static int device_trylock(struct device *d)
{
    int ret=pthread_mutex_trylock(&d->mutex.raw);
    if (ret==EBUSY) return 0;
    assert(!ret && !d->mutex.owner); d->mutex.owner=thread_id; return 1;
}
static void device_unlock(struct device *d) { mutex_unlock(&d->mutex); }
static void *dev_get_drvdata(struct device *d)
{
    if (reading_nvmem) assert(d==&pdev.dev && d->mutex.owner==thread_id);
    return d->drvdata;
}
static void platform_set_drvdata(struct platform_device *p,void *data) { p->dev.drvdata=data; }
static void record(unsigned int reg,bool write)
{
    assert(rtc && rtc_mutex_live && rtc->lock.owner==thread_id);
    if (reading_nvmem) assert(!write && (reg==STOCK_SPARE_0 || reg==STOCK_SPARE_1));
    if (watch_transactions) { assert(nevents<ARRAY_SIZE(events));events[nevents++]=(struct event){thread_id,reg,write}; }
    sched_yield();
}
static int regmap_read(struct regmap *m,unsigned int reg,unsigned int *value)
{
    assert(m==&map && reg>=STOCK_BASE && reg<STOCK_BASE+0x40); record(reg,false);
    if (reading_nvmem && block_nvmem) {
        assert(!pthread_mutex_lock(&control));
        nvmem_entered=true; pthread_cond_broadcast(&condition);
        while (!release_nvmem) assert(!pthread_cond_wait(&condition,&control));
        assert(!pthread_mutex_unlock(&control));
    }
    int ret=++reads==fail_read ? read_error : 0;
    *value=ret ? 0xfefefefe : regs[reg]; return ret;
}
static int regmap_write(struct regmap *m,unsigned int reg,unsigned int value)
{
    assert(m==&map && !reading_nvmem);record(reg,true);
    int ret=++writes==fail_write ? write_error : 0; if (ret) return ret;
    if (reg==STOCK_BASE+rtc->data->wrtgr) {
        assert(value==1);
        for (unsigned int i=STOCK_BASE;i<STOCK_BASE+0x40;i++) if (dirty[i]) {regs[i]=shadow[i];dirty[i]=false;}
        regs[STOCK_BASE]=0; /* WRTGR completion visible to the real polling macro. */
    } else {shadow[reg]=value;dirty[reg]=true;}
    return 0;
}
static int regmap_bulk_read(struct regmap *m,unsigned int start,void *buf,size_t count)
{
    u16 *values=buf;
    for (unsigned int i=0;i<count;i++) {unsigned int value;int ret=regmap_read(m,start+2*i,&value);if(ret)return ret;values[i]=value;}
    return 0;
}
static int regmap_bulk_write(struct regmap *m,unsigned int start,const void *buf,size_t count)
{
    const u16 *values=buf;
    for (unsigned int i=0;i<count;i++) {int ret=regmap_write(m,start+2*i,values[i]);if(ret)return ret;}
    if (block_alarm) {
        assert(!pthread_mutex_lock(&control));
        alarm_entered=true;pthread_cond_broadcast(&condition);
        while (!release_alarm) assert(!pthread_cond_wait(&condition,&control));
        assert(!pthread_mutex_unlock(&control));
    }
    return 0;
}
static int regmap_update_bits(struct regmap *m,unsigned int reg,unsigned int mask,unsigned int value)
{ unsigned int old;int ret=regmap_read(m,reg,&old);return ret ? ret : regmap_write(m,reg,(old&~mask)|(value&mask)); }
static void rtc_update_irq(struct rtc_device *d,unsigned int count,unsigned int flags) { assert(false); }
static void *devm_kzalloc(struct device *d,size_t size,int flags)
{
    assert(size==sizeof(*rtc));if(alloc_fail)return NULL;
    rtc=calloc(1,size);assert(rtc);return rtc;
}
static struct resource *platform_get_resource(struct platform_device *p,int type,unsigned int index)
{ assert(type==IORESOURCE_MEM && !index);return no_resource ? NULL : &resource; }
static const void *of_device_get_match_data(struct device *d)
{ return no_match ? NULL : match_kind==0 ? &mt6357_rtc_data : match_kind==1 ? &mt6358_rtc_data : &mt6397_rtc_data; }
static int platform_get_irq(struct platform_device *p,unsigned int index)
{ assert(!index);return irq_error ? irq_error : 42; }
static struct rtc_device *devm_rtc_allocate_device(struct device *d)
{ if(rtc_alloc_fail)return ERR_PTR(-ENOMEM);memset(&rtc_dev,0,sizeof(rtc_dev));rtc_dev.dev.parent=d;return &rtc_dev; }
static int devm_request_threaded_irq(struct device *d,int irq,void *handler,
    irqreturn_t (*thread)(int,void *),unsigned int flags,const char *name,void *data)
{
    assert(irq==42 && !handler && thread && data==rtc && flags==(IRQF_ONESHOT|IRQF_TRIGGER_HIGH));
    if(request_error)return request_error;
    irq_registered=true;return 0;
}
static void device_init_wakeup(struct device *d,bool enabled) { assert(enabled); }
static int64_t mktime64(int year,int month,int day,int hour,int minute,int second) { return year; }
static int devm_rtc_register_device(struct rtc_device *d)
{
    assert(d==&rtc_dev && irq_registered && rtc_mutex_live && d->ops && d->set_start_time);
    if(rtc_register_error)return rtc_register_error;
    rtc_registered=true;rtc_registrations++;return 0;
}
static int devm_rtc_nvmem_register(struct rtc_device *d,struct nvmem_config *config)
{
    assert(CONFIG_RTC_NVMEM && d==&rtc_dev && rtc_registered && match_kind==0);
    assert(config->size==2 && config->stride==1 && config->word_size==1);
    assert(config->read_only && config->ignore_wp && !config->reg_write);
    assert(config->priv==&pdev.dev && config->priv!=(void *)rtc && config->reg_read);
    provider=*config;
    /* The new sysfs callback may run during probe, which owns device_lock. */
    u8 value=0xa5;unsigned int before=reads;
    assert(provider.reg_read(provider.priv,0,&value,1)==-EAGAIN && value==0xa5 && reads==before);
    if(nvmem_register_error)return nvmem_register_error;
    nvmem_registered=true;nvmem_registrations++;return 0;
}
/* INSERT PRODUCTION HERE */
static void cleanup_locked(void)
{
    assert(pdev.dev.mutex.owner==thread_id);
    /* Managed NVMEM unregister occurs before RTC unregister/IRQ/private free.
     * A held NVMEM consumer reference retains the parent device and callback,
     * not the devres allocation; provider is intentionally left callable. */
    nvmem_registered=false;rtc_registered=false;irq_registered=false;
    if(rtc_mutex_live) {assert(!pthread_mutex_destroy(&rtc->lock.raw));rtc_mutex_live=false;}
    free(rtc);rtc=NULL;
    /* Match real core ordering: private devres can be gone before driver=NULL. */
    pdev.dev.drvdata=(void *)(uintptr_t)0x1;
    if(provider.reg_read) {u8 value=0xa5;assert(provider.reg_read(provider.priv,0,&value,1)==-EAGAIN && value==0xa5);}
    pdev.dev.driver=NULL;
}
static void reset(void)
{
    mutex_lock(&pdev.dev.mutex);cleanup_locked();device_unlock(&pdev.dev);
    memset(&provider,0,sizeof(provider));
    memset(regs,0,sizeof(regs));memset(dirty,0,sizeof(dirty));
    regs[STOCK_SPARE_0]=0xab17;regs[STOCK_SPARE_1]=0xcd0c;
    reads=writes=fail_read=fail_write=nevents=0;now=0;
    no_resource=no_match=alloc_fail=rtc_alloc_fail=false;
    irq_error=request_error=rtc_register_error=nvmem_register_error=0;
    match_kind=0;nvmem_registrations=rtc_registrations=0;
    block_nvmem=nvmem_entered=release_nvmem=false;
    block_alarm=alarm_entered=release_alarm=nvmem_waiting_rtc=false;
    watch_transactions=false;removal_started=removed=false;
    pmic.regmap=&map;pmic_dev.drvdata=&pmic;
}
static int probe(void)
{
    mutex_lock(&pdev.dev.mutex);pdev.dev.driver=&mtk_rtc_driver.driver;
    int ret=mtk_rtc_probe(&pdev);
    if(ret)cleanup_locked();
    device_unlock(&pdev.dev);return ret;
}
static int read_bytes(unsigned int offset,void *out,size_t count)
{
    reading_nvmem=true;
    int ret=mt6357_rtc_nvmem_read(&pdev.dev,offset,out,count);
    reading_nvmem=false;return ret;
}
static void *read_thread(void *arg)
{
    thread_id=(uintptr_t)arg;u8 bytes[2]={0,0};
    assert(!read_bytes(0,bytes,2));assert(bytes[0]==0xab && bytes[1]==0xcd);return NULL;
}
static void *remove_thread(void *unused)
{
    thread_id=20;atomic_store(&removal_started,true);
    mutex_lock(&pdev.dev.mutex);cleanup_locked();device_unlock(&pdev.dev);
    atomic_store(&removed,true);return NULL;
}
static void *alarm_thread(void *arg)
{
    thread_id=(uintptr_t)arg;
    for (unsigned int i=0;i<(block_alarm ? 1U : 100U);i++) {
        struct rtc_wkalrm alarm={.enabled=true,.time={.tm_sec=i%60,.tm_min=(i+1)%60,
            .tm_hour=i%24,.tm_mday=i%28+1,.tm_mon=i%12,.tm_year=i%128}};
        assert(!mtk_rtc_set_alarm(&pdev.dev,&alarm));
        struct rtc_wkalrm got={0};assert(!mtk_rtc_read_alarm(&pdev.dev,&got));
        assert(got.enabled && got.time.tm_hour>=0 && got.time.tm_hour<24);
    }
    return NULL;
}
static void *reader_thread(void *arg)
{
    thread_id=(uintptr_t)arg;
    for(unsigned int i=0;i<100;i++) {
        u8 bytes[2]={0,0};int ret;
        /* Another reader/probe/remove may own the device mutex; retry EAGAIN. */
        do {ret=read_bytes(0,bytes,2);if(ret==-EAGAIN)sched_yield();} while(ret==-EAGAIN);
        assert(!ret && bytes[0]==0xab && bytes[1]==0xcd);
    }
    return NULL;
}
int main(void)
{
    unsigned int probes=0,samples=0,errors=0;
    mutex_init(&pdev.dev.mutex);reset();
    assert(!probe() && rtc_registered && nvmem_registered==(bool)CONFIG_RTC_NVMEM);
    assert(nvmem_registrations==(unsigned int)CONFIG_RTC_NVMEM);probes++;
    for(int kind=1;kind<3;kind++) {
        reset();match_kind=kind;assert(!probe() && rtc_registered && !nvmem_registered);
        u8 out=0xa5;assert(read_bytes(0,&out,1)==-ENODEV && out==0xa5 && !reads && !writes);probes++;
    }
    for(unsigned int fail=0;fail<10+(unsigned int)CONFIG_RTC_NVMEM;fail++) {
        reset();int expected;
        switch(fail) {
        case 0:pmic_dev.drvdata=NULL;expected=-ENODEV;break;
        case 1:pmic.regmap=NULL;expected=-ENODEV;break;
        case 2:alloc_fail=true;expected=-ENOMEM;break;
        case 3:no_resource=true;expected=-EINVAL;break;
        case 4:no_match=true;expected=-ENODEV;break;
        case 5:irq_error=expected=-ENXIO;break;
        case 6:rtc_alloc_fail=true;expected=-ENOMEM;break;
        case 7:request_error=expected=-EBUSY;break;
        case 8:rtc_register_error=expected=-EIO;break;
        case 9:irq_error=expected=-517;break;
        default:nvmem_register_error=expected=-EIO;break;
        }
        assert(probe()==expected && !rtc && !rtc_registered && !nvmem_registered && !irq_registered);
        assert(!reads && !writes);probes++;
    }
    if(!CONFIG_RTC_NVMEM) {
        reset();assert(!probe() && !nvmem_registered && !provider.reg_read && !reads && !writes);
        mutex_lock(&pdev.dev.mutex);cleanup_locked();device_unlock(&pdev.dev);
        assert(!pthread_mutex_destroy(&pdev.dev.mutex.raw));
        printf("PASS: RTC_NVMEM disabled: %u probe/config/error cases; no provider or register I/O\n",probes);
        return 0;
    }
    reset();assert(!probe());
    for(unsigned int byte=0;byte<2;byte++) for(unsigned int value=0;value<65536;value++) {
        regs[byte ? STOCK_SPARE_1 : STOCK_SPARE_0]=value;
        u8 out[3]={0xa5,0xa5,0xa5};unsigned int before=reads;
        assert(!read_bytes(byte,&out[1],1));
        assert(out[0]==0xa5 && out[1]==(value>>8) && out[2]==0xa5 && reads==before+1 && !writes);samples++;
    }
    regs[STOCK_SPARE_0]=0xab17;regs[STOCK_SPARE_1]=0xcd0c;
    for(unsigned int off=0;off<=3;off++) for(unsigned int count=0;count<=3;count++) {
        u8 out[4]={0xa5,0xa5,0xa5,0xa5};unsigned int before=reads;
        int ret=read_bytes(off,&out[1],count);
        bool valid=off<=2 && count<=2-off;
        assert(ret==(valid ? 0 : -EINVAL));
        assert(reads==before+(valid ? count : 0) && out[0]==0xa5 && out[3]==0xa5);
        if(valid && count) {assert(out[1]==(off ? 0xcd : 0xab));if(count==2)assert(out[2]==0xcd);}
        else assert(out[1]==0xa5 && out[2]==0xa5);
        samples++;
    }
    const unsigned int offsets[]={0,1,2,3,UINT_MAX-1,UINT_MAX};
    for(unsigned int i=0;i<ARRAY_SIZE(offsets);i++) {
        u8 out=0xa5;unsigned int before=reads;
        assert(read_bytes(offsets[i],&out,SIZE_MAX)==-EINVAL && out==0xa5 && reads==before);errors++;
    }
    const int failures[]={-EIO,-ENXIO,-ETIMEDOUT,-EAGAIN};
    for(unsigned int count=1;count<=2;count++) for(unsigned int failed=1;failed<=count;failed++)
    for(unsigned int e=0;e<ARRAY_SIZE(failures);e++) {
        u8 out[2]={0xa5,0xa5};fail_read=reads+failed;read_error=failures[e];
        assert(read_bytes(0,out,count)==failures[e] && out[0]==0xa5 && out[1]==0xa5);
        fail_read=0;assert(!read_bytes(0,out,count) && out[0]==0xab && (count==1 || out[1]==0xcd));errors++;
    }
    assert(!writes);
    mutex_lock(&pdev.dev.mutex);u8 out=0xa5;unsigned int before=reads;
    assert(read_bytes(0,&out,1)==-EAGAIN && out==0xa5 && reads==before);
    assert(!read_bytes(2,&out,0) && out==0xa5 && reads==before);device_unlock(&pdev.dev);errors++;
    /* Exercise real alarm read/modify/write and WRTGR while reads compete. */
    watch_transactions=true;pthread_t threads[4];
    for(uintptr_t i=0;i<2;i++)assert(!pthread_create(&threads[i],NULL,alarm_thread,(void *)(i+2)));
    for(uintptr_t i=2;i<4;i++)assert(!pthread_create(&threads[i],NULL,reader_thread,(void *)(i+2)));
    for(unsigned int i=0;i<4;i++)assert(!pthread_join(threads[i],NULL));
    assert((regs[STOCK_SPARE_0]>>8)==0xab && (regs[STOCK_SPARE_1]>>8)==0xcd);
    unsigned int pairs=0;
    for(unsigned int i=0;i<nevents;i++) if(events[i].thread>=4) {
        assert(events[i].reg==STOCK_SPARE_0 && !events[i].write);
        assert(i+1<nevents && events[i+1].thread==events[i].thread && events[i+1].reg==STOCK_SPARE_1 && !events[i+1].write);
        i++;pairs++;
    }
    assert(pairs==200);watch_transactions=false;
    /* Pause an actual alarm transaction before WRTGR. The NVMEM reader must
     * acquire the same RTC mutex and cannot observe a partially applied alarm. */
    block_alarm=true;assert(!pthread_create(&threads[0],NULL,alarm_thread,(void *)8));
    assert(!pthread_mutex_lock(&control));while(!alarm_entered)assert(!pthread_cond_wait(&condition,&control));
    assert(!pthread_mutex_unlock(&control));
    nvmem_waiting_rtc=false;before=reads;
    assert(!pthread_create(&threads[1],NULL,read_thread,(void *)9));
    assert(!pthread_mutex_lock(&control));while(!nvmem_waiting_rtc)assert(!pthread_cond_wait(&condition,&control));
    assert(reads==before);release_alarm=true;pthread_cond_broadcast(&condition);assert(!pthread_mutex_unlock(&control));
    assert(!pthread_join(threads[0],NULL) && !pthread_join(threads[1],NULL));block_alarm=false;
    /* Removal cannot free drvdata under a read; the device lock delays it. */
    block_nvmem=true;assert(!pthread_create(&threads[0],NULL,read_thread,(void *)10));
    assert(!pthread_mutex_lock(&control));while(!nvmem_entered)assert(!pthread_cond_wait(&condition,&control));
    assert(!pthread_mutex_unlock(&control));
    assert(!pthread_create(&threads[1],NULL,remove_thread,NULL));
    while(!atomic_load(&removal_started))sched_yield();
    assert(!atomic_load(&removed));
    assert(!pthread_mutex_lock(&control));release_nvmem=true;pthread_cond_broadcast(&condition);assert(!pthread_mutex_unlock(&control));
    assert(!pthread_join(threads[0],NULL) && !pthread_join(threads[1],NULL));
    assert(removed && !rtc && !rtc_registered && !nvmem_registered);
    /* The retained NVMEM callback has a live device, but no private RTC state. */
    for(unsigned int i=0;i<100;i++) {u8 bytes[2]={0xa5,0xa5};assert(provider.reg_read(provider.priv,0,bytes,2)==-ENODEV);assert(bytes[0]==0xa5 && bytes[1]==0xa5);}
    struct device_driver foreign={0};pdev.dev.driver=&foreign;
    out=0xa5;assert(provider.reg_read(provider.priv,0,&out,1)==-ENODEV && out==0xa5);
    pdev.dev.driver=NULL;
    /* An old provider resolves current bound state on a legitimate rebind;
     * it must never reuse its original, already freed RTC allocation. */
    struct nvmem_config retained_provider=provider;
    reset();assert(!probe());
    regs[STOCK_SPARE_0]=0x1217;regs[STOCK_SPARE_1]=0x340c;
    u8 fresh[2]={0,0};
    assert(!retained_provider.reg_read(retained_provider.priv,0,fresh,2));
    assert(fresh[0]==0x12 && fresh[1]==0x34);
    reset();match_kind=1;assert(!probe());
    fresh[0]=fresh[1]=0xa5;
    assert(retained_provider.reg_read(retained_provider.priv,0,fresh,2)==-ENODEV);
    assert(fresh[0]==0xa5 && fresh[1]==0xa5);
    mutex_lock(&pdev.dev.mutex);cleanup_locked();device_unlock(&pdev.dev);
    assert(!pthread_mutex_destroy(&pdev.dev.mutex.raw));
    printf("PASS: %u probe/config/error cases, %u independent stock-byte/range comparisons, %u read faults/bounds/lock cases, 200 alarm writes + 200 alarm reads + 200 NVMEM reads, blocked-alarm serialization, active-read removal, 100 late reads, foreign-driver rejection and compatible/incompatible rebinds\n",probes,samples,errors);
    return 0;
}
