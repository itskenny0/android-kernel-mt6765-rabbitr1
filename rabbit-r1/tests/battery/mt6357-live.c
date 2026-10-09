/* SPDX-License-Identifier: GPL-2.0-only */
/* Included after the unmodified production gauge body and shared stock oracle. */
static pthread_mutex_t info_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t pause_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t pause_cond=PTHREAD_COND_INITIALIZER;
static bool provider_alive=true, pause_entered, pause_continue;
static unsigned int raw_calls, scale_calls, pause_raw;
static int raw_values[6], raw_errors[6], scale_errors[6], raw_formats[6], scale_formats[6];
static int scale_nums[6], scale_dens[6];
static unsigned int adc_order[6], adc_latch[6];
static bool enforce_bracket, remove_before_scale;
static unsigned int cases, scale_cases;

static int iio_read_channel_raw(struct iio_channel *chan, int *v)
{
    assert(!pthread_mutex_lock(&info_lock));
    if (!provider_alive) { *v=INT_MIN; pthread_mutex_unlock(&info_lock); return -ENODEV; }
    unsigned int call=raw_calls++;
    assert(call<6 && chan->id<3);
    if (enforce_bracket) {
        assert(holding && gauge.lock.owner == thread_id);
        assert(!(regs[STOCK_CTRL_REG]&(STOCK_CTRL_MASK|STOCK_CLEAR_MASK|STOCK_READY_MASK)));
    }
    adc_order[call]=chan->id; adc_latch[call]=latch_count; now+=17;
    if (pause_raw == call+1) {
        assert(!pthread_mutex_lock(&pause_lock)); pause_entered=true;
        assert(!pthread_cond_broadcast(&pause_cond));
        while (!pause_continue) assert(!pthread_cond_wait(&pause_cond,&pause_lock));
        assert(!pthread_mutex_unlock(&pause_lock));
    }
    *v=raw_errors[call] ? INT_MIN : raw_values[call];
    int ret=raw_errors[call] ?: raw_formats[call];
    assert(!pthread_mutex_unlock(&info_lock)); return ret;
}
static int iio_read_channel_scale(struct iio_channel *chan, int *n, int *d)
{
    assert(!pthread_mutex_lock(&info_lock));
    unsigned int call=scale_calls++;
    assert(call<6); now+=3;
    if (remove_before_scale) provider_alive=false;
    if (!provider_alive) { *n=*d=INT_MIN; pthread_mutex_unlock(&info_lock); return -ENODEV; }
    int ret=scale_errors[call] ?: scale_formats[call];
    if (ret<0) *n=*d=INT_MIN; /* Failed transport may scribble destinations. */
    else {
        *n=scale_nums[call];
        if (ret == IIO_VAL_FRACTIONAL) *d=scale_dens[call];
        /* A successful integer-format provider does not initialize val2. */
    }
    assert(!pthread_mutex_unlock(&info_lock)); return ret;
}

