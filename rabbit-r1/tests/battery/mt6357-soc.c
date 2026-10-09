/* SPDX-License-Identifier: GPL-2.0-only */
/* Actual gauge and model have already been compiled above this fixture. */
static pthread_mutex_t work_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t work_cond=PTHREAD_COND_INITIALIZER;
static unsigned int notifications, cancellations, action_count, action_fail, notifier_count;
static void (*actions[8])(void *);
static void *action_data[8];
static bool notifier_fail;
static unsigned int soc_cases;
static _Thread_local bool checking_suspend_write;
static void mock_write_once(bool *p, bool value)
{
    assert(p == &gauge.suspended);
    if (checking_suspend_write && value) assert(gauge.status_lock.owner == thread_id);
    *p=value;
}
static bool recover_on_sleep;
static void msleep(unsigned int ms)
{
    assert(!holding); sleep_calls++; now+=(u64)ms*1000;
    if (recover_on_sleep) { memset(raw_errors,0,sizeof(raw_errors)); recover_on_sleep=false; }
}
static struct platform_device battery_platform={.dev={.parent=&pmic_dev,.of_node=(void *)1}};

static int queue_delayed_work(void *q, struct delayed_work *w, unsigned long delay)
{
    assert(w == &gauge.soc.work || w == &gauge.status_work);
    assert(q == (w == &gauge.soc.work ? system_freezable_power_efficient_wq : system_power_efficient_wq));
    assert(!pthread_mutex_lock(&work_lock));
    bool existed=w->queued;
    if (!existed) { w->queued=true; w->delay=delay; }
    assert(!pthread_mutex_unlock(&work_lock)); return !existed;
}
static int mod_delayed_work(void *q, struct delayed_work *w, unsigned long delay)
{
    assert(!pthread_mutex_lock(&work_lock)); bool existed=w->queued; w->queued=true; w->delay=delay;
    assert(!pthread_mutex_unlock(&work_lock)); return existed;
}
static void cancel_delayed_work_sync(struct delayed_work *w)
{
    assert(!holding); assert(!pthread_mutex_lock(&work_lock)); cancellations++;
    w->queued=false; assert(!pthread_cond_broadcast(&work_cond));
    while (w->running) assert(!pthread_cond_wait(&work_cond,&work_lock));
    w->queued=false; assert(!pthread_mutex_unlock(&work_lock));
}
static void run_work(struct delayed_work *w)
{
    assert(!pthread_mutex_lock(&work_lock)); assert(w->queued && !w->running);
    w->queued=false; w->running=true; unsigned long delay=w->delay;
    assert(!pthread_mutex_unlock(&work_lock)); now+=delay*1000000/HZ;
    w->work.fn(&w->work);
    assert(!pthread_mutex_lock(&work_lock)); w->running=false;
    assert(!pthread_cond_broadcast(&work_cond)); assert(!pthread_mutex_unlock(&work_lock));
}
static void power_supply_changed(struct power_supply *p)
{ assert(!holding && p == &psy); notifications++; }
static int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *data)
{
    assert(data == &gauge && action_count<ARRAY_SIZE(actions));
    if (action_count+1 == action_fail) { fn(data); return -ENOMEM; }
    actions[action_count]=fn; action_data[action_count++]=data; return 0;
}
static int power_supply_reg_notifier(struct notifier_block *nb)
{ assert(nb == &gauge.status_nb && !notifier_count); if (notifier_fail) return -EIO; notifier_count++; return 0; }
static void power_supply_unreg_notifier(struct notifier_block *nb)
{ assert(nb == &gauge.status_nb && notifier_count == 1 && !holding); notifier_count--; }
static int power_supply_get_property(struct power_supply *p, enum power_supply_property prop, union power_supply_propval *v)
{ assert(p == &charger && prop == POWER_SUPPLY_PROP_STATUS); v->intval=2; return 0; }
static int of_count_phandle_with_args(void *n, const char *p, void *a)
{ assert(n && !strcmp(p,"power-supplies") && !a); return 1; }
static struct device_node *of_parse_phandle(void *n, const char *p, int i)
{ assert(n && !strcmp(p,"power-supplies") && i == 0); node_refs++; return &charger_node; }
static struct power_supply *devm_power_supply_get_by_parent(struct device *d, struct device *p)
{ assert(provider_locked && p == &charger_platform.dev); return &charger; }

