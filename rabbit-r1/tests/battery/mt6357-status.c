/* Host fixture appended to the counter suite's production functions and PMIC model.
 * Real pthread synchronization; only Linux/framework/hardware boundaries are mocked. */
#include <stddef.h>
#include <stdatomic.h>
#include <stdlib.h>
#define HZ 100
#define GFP_KERNEL 0
#define EPROBE_DEFER 517
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define PTR_ERR(p) ((intptr_t)(p))
#define dev_err_probe(d,e,...) (e)
#define DL_FLAG_AUTOREMOVE_CONSUMER 1
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_delayed_work(p) container_of(p,struct delayed_work,work)
#define INIT_DELAYED_WORK(p,f) ((p)->work.fn=(f))
#define system_power_efficient_wq NULL
#define PSY_EVENT_PROP_CHANGED 1
#define NOTIFY_DONE 0
#define POWER_SUPPLY_STATUS_UNKNOWN 0
#define POWER_SUPPLY_STATUS_CHARGING 1
#define POWER_SUPPLY_STATUS_DISCHARGING 2
#define POWER_SUPPLY_STATUS_NOT_CHARGING 3
#define POWER_SUPPLY_STATUS_FULL 4
struct platform_device { struct device dev; };
struct power_supply_config { void *drv_data; void *fwnode; };
struct mt6397_chip { struct regmap *regmap; };
static struct device charger_dev, battery_dev;
static struct power_supply charger={.dev={.parent=&charger_dev}};
static struct mt6397_chip pmic={.regmap=&map};
static struct device pmic_dev={.driver_data=&pmic};
static struct platform_device pdev={.dev={.parent=&pmic_dev,.of_node=&battery_dev}};
static pthread_mutex_t qlock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t qcond=PTHREAD_COND_INITIALIZER;
static bool pending, running, canceling, read_blocked, read_entered, read_release;
static bool measure_lock_init, status_lock_init, registered, supplier_alive, linked;
static bool reference_present, adc_present, feedback, event_during_read;
static int reference_count, reference_error, registration_error, notifier_error, action_error, mutex_error;
static bool link_error, null_reference;
static int charger_status, charger_error;
/* Only fields used by the actual charger status functions are modeled. */
struct mt6370_priv { struct mutex attach_lock; int attach; };
static struct mt6370_priv charger_priv;
#define F_CHG_STAT 0
static unsigned int charger_field, charger_field_reads;
static bool use_real_charger;
static int mt6370_chg_get_status(struct mt6370_priv *priv, union power_supply_propval *val);
static int mt6370_chg_field_get(struct mt6370_priv *priv, int field, unsigned int *value)
{
    assert(priv == &charger_priv && field == F_CHG_STAT && !holding);
    charger_field_reads++; *value=charger_error ? UINT_MAX : charger_field;
    return charger_error;
}
static unsigned long queued_delay;
static unsigned int notifications, supplier_reads, cancellations, registrations, refs, links;
static pthread_rwlock_t notifier_lock=PTHREAD_RWLOCK_INITIALIZER;
static struct notifier_block *registered_notifier;
static bool pause_notifier, notifier_paused, release_notifier, unregister_entered;
static atomic_bool resources_freed;
static void before_mutex_lock(struct mutex *m)
{
    if (m != &gauge.status_lock || thread_id != 22) return;
    assert(!pthread_mutex_lock(&qlock));
    if (pause_notifier) {
        notifier_paused=true; pthread_cond_broadcast(&qcond);
        while (!release_notifier) assert(!pthread_cond_wait(&qcond,&qlock));
    }
    assert(!pthread_mutex_unlock(&qlock));
}
static void notify_event(unsigned long event, void *data)
{
    assert(!pthread_rwlock_rdlock(&notifier_lock));
    if (registered_notifier)
        assert(registered_notifier->notifier_call(registered_notifier,event,data) == NOTIFY_DONE);
    assert(!pthread_rwlock_unlock(&notifier_lock));
}
static void notify_supplier(void) { notify_event(PSY_EVENT_PROP_CHANGED,&charger); }
static int power_supply_reg_notifier(struct notifier_block *nb)
{
    assert(nb == &gauge.status_nb && registered && !gauge.status_active);
    if (notifier_error) return notifier_error;
    assert(!pthread_rwlock_wrlock(&notifier_lock));
    assert(!registered_notifier); registered_notifier=nb;
    assert(!pthread_rwlock_unlock(&notifier_lock));
    /* An event can enter before registration returns, but cannot start work. */
    notify_supplier(); assert(!pending);
    return 0;
}
static void power_supply_unreg_notifier(struct notifier_block *nb)
{
    assert(!holding);
    assert(!pthread_mutex_lock(&qlock));
    unregister_entered=true; pthread_cond_broadcast(&qcond);
    assert(!pthread_mutex_unlock(&qlock));
    /* This is the real blocking-notifier API's rwsem synchronization guarantee. */
    assert(!pthread_rwlock_wrlock(&notifier_lock));
    assert(registered_notifier == nb); registered_notifier=NULL;
    assert(!pthread_rwlock_unlock(&notifier_lock));
}
static void (*stop_action)(void *);
static void *stop_data;
static atomic_uint completed_measurements, completed_status_reads;
static int power_supply_get_property(struct power_supply *p, enum power_supply_property prop,
                                      union power_supply_propval *v)
{
    /* Taking either gauge mutex across supplier calls is a deadlock risk. */
    assert(!holding && p == &charger && prop == POWER_SUPPLY_PROP_STATUS);
    assert(!pthread_mutex_lock(&qlock));
    assert(supplier_alive && refs && linked);
    supplier_reads++;
    if (read_blocked) {
        read_entered=true; pthread_cond_broadcast(&qcond);
        while (!read_release) assert(!pthread_cond_wait(&qcond,&qlock));
    }
    int ret=charger_error, value=charger_status;
    bool event=event_during_read; event_during_read=false;
    assert(!pthread_mutex_unlock(&qlock));
    /* The supplier may have a pending changed notification during a query. */
    if (event) notify_supplier();
    if (use_real_charger) return mt6370_chg_get_status(&charger_priv,v);
    v->intval=ret ? INT_MIN : value;
    sched_yield();
    return ret;
}
static bool queue_delayed_work(void *q, struct delayed_work *w, unsigned long delay)
{
    assert(w == &gauge.status_work && gauge.status_lock.owner == thread_id);
    assert(!pthread_mutex_lock(&qlock));
    assert(!canceling && registered);
    bool added=!pending;
    if (added) { pending=true; queued_delay=delay; }
    assert(!pthread_mutex_unlock(&qlock));
    return added;
}
static bool mod_delayed_work(void *q, struct delayed_work *w, unsigned long delay)
{
    assert(w == &gauge.status_work && gauge.status_lock.owner == thread_id);
    assert(!pthread_mutex_lock(&qlock));
    assert(!canceling && registered);
    bool before=pending; pending=true; queued_delay=delay;
    assert(!pthread_mutex_unlock(&qlock)); return before;
}
static void cancel_delayed_work_sync(struct delayed_work *w)
{
    assert(!holding && w == &gauge.status_work);
    assert(!pthread_mutex_lock(&qlock));
    canceling=true; pending=false; cancellations++;
    pthread_cond_broadcast(&qcond);
    while (running) assert(!pthread_cond_wait(&qcond,&qlock));
    assert(!pending); canceling=false;
    assert(!pthread_mutex_unlock(&qlock));
}
static void power_supply_changed(struct power_supply *p)
{
    assert(p == &psy && registered && supplier_alive && !holding);
    notifications++;
    /* r1's policy reacts to battery notifications by writing charger settings.
     * Setters can notify even if their effective settings/status are unchanged. */
    if (feedback) notify_supplier();
}
static bool run_one(bool allow_poll)
{
    assert(!pthread_mutex_lock(&qlock));
    if (!pending || (!allow_poll && queued_delay)) {
        assert(!pthread_mutex_unlock(&qlock)); return false;
    }
    assert(!running && !canceling); pending=false; running=true;
    assert(!pthread_mutex_unlock(&qlock));
    gauge.status_work.work.fn(&gauge.status_work.work);
    assert(!pthread_mutex_lock(&qlock));
    running=false; pthread_cond_broadcast(&qcond);
    assert(!pthread_mutex_unlock(&qlock)); return true;
}
static void settle(void)
{
    unsigned int calls=0;
    while (run_one(false)) assert(++calls <= 2); /* a change and its one feedback event */
    assert(pending && queued_delay == MT6357_STATUS_POLL_INTERVAL);
}
static void *devm_kzalloc(struct device *d, size_t n, int flags)
{ assert(n == sizeof(gauge)); memset(&gauge,0,n); return &gauge; }
static void *dev_get_drvdata(struct device *d) { return d->driver_data; }
static void *dev_fwnode(struct device *d) { return d->of_node; }
static bool device_property_present(struct device *d, const char *name)
{
    if (!strcmp(name,"power-supplies")) return reference_present;
    assert(!strcmp(name,"mediatek,current-gain-permille")); return false;
}
static int device_property_read_u32(struct device *d, const char *name, u32 *out)
{ assert(!strcmp(name,"shunt-resistor-micro-ohms")); *out=10000; return 0; }
static int of_count_phandle_with_args(void *node, const char *name, void *args)
{ assert(node == pdev.dev.of_node && !strcmp(name,"power-supplies") && !args); return reference_count; }
static struct power_supply *devm_power_supply_get_by_reference(struct device *d, const char *name)
{
    assert(d == &pdev.dev && !strcmp(name,"power-supplies"));
    if (reference_error) return ERR_PTR(reference_error);
    if (null_reference) return NULL;
    refs++; return &charger;
}
static void *device_link_add(struct device *consumer, struct device *supplier, unsigned int flags)
{
    assert(consumer == &pdev.dev && supplier == &charger_dev && flags == DL_FLAG_AUTOREMOVE_CONSUMER);
    if (link_error) return NULL;
    links++; linked=true; return &charger_dev;
}
static int devm_mutex_init(struct device *d, struct mutex *m)
{
    assert(m == &gauge.lock || m == &gauge.status_lock);
    if (m == &gauge.status_lock && mutex_error) return mutex_error;
    assert(!pthread_mutex_init(&m->raw,NULL));
    if (m == &gauge.lock) measure_lock_init=true; else status_lock_init=true;
    return 0;
}
static int mt6357_gauge_init_adc(struct mt6357_gauge *g)
{
    /* Existing current suite covers the real ADC init; isolate supplier probe. */
    if (adc_present) g->desc=&mt6357_gauge_adc_desc;
    return 0;
}
static struct power_supply *devm_power_supply_register(struct device *d,
    const struct power_supply_desc *desc, const struct power_supply_config *config)
{
    assert(config->drv_data == &gauge && config->fwnode == pdev.dev.of_node);
    unsigned int count=adc_present ? 5 : 3;
    assert(desc->num_properties == count+(reference_present ? 1 : 0));
    for (unsigned int i=0; i<count; i++) assert(desc->properties[i] == mt6357_gauge_properties[i]);
    if (reference_present) {
        assert(desc->properties[count] == POWER_SUPPLY_PROP_STATUS);
        assert(desc == (adc_present ? &mt6357_gauge_adc_status_desc : &mt6357_gauge_status_desc));
        assert(!desc->external_power_changed);
        /* No callback or descriptor points into removable per-device memory. */
        assert(!pending && !gauge.psy && !registered_notifier);
        union power_supply_propval val={.intval=123};
        assert(!desc->get_property(&psy,POWER_SUPPLY_PROP_STATUS,&val));
        assert(val.intval == charger_status);
    } else assert(!desc->external_power_changed);
    if (registration_error) return ERR_PTR(registration_error);
    registered=true; registrations++; return &psy;
}
static int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *data)
{
    assert(registered && !pending && !gauge.status_active && data == &gauge);
    if (action_error) { fn(data); return action_error; }
    stop_action=fn; stop_data=data; return 0;
}
/* INSERT PRODUCTION STATUS FUNCTIONS HERE */
static void release_resources(void)
{
    if (stop_action) { stop_action(stop_data); stop_action=NULL; }
    assert(!pending && !running && !registered_notifier);
    registered=false; /* devm_power_supply_unregister, after the stop action */
    if (status_lock_init) { assert(!pthread_mutex_destroy(&gauge.status_lock.raw)); status_lock_init=false; }
    if (measure_lock_init) { assert(!pthread_mutex_destroy(&gauge.lock.raw)); measure_lock_init=false; }
    refs=0; linked=false; /* supplier reference and device link released last */
}
static void reset_fixture(void)
{
    release_resources();
    memset(&gauge,0,sizeof(gauge));
    reference_present=true; reference_count=1; adc_present=true;
    reference_error=registration_error=notifier_error=action_error=mutex_error=0;
    link_error=null_reference=feedback=event_during_read=false;
    read_blocked=read_entered=read_release=false;
    pause_notifier=notifier_paused=release_notifier=unregister_entered=false;
    resources_freed=false;
    charger_status=POWER_SUPPLY_STATUS_DISCHARGING; charger_error=0;
    supplier_alive=true; supplier_reads=notifications=cancellations=registrations=links=0;
    queued_delay=0;
}
static void expect_status(int result, int value)
{
    union power_supply_propval val={.intval=123};
    assert(mt6357_gauge_get_property(&psy,POWER_SUPPLY_PROP_STATUS,&val) == result);
    assert(val.intval == (result ? 123 : value));
}
static void set_supplier(int status, int error)
{
    assert(!pthread_mutex_lock(&qlock));
    charger_status=status; charger_error=error;
    assert(!pthread_mutex_unlock(&qlock));
}
static void *run_blocked_work(void *unused)
{ thread_id=20; assert(run_one(true)); return NULL; }
static void *remove_consumer(void *unused)
{ thread_id=21; mt6357_gauge_stop_status(&gauge); return NULL; }
static void *paused_notifier_thread(void *arg)
{ thread_id=22; notify_supplier(); return NULL; }
static void *release_all_thread(void *arg)
{ thread_id=23; release_resources(); atomic_store(&resources_freed,true); return NULL; }
static void *measurements(void *arg)
{
    thread_id=(uintptr_t)arg;
    for (unsigned int i=0; i<50; i++) {
        union power_supply_propval val={.intval=123};
        bool current=i&1;
        assert(!mt6357_gauge_get_property(&psy,current ? POWER_SUPPLY_PROP_CURRENT_NOW : POWER_SUPPLY_PROP_CHARGE_COUNTER,&val));
        assert(val.intval == (current ? 300 : 100));
        atomic_fetch_add(&completed_measurements,1);
    }
    return NULL;
}
static void *status_readers(void *arg)
{
    thread_id=(uintptr_t)arg;
    for (unsigned int i=0; i<100; i++) {
        expect_status(0,POWER_SUPPLY_STATUS_CHARGING);
        atomic_fetch_add(&completed_status_reads,1);
    }
    return NULL;
}
int main(void)
{
    unsigned int transitions=0, error_cases=0, probe_cases=0, charger_cases=0;
    reset_fixture(); reference_present=false;
    assert(!mt6357_gauge_probe(&pdev));
    assert(gauge.desc->num_properties == 5 && !links && !refs && !pending);
    expect_status(-ENODATA,0); probe_cases++;
    for (unsigned int adc=0; adc<2; adc++) {
        reset_fixture(); adc_present=adc;
        assert(!mt6357_gauge_probe(&pdev));
        assert(gauge.desc->num_properties == (adc ? 6 : 4) && links == 1 && refs == 1);
        assert(gauge.psy == &psy && stop_action == mt6357_gauge_stop_status);
        assert(pending && !queued_delay && !notifications);
        settle(); assert(notifications == 1); probe_cases++;
    }
    const int counts[]={0,2,3,-EINVAL,-ENOENT};
    for (unsigned int i=0; i<ARRAY_SIZE(counts); i++) {
        reset_fixture(); reference_count=counts[i];
        assert(mt6357_gauge_probe(&pdev) == (counts[i]<0 ? counts[i] : -EINVAL));
        assert(!registrations && !pending && !refs); probe_cases++;
    }
    for (unsigned int f=0; f<7; f++) {
        reset_fixture(); int expected_error;
        switch (f) {
        case 0: reference_error=expected_error=-EPROBE_DEFER; break;
        case 1: null_reference=true; expected_error=-EPROBE_DEFER; break;
        case 2: link_error=true; expected_error=-ENOMEM; break;
        case 3: mutex_error=expected_error=-ENOMEM; break;
        case 4: registration_error=expected_error=-EIO; break;
        case 5: notifier_error=expected_error=-EIO; break;
        default: action_error=expected_error=-ENOMEM; break;
        }
        assert(mt6357_gauge_probe(&pdev) == expected_error);
        assert(!pending && !stop_action && !gauge.status_active);
        assert(registrations == (f >= 5 ? 1U : 0U));
        assert(cancellations == (f == 6 ? 1U : 0U)); probe_cases++;
    }
    reset_fixture(); assert(!mt6357_gauge_probe(&pdev));
    /* First-read failure is published once; different errno values do not loop. */
    set_supplier(INT_MIN,-EIO); settle(); assert(notifications == 1 && !gauge.status_valid);
    set_supplier(INT_MIN,-EAGAIN); notify_supplier(); settle();
    assert(notifications == 1);
    set_supplier(POWER_SUPPLY_STATUS_UNKNOWN,0); notify_supplier(); settle();
    assert(notifications == 2 && gauge.status_valid);
    reset_fixture(); assert(!mt6357_gauge_probe(&pdev));
    assert(!pthread_mutex_init(&charger_priv.attach_lock.raw,NULL));
    use_real_charger=true;
    const int expected_map[]={POWER_SUPPLY_STATUS_NOT_CHARGING, POWER_SUPPLY_STATUS_CHARGING,
                              POWER_SUPPLY_STATUS_FULL, POWER_SUPPLY_STATUS_NOT_CHARGING,
                              POWER_SUPPLY_STATUS_UNKNOWN};
    for (int attach=0; attach<4; attach++) for (unsigned int field=0; field<5; field++)
    for (unsigned int failure=0; failure<2; failure++) {
        charger_priv.attach=attach; charger_field=field; charger_error=failure ? -EIO : 0;
        charger_field_reads=0;
        expect_status(attach && failure ? -EIO : 0,
                      attach ? expected_map[field] : POWER_SUPPLY_STATUS_DISCHARGING);
        assert(charger_field_reads == (attach ? 1U : 0U)); charger_cases++;
    }
    use_real_charger=false; assert(!pthread_mutex_destroy(&charger_priv.attach_lock.raw));
    reset_fixture(); assert(!mt6357_gauge_probe(&pdev)); feedback=true; settle();
    assert(notifications == 1);
    unsigned int reads_before=supplier_reads;
    notify_event(PSY_EVENT_PROP_CHANGED,&psy);
    notify_event(PSY_EVENT_PROP_CHANGED,NULL);
    notify_event(PSY_EVENT_PROP_CHANGED+1,&charger);
    assert(pending && queued_delay == MT6357_STATUS_POLL_INTERVAL && supplier_reads == reads_before);
    /* Stable writes and policy feedback must not form an unbounded event loop. */
    for (unsigned int i=0; i<1000; i++) {
        notify_supplier(); settle();
        assert(notifications == 1);
    }
    /* All valid status transitions, including valid UNKNOWN, remain observable. */
    for (int before=0; before<=4; before++) for (int after=0; after<=4; after++) {
        set_supplier(before,0); notify_supplier(); settle();
        unsigned int previous=notifications;
        set_supplier(after,0); expect_status(0,after);
        notify_supplier(); settle();
        assert(notifications == previous+(before != after)); transitions++;
    }
    /* Silent hardware transition: periodic refresh, independent of supplier IRQs. */
    set_supplier(POWER_SUPPLY_STATUS_DISCHARGING,0); notify_supplier(); settle();
    unsigned int previous=notifications;
    set_supplier(POWER_SUPPLY_STATUS_FULL,0); assert(run_one(true)); settle();
    assert(notifications == previous+1); transitions++;
    /* An event arriving inside a read must keep the immediate refresh queued. */
    event_during_read=true; assert(run_one(true)); assert(pending && !queued_delay);
    settle(); assert(notifications == previous+1);
    const int failures[]={-EIO,-ENODEV,-EAGAIN,-ENODATA,-ETIMEDOUT};
    previous=notifications;
    for (unsigned int i=0; i<ARRAY_SIZE(failures); i++) {
        set_supplier(INT_MIN,failures[i]); expect_status(failures[i],0);
        notify_supplier(); settle();
        assert(notifications == previous+1); error_cases++;
    }
    const int invalid[]={-1,5,INT_MIN,INT_MAX};
    for (unsigned int i=0; i<ARRAY_SIZE(invalid); i++) {
        set_supplier(invalid[i],0); expect_status(-ERANGE,0);
        notify_supplier(); settle();
        assert(notifications == previous+1); error_cases++;
    }
    set_supplier(POWER_SUPPLY_STATUS_UNKNOWN,0); notify_supplier(); settle();
    assert(notifications == previous+2); /* invalid -> valid UNKNOWN */
    set_supplier(POWER_SUPPLY_STATUS_CHARGING,0); notify_supplier(); settle();
    /* The latch model asserts every current/counter bus transaction stays whole,
     * while a supplier read asserts that this thread holds neither gauge mutex. */
    setup(true,0x4800); pthread_t threads[6];
    for (uintptr_t i=0; i<4; i++) assert(!pthread_create(&threads[i],NULL,measurements,(void *)(i+2)));
    for (uintptr_t i=4; i<6; i++) assert(!pthread_create(&threads[i],NULL,status_readers,(void *)(i+2)));
    for (unsigned int i=0; i<100; i++) {
        notify_supplier(); settle();
    }
    for (unsigned int i=0; i<6; i++) assert(!pthread_join(threads[i],NULL));
    assert(completed_measurements == 200 && completed_status_reads == 200);
    assert(low_reads == 100 && high_reads == 100 && current_reads == 100);
    unsigned int transactions=0;
    for (unsigned int i=0; i<nops;) {
        assert(trace[i].op == 'r' && trace[i].reg == STOCK_ON);
        unsigned int owner=trace[i++].thread;
        while (i<nops && trace[i].reg != STOCK_ON) assert(trace[i++].thread == owner);
        transactions++;
    }
    assert(transactions == 200 && !gauge.needs_release);
    /* Removal while a supplier transaction is blocked: cancellation must wait;
     * late callbacks cannot restart work; supplier data stays alive until drain. */
    read_blocked=true; previous=notifications;
    pthread_t work_thread, remove_thread;
    assert(!pthread_create(&work_thread,NULL,run_blocked_work,NULL));
    assert(!pthread_mutex_lock(&qlock));
    while (!read_entered) assert(!pthread_cond_wait(&qcond,&qlock));
    assert(!pthread_mutex_unlock(&qlock));
    /* The blocked supplier call must not stall a genuine PMIC transaction. */
    int current;
    assert(!mt6357_gauge_read_current(&gauge,&current) && current == 300);
    assert(!pthread_create(&remove_thread,NULL,remove_consumer,NULL));
    assert(!pthread_mutex_lock(&qlock));
    while (!canceling) assert(!pthread_cond_wait(&qcond,&qlock));
    assert(running && !pending && supplier_alive && registered);
    assert(!pthread_mutex_unlock(&qlock));
    for (unsigned int i=0; i<100; i++) notify_supplier();
    assert(!pthread_mutex_lock(&qlock));
    read_release=true; pthread_cond_broadcast(&qcond);
    assert(!pthread_mutex_unlock(&qlock));
    assert(!pthread_join(work_thread,NULL) && !pthread_join(remove_thread,NULL));
    assert(!pending && !running && !gauge.status_active && notifications == previous);
    stop_action=NULL; /* The managed action was invoked by the removal thread. */
    for (unsigned int i=0; i<100; i++) notify_supplier();
    assert(!pending);
    release_resources(); supplier_alive=false;
    assert(!pending && !running && !refs && !linked);
    /* The review regression: a notifier has its gauge pointer but is paused
     * before status_lock. Removal must wait before freeing ANY gauge resources. */
    reset_fixture(); assert(!mt6357_gauge_probe(&pdev)); settle();
    pause_notifier=true;
    assert(!pthread_create(&work_thread,NULL,paused_notifier_thread,NULL));
    assert(!pthread_mutex_lock(&qlock));
    while (!notifier_paused) assert(!pthread_cond_wait(&qcond,&qlock));
    assert(!pthread_mutex_unlock(&qlock));
    assert(!pthread_create(&remove_thread,NULL,release_all_thread,NULL));
    assert(!pthread_mutex_lock(&qlock));
    while (!unregister_entered) assert(!pthread_cond_wait(&qcond,&qlock));
    assert(registered && registered_notifier && measure_lock_init && status_lock_init);
    assert(!resources_freed && !gauge.status_active);
    release_notifier=true; pthread_cond_broadcast(&qcond);
    assert(!pthread_mutex_unlock(&qlock));
    assert(!pthread_join(work_thread,NULL) && !pthread_join(remove_thread,NULL));
    assert(resources_freed && !registered_notifier && !pending && !running);
    /* Simulate a supply class iterator keeping psy beyond driver unbind.
     * Its descriptor must remain readable even after private data is poisoned. */
    const struct power_supply_desc *retained_desc=gauge.desc;
    memset(&gauge,0xa5,sizeof(gauge));
    assert(!retained_desc->external_power_changed);
    assert(retained_desc->num_properties == 6 && retained_desc->get_property == mt6357_gauge_get_property);
    for (unsigned int i=0; i<100; i++) notify_supplier();
    printf("PASS: %u actual charger mapping/error cases, %u supplier/probe cases, 1000 stable write/feedback cycles, %u transitions, %u error/invalid results, immediate-event preservation, blocked-I/O and entered-notifier removal, static descriptor lifetime, 300 late events, 200 concurrent measurement + 200 STATUS reads\n",charger_cases,probe_cases,transitions,error_cases);
    return 0;
}