static void live_setup(bool inherited)
{
    setup(inherited,3182); adc_setup();
    gauge.live_generation=77; gauge.live_sequence=0;
    for (unsigned int i=0; i<ARRAY_SIZE(car_words); i++) {
        current_words[i]=i%2 ? 62353 : 3182;
        car_words[i]=(10000+i)*2048;
        baton_words[i]=1;
    }
    latch_count=raw_calls=scale_calls=pause_raw=0;
    provider_alive=true; pause_entered=pause_continue=false; enforce_bracket=true;
    links=stage=fail_stage=log_lines=0; adc_provider.links.status=DL_DEV_DRIVER_BOUND;
    assert(!provider_refs && !node_refs); of_name_error=of_parse_error=0; missing_provider=self_provider=false;
    stale_channels=remove_before_scale=provider_busy=provider_unbound=false; assert(!provider_locked);
    for (unsigned int i=0; i<3; i++) {
        adc_specs[i]=(struct iio_chan_spec){{'u',i ? 12 : 15},false};
    }
    for (unsigned int i=0; i<6; i++) {
        unsigned int channel=i<3 ? i : 5-i;
        raw_values[i]=channel == 0 ? 23060 : channel == 1 ? 1560 : 4000;
        raw_errors[i]=scale_errors[i]=0;
        raw_formats[i]=IIO_VAL_INT; scale_formats[i]=IIO_VAL_FRACTIONAL;
        scale_nums[i]=channel ? 1800 : 5400;
        scale_dens[i]=channel ? 4096 : 32768;
        adc_order[i]=adc_latch[i]=UINT_MAX;
    }
}
static int expected_charge(unsigned int raw)
{
    unsigned int field=(raw>>11)&0xfffff;
    uint64_t mag=(field == 0 || field == 0xfffff) ? 0 : raw>>31 ? 0xfffff-field : field;
    unsigned int ua=((mag*11176/10000+5)/10)*100;
    return raw>>31 ? -(int)ua : (int)ua;
}
static void assert_success(const struct mt6357_live_observation *s)
{
    assert(!s->error && s->generation == gauge.live_generation && s->sequence == gauge.live_sequence);
    assert(s->started_ns<s->finished_ns && s->started_ns<s->fg[0].started_ns);
    assert(s->fg[0].finished_ns<s->adc[0][0].started_ns);
    assert(s->adc[0][0].finished_ns<s->adc[0][1].started_ns);
    assert(s->adc[0][1].finished_ns<s->adc[0][2].started_ns);
    assert(s->adc[0][2].finished_ns<s->adc[1][2].started_ns);
    assert(s->adc[1][2].finished_ns<s->adc[1][1].started_ns);
    assert(s->adc[1][1].finished_ns<s->adc[1][0].started_ns);
    assert(s->adc[1][0].finished_ns<s->fg[1].started_ns && s->fg[1].finished_ns<s->finished_ns);
    for (unsigned int set=0; set<2; set++) {
        const struct mt6357_live_fg *fg=&s->fg[set];
        assert(!fg->prepare_error && !fg->latch_error && !fg->release_error);
        assert(fg->started_ns<fg->ready_ns && fg->ready_ns<fg->finished_ns);
        assert(fg->words[MT6357_LIVE_CURRENT].value == (int)current_words[set]);
        assert(fg->words[MT6357_LIVE_CAR_LOW].value == (int)(car_words[set]&0xffff));
        assert(fg->words[MT6357_LIVE_CAR_HIGH].value == (int)(car_words[set]>>16));
        assert(fg->current_ua.value == expected(current_words[set]) && !fg->current_ua.error);
        assert(fg->charge_uah.value == expected_charge(car_words[set]) && !fg->charge_uah.error);
        assert(!fg->present.error && fg->present.value == !(baton_words[set]&2));
        assert(!s->temperature_decic[set].error);
        assert(s->temperature_decic[set].value == stock_temperature(
            s->adc[set][1].mv.value,s->adc[set][2].mv.value,expected(current_words[set])));
    }
    for (unsigned int i=0; i<6; i++) {
        assert(adc_order[i] == (i<3 ? i : 5-i));
        assert(adc_latch[i] == 1);
    }
    assert(latch_count == 2 && data_reads == 6 && raw_calls == 6 && scale_calls == 6);
    assert(!gauge.needs_release && !holding);
}
static void success_and_identity(void)
{
    struct mt6357_live_observation s;
    for (unsigned int inherited=0; inherited<2; inherited++) {
        live_setup(inherited); ready_delay=200; clear_delay=100;
        assert(!mt6357_gauge_collect_live(&gauge,&s)); assert_success(&s); cases++;
    }
    /* Absence is independently valid evidence, not an accepted seed. */
    live_setup(false); baton_words[0]=baton_words[1]=3;
    assert(!mt6357_gauge_collect_live(&gauge,&s)); assert_success(&s); cases++;
    /* Every special signed/current and CAR code remains raw evidence. */
    const unsigned int currents[]={0,1,0x7fff,0x8000,0xfffe,0xffff};
    const unsigned int cars[]={0,0x80000000,0x7ffff800,0xfffff800,0x80000800,0x7ffff000};
    for (unsigned int i=0; i<ARRAY_SIZE(currents); i++) {
        live_setup(false); current_words[0]=current_words[1]=currents[i];
        car_words[0]=car_words[1]=cars[i];
        assert(!mt6357_gauge_collect_live(&gauge,&s)); assert_success(&s); cases++;
    }
    live_setup(false); assert(mt6357_gauge_collect_live(&gauge,NULL) == -EINVAL);
    assert(!nops && !raw_calls && !gauge.live_sequence); cases++;
    gauge.live_sequence=U64_MAX; memset(&s,0xa5,sizeof(s));
    assert(mt6357_gauge_collect_live(&gauge,&s) == -EOVERFLOW);
    assert(s.error == -EOVERFLOW && s.sequence == 0 && s.generation == 77);
    assert(!nops && !raw_calls && s.fg[0].prepare_error == -ENODATA);
    assert(s.adc[1][2].raw.error == -ENODATA && gauge.live_sequence == U64_MAX); cases++;
    live_setup(false); assert(!mt6357_gauge_collect_live(&gauge,&s));
    struct mt6357_live_observation saved=s;
    raw_calls=scale_calls=0; assert(!mt6357_gauge_collect_live(&gauge,&s));
    assert(s.sequence == 2 && saved.sequence == 1 && saved.fg[0].words[MT6357_LIVE_CURRENT].value == 3182);
    assert(memcmp(&s,&saved,sizeof(s))); cases++;
    /* Ordinary wrappers retain only their requested data registers. */
    live_setup(false); int result;
    assert(!mt6357_gauge_read_current(&gauge,&result)); assert(data_reads == 1 && latch_count == 1 && !raw_calls);
    assert(!mt6357_gauge_read_charge(&gauge,&result)); assert(data_reads == 3 && latch_count == 2 && !raw_calls); cases++;
}
static void fg_failures(void)
{
    struct mt6357_live_observation s;
    for (unsigned int inherited=0; inherited<2; inherited++) {
        live_setup(inherited); assert(!mt6357_gauge_collect_live(&gauge,&s));
        unsigned int count=nops;
        for (unsigned int op=0; op<count; op++)
        for (unsigned int effect=0; effect<2; effect++) {
            live_setup(inherited); errors[op]=EIO; effect_on_error=effect;
            memset(&s,0xa5,sizeof(s)); assert(mt6357_gauge_collect_live(&gauge,&s) == -EIO);
            assert(s.error == -EIO && s.sequence == 1 && !holding);
            for (unsigned int set=0; set<2; set++) {
                for (unsigned int word=0; word<MT6357_LIVE_WORDS; word++)
                    if (s.fg[set].words[word].error) assert(!s.fg[set].words[word].value);
                if (s.fg[set].current_ua.error) assert(!s.fg[set].current_ua.value);
                if (s.fg[set].charge_uah.error) assert(!s.fg[set].charge_uah.value);
            }
            if (raw_calls) assert(s.fg[1].prepare_error != -ENODATA);
            else assert(s.adc[0][0].raw.error == -ENODATA && s.fg[1].prepare_error == -ENODATA);
            /* Recovery uses a new latch; failed data never becomes cached success. */
            memset(errors,0,sizeof(errors)); raw_calls=scale_calls=0; effect_on_error=false;
            assert(!mt6357_gauge_collect_live(&gauge,&s) && s.sequence == 2 && !gauge.needs_release);
            cases++;
        }
    }
    /* Retain a primary data failure and a different cleanup failure together. */
    live_setup(false); assert(!mt6357_gauge_collect_live(&gauge,&s));
    unsigned int current_op=UINT_MAX, clear_op=UINT_MAX;
    for (unsigned int op=0; op<nops; op++) {
        if (current_op == UINT_MAX && trace[op].reg == STOCK_DATA_REG) current_op=op;
        if (current_op != UINT_MAX && trace[op].op == 'w' && (trace[op].value&STOCK_CLEAR_MASK)) { clear_op=op; break; }
    }
    assert(current_op != UINT_MAX && clear_op != UINT_MAX);
    live_setup(false); errors[current_op]=EIO; errors[clear_op]=ENXIO;
    assert(mt6357_gauge_collect_live(&gauge,&s) == -EIO);
    assert(s.fg[0].words[MT6357_LIVE_CURRENT].error == -EIO && s.fg[0].release_error == -ENXIO);
    assert(s.fg[0].words[MT6357_LIVE_CAR_LOW].error == 0 && !raw_calls && gauge.needs_release); cases++;
    for (unsigned int mode=0; mode<6; mode++) {
        live_setup(mode == 5);
        if (mode == 0) regs[STOCK_ON_REG]=0;
        if (mode == 1) regs[STOCK_DIG_PD_REG]=STOCK_DIG_PD_MASK;
        if (mode == 2) regs[STOCK_DIG_PD_REG]=STOCK_ANA_PD_MASK;
        if (mode == 3) stall_start=true;
        if (mode>=4) stall_stop=true;
        int expected_error=mode<3 ? -EAGAIN : -ETIMEDOUT;
        assert(mt6357_gauge_collect_live(&gauge,&s) == expected_error && !raw_calls);
        assert(s.error == expected_error && !holding); cases++;
    }
    /* Detector-off status is separate from successfully converted FG data. */
    for (unsigned int set=0; set<2; set++) {
        live_setup(false); baton_words[set]=0;
        assert(mt6357_gauge_collect_live(&gauge,&s) == -EAGAIN);
        assert(!s.fg[set].current_ua.error && !s.fg[set].charge_uah.error);
        assert(s.fg[set].present.error == -EAGAIN && !s.fg[set].words[MT6357_LIVE_BATON].error);
        assert(raw_calls == 6 && latch_count == 2); cases++;
    }
}
static void adc_failures_and_scaling(void)
{
    struct mt6357_live_observation s;
    const int errors_to_inject[]={-EIO,-ENODEV,-ETIMEDOUT};
    for (unsigned int i=0; i<ARRAY_SIZE(errors_to_inject); i++)
    for (unsigned int stage_id=0; stage_id<2; stage_id++)
    for (unsigned int call=0; call<6; call++) {
        live_setup(false);
        (stage_id ? scale_errors : raw_errors)[call]=errors_to_inject[i];
        assert(mt6357_gauge_collect_live(&gauge,&s) == errors_to_inject[i]);
        assert(s.error == errors_to_inject[i] && raw_calls == 6 && latch_count == 2);
        assert(!s.fg[1].release_error);
        unsigned int set=call/3, channel=set ? 5-call : call;
        const struct mt6357_live_adc *a=&s.adc[set][channel];
        assert(a->mv.error == errors_to_inject[i] && !a->mv.value);
        if (stage_id) assert(!a->raw.error && a->raw.value == raw_values[call] && !a->scale_numerator && !a->scale_denominator);
        else assert(a->raw.error == errors_to_inject[i] && !a->raw.value && a->scale_error == -ENODATA);
        cases++;
    }
    for (unsigned int bad=0; bad<16; bad++) {
        struct mt6357_live_adc a;
        live_setup(false); enforce_bracket=false;
        struct iio_channel *channel=&channels[0];
        int expected_error=-ERANGE;
        switch (bad) {
        case 0: channel=NULL; expected_error=-ENODATA; break;
        case 1: adc_specs[0].has_offset=true; expected_error=-EOPNOTSUPP; break;
        case 2: adc_specs[0].scan_type.sign='s'; expected_error=-EOPNOTSUPP; break;
        case 3: adc_specs[0].scan_type.realbits=0; expected_error=-EOPNOTSUPP; break;
        case 4: adc_specs[0].scan_type.realbits=32; expected_error=-EOPNOTSUPP; break;
        case 5: raw_values[0]=-1; break;
        case 6: raw_values[0]=32768; break;
        case 7: raw_formats[0]=IIO_VAL_FRACTIONAL; expected_error=-EOPNOTSUPP; break;
        case 8: scale_formats[0]=IIO_VAL_INT; expected_error=-EOPNOTSUPP; break;
        case 9: scale_nums[0]=0; break;
        case 10: scale_dens[0]=0; break;
        case 11: scale_nums[0]=-1; break;
        case 12: scale_dens[0]=-1; break;
        case 13: scale_nums[0]=INT_MAX; scale_dens[0]=1; break;
        case 14: adc_specs[0].scan_type.realbits=31; raw_values[0]=INT_MAX; scale_nums[0]=INT_MAX; scale_dens[0]=INT_MAX; break;
        case 15: raw_values[0]=INT_MIN; break;
        }
        memset(&a,0xa5,sizeof(a));
        assert(mt6357_gauge_live_adc(channel,&a) == expected_error && a.mv.error == expected_error && !a.mv.value);
        assert(a.started_ns<a.finished_ns);
        if (bad == 8) assert(a.scale_denominator == 0); /* unsupported format left val2 untouched */
        cases++;
    }
    for (unsigned int channel=0; channel<3; channel++) {
        live_setup(false); enforce_bracket=false;
        unsigned int denominator=channel ? 4096 : 32768, numerator=channel ? 1800 : 5400;
        for (unsigned int raw=0; raw<denominator; raw++) {
            struct mt6357_live_adc a;
            raw_calls=scale_calls=0; raw_values[0]=raw;
            scale_nums[0]=numerator; scale_dens[0]=denominator;
            assert(!mt6357_gauge_live_adc(&channels[channel],&a));
            assert(a.raw.value == (int)raw && !a.raw.error && !a.scale_error && !a.mv.error);
            assert(a.mv.value == (int)((uint64_t)raw*numerator/denominator)); scale_cases++;
        }
    }
    /* Zero terminal voltage and invalid divider data cannot become complete bundles. */
    for (unsigned int bad=0; bad<4; bad++) {
        live_setup(false);
        if (!bad) raw_values[0]=0;
        if (bad == 1) raw_values[1]=0;
        if (bad == 2) raw_values[1]=raw_values[2];
        if (bad == 3) raw_values[2]=0;
        assert(mt6357_gauge_collect_live(&gauge,&s) == -ERANGE && latch_count == 2); cases++;
    }
    live_setup(false); gauge.reference=NULL;
    assert(mt6357_gauge_collect_live(&gauge,&s) == -ENODATA && latch_count == 2);
    assert(s.adc[0][2].raw.error == -ENODATA && s.temperature_decic[0].error == -ENODATA); cases++;
}