static void cleanup(void)
{
    while (action_count) { unsigned int i=--action_count; actions[i](action_data[i]); }
    assert(!notifier_count && !provider_refs && !node_refs);
    struct mutex *locks[]={&gauge.soc.lock,&gauge.status_lock,&gauge.lock};
    for (unsigned int i=0; i<ARRAY_SIZE(locks); i++) if (locks[i]->initialized) {
        assert(!locks[i]->owner); assert(!pthread_mutex_destroy(&locks[i]->raw)); locks[i]->initialized=false;
    }
    assert(!gauge.soc.work.running && !gauge.status_work.running);
}
static void soc_setup(void)
{
    cleanup(); memset(&gauge,0,sizeof(gauge)); live_setup(false);
    board_r1=charger_present=adc_present=true; adc_delay_us=0; recover_on_sleep=false; property_gain=1000; property_shunt=10000;
    shunt_present=true; gain_present=malformed_gain=fail_registration=notifier_fail=false;
    registrations=notifications=cancellations=action_count=action_fail=sleep_calls=notifier_count=0;
    battery_platform.dev.driver_data=NULL;
    gauge.dev=&battery_platform.dev; gauge.psy=&psy; gauge.charger=&charger;
    charger_platform.dev.links.status=DL_DEV_DRIVER_BOUND;
    for (unsigned int i=0; i<ARRAY_SIZE(current_words); i++) { current_words[i]=0; car_words[i]=10000*2048; baton_words[i]=1; }
    for (unsigned int i=0; i<6; i++) { unsigned int channel=i<3 ? i : 5-i; raw_values[i]=channel == 0 ? 24273 : channel == 1 ? 1522 : 4095; }
    now=1000000;
}
static void direct_init(void)
{
    soc_setup(); init_lock(); assert(!devm_mutex_init(gauge.dev,&gauge.status_lock));
    INIT_DELAYED_WORK(&gauge.status_work,mt6357_gauge_status_work);
    assert(!mt6357_soc_init(&gauge)); battery_platform.dev.driver_data=&gauge;
}
static void seed_and_cache(void)
{
    direct_init(); int ret=mt6357_soc_seed(&gauge);
    if (ret) fprintf(stderr,"seed=%d temp=%d mv=%d\n",ret,gauge.soc.live.temperature_decic[0].value,gauge.soc.live.adc[0][0].mv.value);
    assert(!ret); assert(latch_count == 2 && raw_calls == 6 && !sleep_calls);
    int value=-777; assert(!mt6357_soc_read_capacity(&gauge,&value)); assert(value>=0 && value<=100);
    struct r1_battery_estimate e; assert(!r1_battery_session_read(&gauge.soc.session,ktime_get_boottime_ns(),&e));
    assert(value == (e.soc_point_bp+50)/100); unsigned int old_ops=nops;
    for (unsigned int i=0; i<200; i++) assert(!mt6357_soc_read_capacity(&gauge,&value));
    assert(old_ops == nops && raw_calls == 6); soc_cases++;
    now+=6000000; value=-777; assert(mt6357_soc_read_capacity(&gauge,&value) == -ENODATA && value == -777); soc_cases++;
}

