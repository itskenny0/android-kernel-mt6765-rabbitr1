/* SPDX-License-Identifier: GPL-2.0-only */
#include "session.h"
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned int checks;
#define CHECK(condition) do { checks++; if (!(condition)) { \
    fprintf(stderr, "session check failed line %d: %s\n", __LINE__, #condition); abort(); \
} } while (0)

static void read_values(FILE *file, r1_battery_int *v, size_t count)
{
    size_t i;
    for (i = 0; i < count; i++) assert(fscanf(file, "%" SCNd64, &v[i]) == 1);
}

static struct r1_battery_policy policy_values(const r1_battery_int *v)
{
    return (struct r1_battery_policy) {
        .minimum_01mv=v[0], .discharge_01ma=v[1], .shunt_01mohm=v[2],
        .meter_01mohm=v[3], .dc_ratio_percent=v[4], .max_capture_ns=v[5],
        .max_gap_ns=v[6], .max_epoch_ns=v[7], .valid_for_ns=v[8],
        .observed_current_limit_ua=v[9], .interval_current_bound_ua=v[10],
        .voltage_motion_uv=v[11], .current_motion_ua=v[12], .temperature_motion_decic=v[13],
        .model_error_uah=v[14], .cutoff_error_uah=v[15], .car_delta_error_uah=v[16],
        .max_charge_width_uah=v[17], .max_soc_width_bp=v[18],
    };
}

static struct r1_battery_observation observation_values(const r1_battery_int *v)
{
    struct r1_battery_observation o = {
        .generation=v[0], .sequence=v[1], .started_ns=v[2], .finished_ns=v[3],
        .error=(int)v[4], .event=(enum r1_battery_event)v[5],
    };
    size_t i;
    for (i = 0; i < 2; i++) {
        const r1_battery_int *e = v+6+i*7;
        o.endpoint[i] = (struct r1_battery_endpoint) {
            .error=(int)e[0], .present=(int)e[1], .raw_car=e[2], .car_uah=e[3],
            .current_ua=e[4], .voltage_uv=e[5], .temperature_decic=e[6],
        };
    }
    return o;
}

static void check_result(const struct r1_battery_estimate *e, const r1_battery_int *v,
                          unsigned int operation)
{
    r1_battery_int result[] = {e->validity,e->reason,e->detail_error,e->event,e->epoch,
        e->generation,e->sequence,e->accepted_ns,e->expires_ns,e->temperature_c,
        e->discharged_uah.low,e->discharged_uah.high,e->usable_uah.low,e->usable_uah.high,
        e->soc_bp.low,e->soc_bp.high,e->discharged_point_uah,e->usable_point_uah,
        e->soc_point_bp,e->boundary};
    size_t i;
    for (i = 0; i < sizeof(result)/sizeof(*result); i++) {
        if (result[i] != v[i]) {
            fprintf(stderr, "reference operation %u field %zu expected %" PRId64
                    " actual %" PRId64 "\n", operation, i, v[i], result[i]);
            abort();
        }
    }
}

static unsigned int reference(const char *path)
{
    FILE *f = fopen(path, "r");
    struct r1_battery_session s = {0}, saved;
    struct r1_battery_estimate e;
    struct r1_battery_policy p = {0}, saved_p;
    struct r1_battery_observation o, saved_o;
    r1_battery_int values[40];
    unsigned int operations = 0;
    char kind;
    assert(f);
    while (fscanf(f, " %c", &kind) == 1) {
        if (kind == 'Z') { s = (struct r1_battery_session){0}; continue; }
        if (kind == 'P') { read_values(f,values,19); p=policy_values(values); continue; }
        operations++;
        saved_p = p;
        if (kind == 'O') {
            read_values(f,values,40); o=observation_values(values); saved_o=o;
            assert(!r1_battery_session_step(&s,&p,&o,&e));
            assert(!memcmp(&o,&saved_o,sizeof(o)));
            check_result(&e,values+20,operations);
        } else {
            assert(kind=='R'); read_values(f,values,21); saved=s;
            assert(!r1_battery_session_read(&s,values[0],&e));
            assert(!memcmp(&s,&saved,sizeof(s)));
            check_result(&e,values+1,operations);
        }
        assert(!memcmp(&p,&saved_p,sizeof(p)));
    }
    assert(!fclose(f));
    return operations;
}