static _Atomic bool competing_started, competing_done, removal_started, removal_done;
static struct mt6357_live_observation threaded;
static int threaded_result;
static void *collect_thread(void *unused)
{ thread_id=2; threaded_result=mt6357_gauge_collect_live(&gauge,&threaded); return NULL; }
static void *current_thread(void *unused)
{ thread_id=3; competing_started=true; int v; assert(!mt6357_gauge_read_current(&gauge,&v)); competing_done=true; return NULL; }
static void *remove_provider(void *unused)
{
    removal_started=true; pthread_mutex_lock(&info_lock); provider_alive=false;
    pthread_mutex_unlock(&info_lock); removal_done=true; return NULL;
}
static void await_pause(void)
{
    pthread_mutex_lock(&pause_lock);
    while (!pause_entered) pthread_cond_wait(&pause_cond,&pause_lock);
    pthread_mutex_unlock(&pause_lock);
}
static void unpause(void)
{
    pthread_mutex_lock(&pause_lock); pause_continue=true;
    pthread_cond_broadcast(&pause_cond); pthread_mutex_unlock(&pause_lock);
}
static void concurrency(void)
{
    for (unsigned int call=1; call<=6; call++) {
        live_setup(false); pause_raw=call; competing_started=competing_done=false;
        pthread_t collection,current;
        assert(!pthread_create(&collection,NULL,collect_thread,NULL)); await_pause();
        unsigned int count=nops;
        assert(!pthread_create(&current,NULL,current_thread,NULL));
        while (!competing_started) sched_yield();
        assert(!competing_done && nops == count && latch_count == 1);
        unpause(); pthread_join(collection,NULL); pthread_join(current,NULL);
        assert(!threaded_result && competing_done && latch_count == 3 && data_reads == 7);
        bool seen_other=false;
        for (unsigned int i=0; i<nops; i++) {
            if (trace[i].thread == 3) seen_other=true;
            else assert(trace[i].thread == 2 && !seen_other);
        }
        cases++;
    }
    for (unsigned int call=1; call<=6; call++) {
        live_setup(false); pause_raw=call; removal_started=removal_done=false;
        pthread_t collection,removal;
        assert(!pthread_create(&collection,NULL,collect_thread,NULL)); await_pause();
        assert(!pthread_create(&removal,NULL,remove_provider,NULL));
        while (!removal_started) sched_yield();
        assert(!removal_done); /* entered IIO callback holds info_exist_lock model */
        unpause(); pthread_join(collection,NULL); pthread_join(removal,NULL);
        /* Scheduling may finish the bracket before unregister acquires info_lock. */
        assert((!threaded_result || threaded_result == -ENODEV) && removal_done);
        assert(!threaded.fg[1].release_error && latch_count == 2);
        raw_calls=scale_calls=0;
        struct mt6357_live_observation next;
        assert(mt6357_gauge_collect_live(&gauge,&next) == -ENODEV);
        assert(next.adc[0][0].raw.error == -ENODEV && !next.adc[0][0].raw.value);
        cases++;
    }
    /* Deterministic removal after raw succeeds and before scale can enter. */
    live_setup(false); enforce_bracket=false; remove_before_scale=true;
    struct mt6357_live_adc a;
    assert(mt6357_gauge_live_adc(&channels[0],&a) == -ENODEV);
    assert(!a.raw.error && a.raw.value == raw_values[0]);
    assert(a.scale_error == -ENODEV && !a.scale_numerator && !a.scale_denominator && !a.mv.value); cases++;

}
static void probe_and_links(void)
{
    destroy_lock();
    struct platform_device pdev={.dev={.parent=&pmic_dev,.of_node=&adc_provider}};
    u64 prior_generation=0;
    for (unsigned int mode=0; mode<4; mode++) {
        live_setup(false); adc_present=true; gain_present=true;
        if (mode == 1) regs[STOCK_ON_REG]=0;
        if (mode == 2) raw_errors[0]=-EIO;
        if (mode == 3) fail_registration=true;
        unsigned int before=registrations;
        int ret=mt6357_gauge_probe(&pdev);
        assert(ret == (mode == 3 ? -EIO : 0));
        assert(registrations == before+(mode != 3));
        assert(gauge.live_generation>prior_generation && links == 3);
        prior_generation=gauge.live_generation;
        if (CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS) {
            assert(gauge.live_sequence == 1 && log_lines && nops);
            assert(raw_calls == (mode == 1 ? 0U : 6U));
        } else assert(!gauge.live_sequence && !log_lines && !nops && !raw_calls);
        assert(!provider_refs && !node_refs && !provider_locked);
        destroy_lock(); fail_registration=false; cases++;
    }
    /* Required supplier-link failure aborts before optional diagnostic reads. */
    live_setup(false); adc_present=true; gain_present=true;
    assert(!mt6357_gauge_probe(&pdev)); unsigned int stages=stage; destroy_lock();
    for (unsigned int f=1; f<=stages; f++) {
        live_setup(false); stage=0; fail_stage=f;
        assert(mt6357_gauge_probe(&pdev) < 0); destroy_lock(); cases++;
    }
    fail_stage=0; init_lock(); live_setup(false); gauge.dev=&pdev.dev;
    struct iio_channel *channel=(void *)1;
    for (unsigned int bad=0; bad<11; bad++) {
        live_setup(false); gauge.dev=&pdev.dev;
        int expected_error=-EPROBE_DEFER;
        if (bad == 0) provider_busy=true;
        if (bad == 1) provider_unbound=true;
        if (bad == 2) adc_provider.links.status=DL_DEV_UNBINDING;
        if (bad == 3) stale_channels=true;
        if (bad == 4) missing_provider=true;
        if (bad == 5) of_name_error=expected_error=-ENODATA;
        if (bad == 6) of_parse_error=expected_error=-EPROTO;
        if (bad == 7) { adc_iio.dev.parent=(void *)0xdead; expected_error=-EINVAL; }
        if (bad == 8) { channel_type_error[0]=expected_error=-EIO; }
        if (bad == 9) { channels[0].type=IIO_TEMP; expected_error=-EINVAL; }
        if (bad == 10) { self_provider=true; expected_error=-EINVAL; }
        channel=(void *)1;
        assert(mt6357_gauge_get_channel(&gauge,"battery-voltage",&channel) == expected_error);
        assert(channel == (void *)1 && !provider_refs && !node_refs && !provider_locked);
        assert(!nops && !raw_calls); adc_iio.dev.parent=&adc_provider; cases++;
    }
    live_setup(false); gauge.dev=&pdev.dev;
    assert(!mt6357_gauge_get_channel(&gauge,"battery-voltage",&channel));
    assert(channel == &channels[0] && !provider_refs && !node_refs && !provider_locked); cases++;

}
int main(void)
{
    init_lock(); success_and_identity(); fg_failures(); adc_failures_and_scaling(); concurrency(); probe_and_links(); destroy_lock();
    printf("PASS: diagnostics=%d, %u live collector ordering/error/identity/probe/concurrency cases and %u exact ADC code conversions (ASan/UBSan)\n",
           CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS,cases,scale_cases);
    return 0;
}