static void percentages(void)
{
    const struct { int bp, percent; } examples[]={{0,0},{1,0},{49,0},{50,1},{99,1},{100,1},{149,1},{150,2},{9899,99},{9949,99},{9950,100},{9999,100},{10000,100}};
    for (unsigned int i=0; i<ARRAY_SIZE(examples); i++) {
        int value=-777; assert(!mt6357_soc_percent(examples[i].bp,&value));
        assert(value == examples[i].percent); soc_cases++;
    }
    const r1_battery_int invalid[]={-1,10001,INT64_MIN,INT64_MAX};
    for (unsigned int i=0; i<ARRAY_SIZE(invalid); i++) { int value=-777; assert(mt6357_soc_percent(invalid[i],&value) == -ERANGE && value == -777); soc_cases++; }
}
static void adapter(void)
{
    direct_init(); struct mt6357_live_observation good;
    assert(!mt6357_gauge_collect_live(&gauge,&good));
    struct r1_battery_observation o; assert(!mt6357_soc_adapt(&good,&o));
    assert(o.generation == 77 && o.sequence == 1 && o.endpoint[0].present == 1);
    assert(o.endpoint[0].raw_car == 20480000 && o.endpoint[0].car_uah == 111800);
    assert(o.endpoint[0].current_ua == 0 && o.endpoint[0].voltage_uv == 4000000);
    assert(o.endpoint[0].temperature_decic == 250); soc_cases++;
    for (unsigned int set=0; set<2; set++) for (unsigned int failure=0; failure<25; failure++) {
        struct mt6357_live_observation bad=good;
        struct mt6357_live_fg *fg=&bad.fg[set];
        int *error=NULL;
        if (failure == 0) error=&fg->prepare_error;
        else if (failure == 1) error=&fg->latch_error;
        else if (failure == 2) error=&fg->release_error;
        else if (failure == 3) error=&fg->current_ua.error;
        else if (failure == 4) error=&fg->charge_uah.error;
        else if (failure == 5) error=&fg->present.error;
        else if (failure == 6) error=&bad.temperature_decic[set].error;
        else if (failure<7+MT6357_LIVE_WORDS) error=&fg->words[failure-7].error;
        else { unsigned int adc=(failure-7-MT6357_LIVE_WORDS)/3, field=(failure-7-MT6357_LIVE_WORDS)%3;
            if (adc>=3) break;
            error=field == 0 ? &bad.adc[set][adc].raw.error : field == 1 ? &bad.adc[set][adc].scale_error : &bad.adc[set][adc].mv.error;
        }
        *error=-EIO; assert(mt6357_soc_adapt(&bad,&o) == -EIO && o.error == -EIO && o.endpoint[set].error == -EIO);
        assert(!o.endpoint[set].voltage_uv && !o.endpoint[set].raw_car); soc_cases++;
    }
    for (unsigned int fault=0; fault<15; fault++) {
        struct mt6357_live_observation bad=good;
        switch (fault) {
        case 0: bad.generation=0; break;
        case 1: bad.generation=UINT64_MAX; break;
        case 2: bad.sequence=0; break;
        case 3: bad.sequence=UINT64_MAX; break;
        case 4: bad.started_ns=0; break;
        case 5: bad.finished_ns=UINT64_MAX; break;
        case 6: bad.finished_ns=bad.started_ns-1; break;
        case 7: bad.fg[0].started_ns=bad.started_ns-1; break;
        case 8: bad.fg[0].ready_ns=bad.fg[0].started_ns-1; break;
        case 9: bad.fg[1].finished_ns=bad.finished_ns+1; break;
        case 10: bad.adc[1][1].started_ns=bad.started_ns; break;
        case 11: bad.adc[0][1].finished_ns=bad.adc[0][1].started_ns-1; break;
        case 12: bad.adc[0][0].mv.value=INT_MAX; break;
        case 13: bad.adc[0][0].mv.value=0; break;
        case 14: bad.fg[0].words[MT6357_LIVE_CAR_LOW].value=0x10000; break;
        }
        assert(mt6357_soc_adapt(&bad,&o) == -ERANGE && o.error == -ERANGE); soc_cases++;
    }
    struct mt6357_live_observation bad=good; bad.error=-ENXIO;
    bad.fg[0].present.value=0; assert(mt6357_soc_observed_event(&bad) == R1_BATTERY_EVENT_REMOVAL);
    bad.fg[0].present.error=-EIO; assert(mt6357_soc_observed_event(&bad) == R1_BATTERY_EVENT_NONE);
    bad.fg[1].words[MT6357_LIVE_ENGINE].value=0; assert(mt6357_soc_observed_event(&bad) == R1_BATTERY_EVENT_ENGINE_OFF);
    bad.fg[1].words[MT6357_LIVE_ENGINE].error=-EIO; assert(mt6357_soc_observed_event(&bad) == R1_BATTERY_EVENT_NONE); soc_cases+=4;
}
static void worker_and_pm(void)
{
    direct_init(); assert(!mt6357_soc_seed(&gauge)); assert(!mt6357_soc_start(&gauge));
    run_work(&gauge.soc.work); assert(notifications == 1 && gauge.soc.work.queued);
    for (unsigned int i=0; i<5; i++) run_work(&gauge.soc.work);
    assert(notifications == 1); soc_cases++;
    raw_errors[0]=-EIO; run_work(&gauge.soc.work); int value=-777;
    assert(mt6357_soc_read_capacity(&gauge,&value) == -EIO && value == -777 && notifications == 2);
    run_work(&gauge.soc.work); assert(notifications == 2); soc_cases++;
    raw_errors[0]=0; run_work(&gauge.soc.work); assert(!mt6357_soc_read_capacity(&gauge,&value) && notifications == 3); soc_cases++;
    r1_battery_int old_epoch=gauge.soc.session.estimate.epoch;
    checking_suspend_write=true; assert(!mt6357_gauge_suspend(gauge.dev)); checking_suspend_write=false; assert(gauge.suspended && !gauge.soc.work.queued);
    value=-777; assert(mt6357_soc_read_capacity(&gauge,&value) == -EAGAIN && value == -777);
    assert(!mt6357_gauge_resume(gauge.dev)); assert(!gauge.suspended && gauge.soc.work.queued && gauge.soc.work.delay == 1);
    value=-777; assert(mt6357_soc_read_capacity(&gauge,&value) == -EAGAIN && value == -777);
    run_work(&gauge.soc.work); assert(!mt6357_soc_read_capacity(&gauge,&value));
    assert(gauge.soc.session.estimate.epoch>old_epoch); soc_cases++;
    /* A known battery absence invalidates even beside a separate ADC failure. */
    for (unsigned int i=latch_count; i<ARRAY_SIZE(baton_words); i++) baton_words[i]=3;
    raw_errors[0]=-EIO; run_work(&gauge.soc.work);
    assert(gauge.soc.session.estimate.event == R1_BATTERY_EVENT_REMOVAL);
    value=-777; assert(mt6357_soc_read_capacity(&gauge,&value) == -EAGAIN && value == -777); soc_cases++;
    for (unsigned int i=latch_count; i<ARRAY_SIZE(baton_words); i++) baton_words[i]=1;
    raw_errors[0]=0; run_work(&gauge.soc.work); assert(!mt6357_soc_read_capacity(&gauge,&value)); soc_cases++;
    mt6357_gauge_shutdown(&battery_platform); assert(gauge.soc.stopping && !gauge.soc.work.queued);
    value=-777; assert(mt6357_soc_read_capacity(&gauge,&value) == -ENODEV && value == -777); soc_cases++;
}
static void init_and_seed_failures(void)
{
    for (unsigned int fault=0; fault<10; fault++) {
        direct_init(); /* Reinitialize the SOC mutex only for this init boundary test. */
        assert(!pthread_mutex_destroy(&gauge.soc.lock.raw)); memset(&gauge.soc,0,sizeof(gauge.soc));
        switch (fault) {
        case 0: gauge.charger=NULL; break;
        case 1: gauge.voltage=NULL; break;
        case 2: gauge.thermistor=NULL; break;
        case 3: gauge.reference=NULL; break;
        case 4: gauge.shunt_uohms++; break;
        case 5: gauge.gain_permille++; break;
        case 6: gauge.pullup_ohms++; break;
        case 7: gauge.series_uohms++; break;
        case 8: gauge.num_points--; break;
        case 9: table_storage[3].resistance++; break;
        }
        assert(mt6357_soc_init(&gauge) == (fault<4 ? -ENODATA : -EINVAL) && !gauge.soc.enabled); soc_cases++;
    }
    direct_init(); board_r1=false; assert(!pthread_mutex_destroy(&gauge.soc.lock.raw)); memset(&gauge.soc,0,sizeof(gauge.soc));
    assert(!mt6357_soc_init(&gauge) && !gauge.soc.enabled && !mt6357_soc_seed(&gauge)); soc_cases++;
    for (unsigned int fault=0; fault<5; fault++) {
        direct_init();
        switch (fault) {
        case 0: raw_errors[0]=-EIO; break;
        case 1: for (unsigned int i=0; i<6; i++) if (i == 1 || i == 4) raw_values[i]=2800; break; /* Cold profile/NTC rejection. */
        case 2: raw_values[0]=raw_values[5]=30000; break; /* Beyond curve; no root. */
        case 3: for (unsigned int i=0; i<ARRAY_SIZE(current_words); i++) current_words[i]=0x7fff; break;
        case 4: raw_values[5]+=1000; break;
        }
        int ret=mt6357_soc_seed(&gauge); assert(ret<0 && sleep_calls == 7 && latch_count == 16 && !registrations);
        int value=-777; assert(mt6357_soc_read_capacity(&gauge,&value)<0 && value == -777); soc_cases++;
    }
}
static void startup_deadline(void)
{
    direct_init(); raw_errors[0]=-EIO; recover_on_sleep=true;
    assert(!mt6357_soc_seed(&gauge) && sleep_calls == 1 && latch_count == 4); soc_cases++;
    direct_init(); adc_delay_us=1000000;
    assert(mt6357_soc_seed(&gauge)<0 && latch_count == 2 && sleep_calls == 1);
    /* The deadline is checked between I/O calls; it cannot abort slow I/O. */
    assert(now>7000000); soc_cases++;
}
static void probes(void)
{
    soc_setup(); assert(!mt6357_gauge_probe(&battery_platform));
    assert(registrations == 1 && notifier_count == 1 && action_count == 2 && latch_count == 2 && raw_calls == 6);
    assert(actions[0] == mt6357_gauge_stop_status && actions[1] == mt6357_soc_stop);
    assert(gauge.soc.work.queued && gauge.status_work.queued && !provider_refs && !node_refs);
    unsigned int stages=stage; soc_cases++; cleanup();
    for (unsigned int fail=1; fail<=stages; fail++) {
        soc_setup(); fail_stage=fail; int ret=mt6357_gauge_probe(&battery_platform);
        assert(ret<0); assert(!provider_refs && !node_refs); cleanup(); soc_cases++;
    }
    for (unsigned int failure=0; failure<5; failure++) {
        soc_setup();
        if (failure == 0) fail_registration=true;
        if (failure == 1) notifier_fail=true;
        if (failure == 2) action_fail=1;
        if (failure == 3) action_fail=2;
        if (failure == 4) raw_errors[0]=-EIO;
        assert(mt6357_gauge_probe(&battery_platform)<0); cleanup(); soc_cases++;
    }
    soc_setup(); board_r1=false; assert(!mt6357_gauge_probe(&battery_platform));
    assert(!gauge.soc.enabled && registrations == 1 && gauge.desc == &mt6357_gauge_adc_status_desc); soc_cases++;
}