/* Hand-derived exact row fixture. Stock 25 C row1=(201,43580,3004),
 * row35=(7222,37730,2962). Under -1 A and 175 extra resistance units,
 * row35 loaded voltage is 37730-(2962+175)=34593. Earlier rows are above it.
 * With zero capture duration and zero error assumptions, these exact roots
 * conservatively expand to [20100,20200] and [722200,722300] uAh. */
static struct r1_battery_policy base_policy(void)
{
    const r1_battery_int values[] = {34593,10000,100,75,100,1000000000,120000000000,
        600000000000,60000000000,1000000,2000000,20000,300000,9,0,0,200,20000,500};
    return policy_values(values);
}

static struct r1_battery_observation base_observation(void)
{
    struct r1_battery_endpoint endpoint = {.present=1,.raw_car=10000LL<<11,
        .car_uah=111800,.current_ua=0,.voltage_uv=4358000,.temperature_decic=250};
    return (struct r1_battery_observation){.generation=1,.sequence=1,
        .started_ns=10000000000,.finished_ns=10000000000,.endpoint={endpoint,endpoint}};
}

static struct r1_battery_estimate step(struct r1_battery_session *s,
                                       const struct r1_battery_policy *p,
                                       const struct r1_battery_observation *o)
{
    struct r1_battery_estimate e;
    CHECK(!r1_battery_session_step(s,p,o,&e));
    return e;
}