static void *thread_worker(void *unused)
{ thread_id=2; run_work(&gauge.soc.work); return NULL; }
static _Atomic bool stop_returned;
static void *thread_stop(void *suspend)
{
    thread_id=3;
    if (suspend) { checking_suspend_write=true; assert(!mt6357_gauge_suspend(gauge.dev)); checking_suspend_write=false; }
    else mt6357_soc_stop(&gauge);
    stop_returned=true; return NULL;
}
static void entered_worker_lifetime(void)
{
    for (unsigned int suspend=0; suspend<2; suspend++) {
        direct_init(); assert(!mt6357_soc_seed(&gauge)); assert(!mt6357_soc_start(&gauge));
        r1_battery_int accepted=gauge.soc.session.estimate.accepted_ns;
        pause_raw=1; stop_returned=false; pthread_t worker_thread, stop_thread;
        assert(!pthread_create(&worker_thread,NULL,thread_worker,NULL));
        assert(!pthread_mutex_lock(&pause_lock));
        while (!pause_entered) assert(!pthread_cond_wait(&pause_cond,&pause_lock));
        assert(!pthread_mutex_unlock(&pause_lock));
        /* Cache reads do not wait on an entered ADC/measurement transaction. */
        for (unsigned int i=0; i<200; i++) { int value=-777; assert(!mt6357_soc_read_capacity(&gauge,&value)); }
        assert(!pthread_create(&stop_thread,NULL,thread_stop,(void *)(uintptr_t)suspend));
        assert(!pthread_mutex_lock(&work_lock));
        while (!cancellations) assert(!pthread_cond_wait(&work_cond,&work_lock));
        assert(!pthread_mutex_unlock(&work_lock));
        assert(!stop_returned); int value=-777;
        assert(mt6357_soc_read_capacity(&gauge,&value) == (suspend ? -EAGAIN : -ENODEV) && value == -777);
        assert(!pthread_mutex_lock(&pause_lock)); pause_continue=true;
        assert(!pthread_cond_broadcast(&pause_cond)); assert(!pthread_mutex_unlock(&pause_lock));
        assert(!pthread_join(worker_thread,NULL)); assert(!pthread_join(stop_thread,NULL));
        assert(stop_returned && !gauge.soc.work.queued && !gauge.soc.work.running && !notifications);
        assert(gauge.soc.session.estimate.accepted_ns == accepted);
        if (suspend) {
            pause_raw=0; assert(!mt6357_gauge_resume(gauge.dev)); run_work(&gauge.soc.work);
            assert(!mt6357_soc_read_capacity(&gauge,&value));
            assert(gauge.soc.session.estimate.accepted_ns>accepted);
        }
        soc_cases++;
    }
}

static void expiry_during_collection(void)
{
    direct_init(); assert(!mt6357_soc_seed(&gauge)); assert(!mt6357_soc_start(&gauge));
    run_work(&gauge.soc.work); assert(notifications == 1);
    struct r1_battery_session previous=gauge.soc.session;
    int before=-777, after=-777; assert(!mt6357_soc_read_capacity(&gauge,&before));
    /* Delayed worker starts 100 ms before expiry. A valid 180 ms bracket
     * finishes after it; the new capacity rounds to the same integer. */
    now=previous.estimate.expires_ns/1000-100000-2000000;
    adc_delay_us=30000;
    run_work(&gauge.soc.work);
    assert(!mt6357_soc_read_capacity(&gauge,&after) && before == after);
    struct r1_battery_estimate old;
    assert(!r1_battery_session_read(&previous,gauge.soc.session.estimate.accepted_ns,&old));
    assert(old.validity != R1_BATTERY_FRESH && notifications == 2); soc_cases++;
}
static void counter_epochs(void)
{
    direct_init(); assert(!mt6357_soc_seed(&gauge)); assert(!mt6357_soc_start(&gauge)); run_work(&gauge.soc.work);
    r1_battery_int before=gauge.soc.session.estimate.discharged_point_uah;
    r1_battery_int epoch=gauge.soc.session.estimate.epoch;
    for (unsigned int i=latch_count; i<ARRAY_SIZE(car_words); i++) car_words[i]=9400*2048;
    run_work(&gauge.soc.work); assert(!gauge.soc.error);
    assert(gauge.soc.session.estimate.discharged_point_uah>before && gauge.soc.session.estimate.epoch == epoch); soc_cases++;
    for (unsigned int i=latch_count; i<ARRAY_SIZE(car_words); i++) car_words[i]=900000*2048;
    run_work(&gauge.soc.work); int value=-777;
    assert(mt6357_soc_read_capacity(&gauge,&value) == -ENODATA && value == -777);
    assert(gauge.soc.session.estimate.reason == R1_BATTERY_REASON_COUNTER_RATE); soc_cases++;
    /* Next complete sample can seed at its own arbitrary signed CAR baseline. */
    run_work(&gauge.soc.work); assert(!mt6357_soc_read_capacity(&gauge,&value));
    assert(gauge.soc.session.estimate.epoch>epoch); soc_cases++;
    epoch=gauge.soc.session.estimate.epoch;
    now+=61000000; run_work(&gauge.soc.work); assert(!mt6357_soc_read_capacity(&gauge,&value));
    assert(gauge.soc.session.estimate.epoch>epoch); soc_cases++;
    /* A software event token also defeats an equal-time queued publication. */
    mutex_lock(&gauge.soc.lock); gauge.soc.event_token=UINT64_MAX;
    mt6357_soc_event_locked(&gauge,R1_BATTERY_EVENT_RESET); mutex_unlock(&gauge.soc.lock);
    assert(gauge.soc.stopping && gauge.soc.error == -EOVERFLOW);
    value=-777; assert(mt6357_soc_read_capacity(&gauge,&value) == -ENODEV && value == -777); soc_cases++;
}