static void boundaries(void)
{
    struct r1_battery_policy p = base_policy(), bad;
    struct r1_battery_observation o = base_observation();
    struct r1_battery_session s = {0}, saved, zero = {0};
    struct r1_battery_estimate e, output, untouched;
    unsigned int i;
    r1_battery_int offsets_positive[] = {
#define OFF(field) (r1_battery_int)offsetof(struct r1_battery_policy,field)
        OFF(minimum_01mv),OFF(discharge_01ma),OFF(dc_ratio_percent),OFF(max_capture_ns),
        OFF(max_gap_ns),OFF(max_epoch_ns),OFF(valid_for_ns),OFF(interval_current_bound_ua),
        OFF(max_charge_width_uah),OFF(max_soc_width_bp),
    };
    r1_battery_int offsets_nonnegative[] = {OFF(shunt_01mohm),OFF(meter_01mohm),
        OFF(observed_current_limit_ua),OFF(voltage_motion_uv),OFF(current_motion_ua),
        OFF(temperature_motion_decic),OFF(model_error_uah),OFF(cutoff_error_uah),
        OFF(car_delta_error_uah)};
#undef OFF
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_FRESH && e.reason==R1_BATTERY_REASON_SEEDED);
    CHECK(e.discharged_uah.low==20100 && e.discharged_uah.high==20200);
    CHECK(e.usable_uah.low==722200 && e.usable_uah.high==722300);
    CHECK(e.discharged_point_uah==20150 && e.usable_point_uah==722250);
    CHECK(e.soc_point_bp==9721 && e.soc_bp.low==9720 && e.soc_bp.high==9722);
    saved=s;
    memset(&output,0xa5,sizeof(output)); untouched=output;
    for (i=0;i<sizeof(offsets_positive)/sizeof(*offsets_positive);i++) {
        bad=p; *(r1_battery_int *)((char *)&bad+offsets_positive[i])=0;
        CHECK(r1_battery_session_step(&s,&bad,&o,&output)==R1_BATTERY_INVALID);
        CHECK(!memcmp(&s,&saved,sizeof(s)) && !memcmp(&output,&untouched,sizeof(output)));
    }
    for (i=0;i<sizeof(offsets_nonnegative)/sizeof(*offsets_nonnegative);i++) {
        bad=p; *(r1_battery_int *)((char *)&bad+offsets_nonnegative[i])=-1;
        CHECK(r1_battery_session_step(&s,&bad,&o,&output)==R1_BATTERY_INVALID);
        CHECK(!memcmp(&s,&saved,sizeof(s)) && !memcmp(&output,&untouched,sizeof(output)));
    }
    bad=p; bad.valid_for_ns=bad.max_gap_ns+1;
    CHECK(r1_battery_session_step(&s,&bad,&o,&output)==R1_BATTERY_INVALID);
    bad=p; bad.interval_current_bound_ua=bad.observed_current_limit_ua-1;
    CHECK(r1_battery_session_step(&s,&bad,&o,&output)==R1_BATTERY_INVALID);
    CHECK(r1_battery_session_step(NULL,&p,&o,&output)==R1_BATTERY_INVALID);
    CHECK(r1_battery_session_step(&s,NULL,&o,&output)==R1_BATTERY_INVALID);
    CHECK(r1_battery_session_step(&s,&p,NULL,&output)==R1_BATTERY_INVALID);
    CHECK(r1_battery_session_step(&s,&p,&o,NULL)==R1_BATTERY_INVALID);
    o.event=(enum r1_battery_event)99;
    CHECK(r1_battery_session_step(&s,&p,&o,&output)==R1_BATTERY_INVALID);
    CHECK(!memcmp(&s,&saved,sizeof(s)) && !memcmp(&output,&untouched,sizeof(output)));
    CHECK(r1_battery_session_read(NULL,1,&output)==R1_BATTERY_INVALID);
    CHECK(r1_battery_session_read(&s,0,&output)==R1_BATTERY_INVALID);
    CHECK(r1_battery_session_read(&s,1,NULL)==R1_BATTERY_INVALID);
    CHECK(!memcmp(&output,&untouched,sizeof(output)));
    CHECK(!r1_battery_session_read(&s,e.expires_ns,&output));
    CHECK(output.validity==R1_BATTERY_HISTORICAL && output.reason==R1_BATTERY_REASON_WIDTH);
    CHECK(!r1_battery_session_read(&s,e.accepted_ns,&output));
    CHECK(output.validity==R1_BATTERY_FRESH && output.discharged_uah.low==20100);
    CHECK(!r1_battery_session_read(&s,e.accepted_ns+1,&output));
    CHECK(output.validity==R1_BATTERY_FRESH && output.discharged_uah.low==20099 &&
          output.discharged_uah.high==20201 && output.accepted_ns==e.accepted_ns);
    CHECK(!r1_battery_session_read(&s,e.expires_ns+1,&output));
    CHECK(output.validity==R1_BATTERY_HISTORICAL && output.reason==R1_BATTERY_REASON_EXPIRED);
    CHECK(!memcmp(&s,&saved,sizeof(s)));

    /* Valid modeled zero survives; failure preserves it as history, not fresh. */
    s=zero; o=base_observation();
    o.endpoint[0].voltage_uv=o.endpoint[1].voltage_uv=3773000;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_FRESH && e.soc_point_bp==0);
    o.sequence++; o.started_ns++; o.finished_ns++; o.error=-5;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_HISTORICAL && e.soc_point_bp==0 &&
          e.reason==R1_BATTERY_REASON_ACQUISITION && e.accepted_ns==10000000000);
    /* Actual cold repeated-voltage plateau cannot silently choose a root. */
    s=zero; o=base_observation();
    for (i=0;i<2;i++) { o.endpoint[i].temperature_decic=0; o.endpoint[i].voltage_uv=3759000; }
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_UNAVAILABLE && e.reason==R1_BATTERY_REASON_MODEL &&
          e.detail_error==R1_BATTERY_AMBIGUOUS);

    /* Exact inclusive capture/current/motion boundaries and outward timing ceil. */
    s=zero; o=base_observation(); o.finished_ns++;
    e=step(&s,&p,&o);
    CHECK(e.discharged_uah.low==20099 && e.discharged_uah.high==20201);
    s=zero; o=base_observation(); o.finished_ns+=p.max_capture_ns;
    e=step(&s,&p,&o); CHECK(e.validity==R1_BATTERY_FRESH);
    s=zero; o.finished_ns++;
    e=step(&s,&p,&o); CHECK(e.reason==R1_BATTERY_REASON_LIMIT);
    /* Duration product splitting: exactly one hour at 2 A means 2,000,000 uAh;
     * explicit test budget permits the wide interval; this is not board policy. */
    s=zero; o=base_observation(); bad=p;
    bad.max_capture_ns=3600000000000LL; bad.max_charge_width_uah=5000000;
    bad.max_soc_width_bp=100000;
    o.finished_ns+=bad.max_capture_ns;
    e=step(&s,&bad,&o);
    CHECK(e.validity==R1_BATTERY_FRESH && e.discharged_uah.low==-1979900 &&
          e.discharged_uah.high==2020200);
    CHECK(e.soc_bp.low<0 && e.soc_bp.high>10000); /* All signed q/Q corners. */

    /* Program inputs are valid, arithmetic failures are unavailable outcomes. */
    s=zero; o=base_observation(); o.finished_ns++; bad=p; bad.model_error_uah=INT64_MAX;
    e=step(&s,&bad,&o);
    CHECK(e.reason==R1_BATTERY_REASON_ARITHMETIC && e.detail_error==R1_BATTERY_OVERFLOW);
    s=zero; bad=p; bad.cutoff_error_uah=INT64_MAX;
    e=step(&s,&bad,&o);
    CHECK(e.reason==R1_BATTERY_REASON_ARITHMETIC && e.detail_error==R1_BATTERY_OVERFLOW);
    s=zero; bad=p; bad.interval_current_bound_ua=INT64_MAX; o.finished_ns=o.started_ns+2;
    e=step(&s,&bad,&o);
    CHECK(e.reason==R1_BATTERY_REASON_ARITHMETIC && e.detail_error==R1_BATTERY_OVERFLOW);
    s=zero; o=base_observation(); o.started_ns=o.finished_ns=INT64_MAX-10;
    e=step(&s,&p,&o);
    CHECK(e.reason==R1_BATTERY_REASON_ARITHMETIC && e.detail_error==R1_BATTERY_OVERFLOW);
    s=zero; o=base_observation(); bad=p; bad.minimum_01mv=INT64_MAX;
    e=step(&s,&bad,&o);
    CHECK(e.validity==R1_BATTERY_UNAVAILABLE && e.reason==R1_BATTERY_REASON_CUTOFF &&
          e.detail_error==R1_BATTERY_OVERFLOW);

    /* Width inclusivity; no physical-model error is invented to force success. */
    s=zero; bad=p; bad.max_charge_width_uah=100;
    e=step(&s,&bad,&o); CHECK(e.validity==R1_BATTERY_FRESH);
    s=zero; bad.max_charge_width_uah=99;
    e=step(&s,&bad,&o); CHECK(e.reason==R1_BATTERY_REASON_WIDTH);
    s=zero; bad=p; bad.max_soc_width_bp=2;
    e=step(&s,&bad,&o); CHECK(e.validity==R1_BATTERY_FRESH);
    s=zero; bad.max_soc_width_bp=1;
    e=step(&s,&bad,&o); CHECK(e.reason==R1_BATTERY_REASON_WIDTH);

    /* Cache deadline cannot extend a frozen epoch, including a late update.
     * The exact boundary is accepted; one nanosecond beyond it is historical. */
    s=zero; bad=p; bad.max_epoch_ns=10; o=base_observation();
    e=step(&s,&bad,&o); CHECK(e.expires_ns==10000000010);
    o.sequence++; o.started_ns+=8; o.finished_ns+=8;
    e=step(&s,&bad,&o);
    CHECK(e.reason==R1_BATTERY_REASON_TRACKED && e.expires_ns==10000000010);
    CHECK(!r1_battery_session_read(&s,10000000010,&output));
    CHECK(output.validity==R1_BATTERY_FRESH && output.accepted_ns==10000000008);
    CHECK(!r1_battery_session_read(&s,10000000011,&output));
    CHECK(output.validity==R1_BATTERY_HISTORICAL && output.reason==R1_BATTERY_REASON_EXPIRED);
    o.sequence++; o.started_ns+=3; o.finished_ns+=3;
    e=step(&s,&bad,&o);
    CHECK(e.reason==R1_BATTERY_REASON_EPOCH_AGE && e.epoch==2);
    /* In-window counter allowance equality, respecting the actual 100-uAh
     * quantum, versus the next possible converted value above the allowance. */
    s=zero; o=base_observation(); o.endpoint[1].car_uah+=p.car_delta_error_uah;
    e=step(&s,&p,&o); CHECK(e.validity==R1_BATTERY_FRESH);
    s=zero; o.endpoint[1].car_uah+=100;
    e=step(&s,&p,&o); CHECK(e.reason==R1_BATTERY_REASON_COUNTER_RATE);

    /* Raw special aliases are not a modulus. Stable aliases may seed; a
     * within-bracket transition rejects even if both converted CAR values=0. */
    s=zero; o=base_observation();
    for (i=0;i<2;i++) { o.endpoint[i].raw_car=0; o.endpoint[i].car_uah=0; }
    e=step(&s,&p,&o); CHECK(e.validity==R1_BATTERY_FRESH);
    o.sequence++; o.started_ns++; o.finished_ns++;
    o.endpoint[1].raw_car=0xfffffLL<<11;
    e=step(&s,&p,&o);
    CHECK(e.reason==R1_BATTERY_REASON_COUNTER_AMBIGUOUS && e.validity==R1_BATTERY_HISTORICAL);
    CHECK(e.accepted_ns==10000000000);
    /* Event timestamp is a barrier against queued pre-event captures, even
     * if their capture sequence was never previously submitted. */
    s=zero; o=base_observation(); e=step(&s,&p,&o);
    o.event=R1_BATTERY_EVENT_SUSPEND; o.sequence=0;
    o.started_ns=o.finished_ns=30000000000;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_HISTORICAL && e.reason==R1_BATTERY_REASON_EVENT);
    CHECK(s.seen_finished_ns==30000000000 && s.seen_sequence==1);
    o.event=R1_BATTERY_EVENT_NONE; o.sequence=2;
    o.started_ns=o.finished_ns=20000000000;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_HISTORICAL && e.reason==R1_BATTERY_REASON_ORDER);
    CHECK(s.seen_finished_ns==30000000000 && e.accepted_ns==10000000000);
    o.sequence=3; o.started_ns=o.finished_ns=40000000000;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_FRESH && e.epoch==2 && e.reason==R1_BATTERY_REASON_SEEDED);
    /* A new event binding resets capture sequence, never its time barrier. */
    o.event=R1_BATTERY_EVENT_RESET; o.generation=2; o.sequence=0;
    o.started_ns=o.finished_ns=50000000000;
    e=step(&s,&p,&o);
    CHECK(s.seen_sequence==0 && s.seen_generation==2);
    o.event=R1_BATTERY_EVENT_NONE; o.sequence=1;
    o.started_ns=o.finished_ns=60000000000;
    e=step(&s,&p,&o); CHECK(e.validity==R1_BATTERY_FRESH && e.epoch==3);
    /* Equal event/capture clocks cannot establish causality, even when an
     * in-flight capture finishes later. One tick later may seed; ordinary
     * adjacent captures can still share their endpoint/start timestamp. */
    saved=s;
    o.event=R1_BATTERY_EVENT_RESET; o.sequence=0;
    o.started_ns=o.finished_ns=70000000000;
    e=step(&s,&p,&o); CHECK(s.event_barrier_ns==70000000000);
    o.event=R1_BATTERY_EVENT_NONE; o.sequence=2;
    o.started_ns=o.finished_ns=69999999999;
    e=step(&s,&p,&o); CHECK(e.reason==R1_BATTERY_REASON_ORDER);
    o.started_ns=o.finished_ns=70000000000;
    e=step(&s,&p,&o); CHECK(e.reason==R1_BATTERY_REASON_ORDER && !s.active);
    o.finished_ns++;
    e=step(&s,&p,&o);
    CHECK(e.reason==R1_BATTERY_REASON_ORDER && s.seen_finished_ns==70000000000);
    o.started_ns++;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_FRESH && e.reason==R1_BATTERY_REASON_SEEDED);
    o.sequence++;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_FRESH && e.reason==R1_BATTERY_REASON_TRACKED);
    s=saved;
    /* Missing event time blocks all reseeding until a trusted dated event;
     * neither a later sequence nor ordinary sample timestamps repair it. */
    o.event=R1_BATTERY_EVENT_RESET; o.started_ns=o.finished_ns=0;
    e=step(&s,&p,&o); CHECK(s.barrier_missing && e.validity==R1_BATTERY_HISTORICAL);
    o.event=R1_BATTERY_EVENT_NONE; o.sequence=2;
    o.started_ns=o.finished_ns=70000000000;
    e=step(&s,&p,&o);
    CHECK(s.barrier_missing && e.validity==R1_BATTERY_HISTORICAL && e.reason==R1_BATTERY_REASON_ORDER);
    o.event=R1_BATTERY_EVENT_RESET;
    e=step(&s,&p,&o); CHECK(!s.barrier_missing && e.reason==R1_BATTERY_REASON_EVENT);
    o.event=R1_BATTERY_EVENT_NONE; o.sequence=3;
    o.started_ns=o.finished_ns=80000000000;
    e=step(&s,&p,&o); CHECK(e.validity==R1_BATTERY_FRESH && e.epoch==4);
    /* Endpoint error is not hidden by an otherwise populated report. */
    s=zero; o=base_observation(); o.endpoint[1].error=-11;
    e=step(&s,&p,&o);
    CHECK(e.validity==R1_BATTERY_UNAVAILABLE && e.detail_error==-11);
}

int main(int argc, char **argv)
{
    unsigned int operations;
    assert(argc==2);
    operations=reference(argv[1]);
    boundaries();
    printf("Battery session: %u rational-reference operations + %u hand-derived/fault assertions passed (ASan/UBSan)\n",
           operations,checks);
    return 0;
}