static void physical_voltage_grid(void)
{
    const int millivolts[]={3300,3350,3400,3450,3500,3550,3600,3700,3800,3900,4000,4100,4200,4300,4320,4330,4340,4350,4370,4380,4390,4400};
    for (unsigned int i=0; i<ARRAY_SIZE(millivolts); i++) {
        direct_init(); raw_values[0]=raw_values[5]=(millivolts[i]*32768+5399)/5400;
        int ret=mt6357_soc_seed(&gauge), capacity=-777;
        u64 margin_ns=now*1000+2000000000ULL;
        struct r1_battery_estimate *e=&gauge.soc.session.estimate;
        if (!ret) assert(!mt6357_soc_read_capacity(&gauge,&capacity));
        else assert(mt6357_soc_read_capacity(&gauge,&capacity)<0 && capacity == -777);
        printf("GRID %d %d %d %lld %lld %lld %lld %lld %lld %llu %llu\n",
            gauge.soc.live.adc[0][0].mv.value,ret,capacity,
            (long long)e->discharged_uah.low,(long long)e->discharged_uah.high,
            (long long)e->usable_uah.low,(long long)e->usable_uah.high,
            (long long)e->soc_point_bp,(long long)e->accepted_ns,
            (unsigned long long)(gauge.soc.live.finished_ns-gauge.soc.live.started_ns),
            (unsigned long long)margin_ns);
        soc_cases++;
    }
}
int main(void)
{
    seed_and_cache(); percentages(); adapter(); worker_and_pm(); init_and_seed_failures(); startup_deadline(); probes();
    entered_worker_lifetime(); expiry_during_collection(); counter_epochs(); physical_voltage_grid(); cleanup();
    printf("PASS: %u experimental SOC adapter/lifecycle cases\n",soc_cases);
    return 0;
}
