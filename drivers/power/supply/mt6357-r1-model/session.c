/* SPDX-License-Identifier: GPL-2.0-only */
#include "session.h"

#define LIMIT64 9223372036854775807LL
#define MIN64 (-LIMIT64 - 1LL)
#define NS_UA_PER_UAH 3600000000000LL

static int sum(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if ((b > 0 && a > LIMIT64 - b) || (b < 0 && a < MIN64 - b))
        return R1_BATTERY_OVERFLOW;
    *out = a + b;
    return 0;
}

static int difference(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if ((b > 0 && a < MIN64 + b) || (b < 0 && a > LIMIT64 + b))
        return R1_BATTERY_OVERFLOW;
    *out = a - b;
    return 0;
}

static int product(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if (a > 0) {
        if ((b > 0 && a > LIMIT64 / b) || (b < 0 && b < MIN64 / a))
            return R1_BATTERY_OVERFLOW;
    } else if (a < 0) {
        if ((b > 0 && a < MIN64 / b) || (b < 0 && a < LIMIT64 / b))
            return R1_BATTERY_OVERFLOW;
    }
    *out = a * b;
    return 0;
}

static int distance(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    return a >= b ? difference(a, b, out) : difference(b, a, out);
}

static int midpoint(struct r1_battery_interval interval, r1_battery_int *out)
{
    r1_battery_int width;
    int ret = difference(interval.high, interval.low, &width);
    return ret ? ret : sum(interval.low, width / 2, out);
}

static int valid_policy(const struct r1_battery_policy *p)
{
    return p && p->minimum_01mv > 0 && p->discharge_01ma > 0 &&
        p->shunt_01mohm >= 0 && p->meter_01mohm >= 0 && p->dc_ratio_percent > 0 &&
        p->max_capture_ns > 0 && p->max_gap_ns > 0 && p->max_epoch_ns > 0 &&
        p->valid_for_ns > 0 && p->valid_for_ns <= p->max_gap_ns &&
        p->observed_current_limit_ua >= 0 && p->interval_current_bound_ua > 0 &&
        p->interval_current_bound_ua >= p->observed_current_limit_ua &&
        p->voltage_motion_uv >= 0 && p->current_motion_ua >= 0 &&
        p->temperature_motion_decic >= 0 && p->model_error_uah >= 0 &&
        p->cutoff_error_uah >= 0 && p->car_delta_error_uah >= 0 &&
        p->max_charge_width_uah > 0 && p->max_soc_width_bp > 0;
}

static int same_policy(const struct r1_battery_policy *a, const struct r1_battery_policy *b)
{
#define SAME(field) (a->field == b->field)
    return SAME(minimum_01mv) && SAME(discharge_01ma) && SAME(shunt_01mohm) &&
        SAME(meter_01mohm) && SAME(dc_ratio_percent) && SAME(max_capture_ns) &&
        SAME(max_gap_ns) && SAME(max_epoch_ns) && SAME(valid_for_ns) &&
        SAME(observed_current_limit_ua) && SAME(interval_current_bound_ua) &&
        SAME(voltage_motion_uv) && SAME(current_motion_ua) &&
        SAME(temperature_motion_decic) && SAME(model_error_uah) &&
        SAME(cutoff_error_uah) && SAME(car_delta_error_uah) &&
        SAME(max_charge_width_uah) && SAME(max_soc_width_bp);
#undef SAME
}

/* Positive operands only. Splitting time first permits ordinary hour/day
 * intervals without multiplying current by the entire nanosecond count. */
static int charge_bound(r1_battery_int current, r1_battery_int ns, r1_battery_int *out)
{
    r1_battery_int whole, fraction, rounded;
    int ret;
    if ((ret = product(current, ns / NS_UA_PER_UAH, &whole)) ||
        (ret = product(current, ns % NS_UA_PER_UAH, &fraction))) return ret;
    rounded = fraction / NS_UA_PER_UAH + !!(fraction % NS_UA_PER_UAH);
    return sum(whole, rounded, out);
}

static int intersect(struct r1_battery_interval a, struct r1_battery_interval b,
                      struct r1_battery_interval *out)
{
    out->low = a.low > b.low ? a.low : b.low;
    out->high = a.high < b.high ? a.high : b.high;
    return out->low <= out->high;
}

static int car_class(r1_battery_int raw)
{
    r1_battery_int magnitude = (raw >> 11) & 0xfffff;
    return (int)((raw >> 31) * 4) + (magnitude == 0 ? 1 : magnitude == 0xfffff ? 2 : 0);
}

static int valid_endpoint(const struct r1_battery_endpoint *e)
{
    int code;
    if (e->raw_car < 0 || e->raw_car > 0xffffffffLL || e->voltage_uv <= 0 ||
        e->voltage_uv % 1000 || e->current_ua % 100 || e->car_uah % 100)
        return 0;
    code = car_class(e->raw_car);
    if ((code % 4 && e->car_uah) || (code >= 4 && e->car_uah > 0) ||
        (code < 4 && e->car_uah < 0)) return 0;
    return 1;
}

static int expand_root(r1_battery_int q, r1_battery_int error,
                        struct r1_battery_interval *out)
{
    r1_battery_int base, high;
    int ret;
    if ((ret = product(q, 100, &base)) || (ret = sum(base, 100, &high)) ||
        (ret = difference(base, error, &out->low))) return ret;
    return sum(high, error, &out->high);
}

static int possible_delta(r1_battery_int a, r1_battery_int b, r1_battery_int elapsed,
                           const struct r1_battery_policy *p, int *possible)
{
    r1_battery_int movement, allowance;
    int ret;
    if ((ret = distance(a, b, &movement)) ||
        (ret = charge_bound(p->interval_current_bound_ua, elapsed, &allowance)) ||
        (ret = sum(allowance, p->car_delta_error_uah, &allowance))) return ret;
    *possible = movement <= allowance;
    return 0;
}

/* This frame owns the only temporary curve. Keep it out of the reducer's
 * state-copy frame; no retained profile/devres pointer or dynamic allocation. */
static __attribute__((__noinline__)) int candidate(const struct r1_battery_policy *p,
                      const struct r1_battery_observation *o,
                      struct r1_battery_estimate *e, enum r1_battery_reason *why)
{
    struct r1_battery_profile curve;
    struct r1_battery_model_parameters parameters = {
        .shunt_01mohm = p->shunt_01mohm, .meter_01mohm = p->meter_01mohm,
        .dc_ratio_percent = p->dc_ratio_percent,
    };
    struct r1_battery_model_point root;
    struct r1_battery_model_capacity cutoff;
    struct r1_battery_interval intervals[2];
    r1_battery_int padding;
    int ret, i;

    *why = R1_BATTERY_REASON_ARITHMETIC;
    if ((ret = charge_bound(p->interval_current_bound_ua,
                           o->finished_ns - o->started_ns, &padding)) ||
        (ret = sum(padding, p->model_error_uah, &padding))) return ret;
    e->temperature_c = r1_battery_whole_celsius(o->endpoint[0].temperature_decic);
    *why = R1_BATTERY_REASON_MODEL;
    ret = r1_battery_profile_at(r1_battery_stock_tables, R1_BATTERY_TABLES,
                                e->temperature_c, &curve);
    if (ret) return ret;
    for (i = 0; i < 2; i++) {
        parameters.current_01ma = o->endpoint[i].current_ua / 100;
        ret = r1_battery_model_seed(&curve, o->endpoint[i].voltage_uv / 100,
                                    &parameters, &root);
        if (ret) return ret;
        ret = expand_root(root.charge_01mah, padding, &intervals[i]);
        if (ret) { *why = R1_BATTERY_REASON_ARITHMETIC; return ret; }
    }
    if (!intersect(intervals[0], intervals[1], &e->discharged_uah)) {
        *why = R1_BATTERY_REASON_DISAGREEMENT;
        return R1_BATTERY_NO_ROOT;
    }
    *why = R1_BATTERY_REASON_CUTOFF;
    parameters.current_01ma = -p->discharge_01ma;
    ret = r1_battery_model_cutoff(&curve, p->minimum_01mv, &parameters, &cutoff);
    if (ret) return ret;
    if (cutoff.boundary == R1_BATTERY_MODEL_PROFILE_END) return R1_BATTERY_NO_CUTOFF;
    e->boundary = cutoff.boundary;
    ret = expand_root(cutoff.point.charge_01mah, p->cutoff_error_uah, &e->usable_uah);
    if (ret) { *why = R1_BATTERY_REASON_ARITHMETIC; return ret; }
    return e->usable_uah.low > 0 ? 0 : R1_BATTERY_NO_CUTOFF;
}

/* Exact rational SOC rounded outwards. Positive Q is guaranteed by candidate.
 * Keep negative/out-of-range coordinates: presentation clamping is separate. */
static int soc(r1_battery_int q, r1_battery_int usable, int round_up,
                r1_battery_int *out)
{
    r1_battery_int numerator, quotient;
    int ret;
    if ((ret = difference(usable, q, &numerator)) ||
        (ret = product(numerator, 10000, &numerator))) return ret;
    quotient = numerator / usable;
    if (numerator % usable) {
        if (round_up && numerator > 0) quotient++;
        if (!round_up && numerator < 0) quotient--;
    }
    *out = quotient;
    return 0;
}

static int normalize(struct r1_battery_estimate *e, const struct r1_battery_policy *p,
                      enum r1_battery_reason *why)
{
    r1_battery_int q[2] = {e->discharged_uah.low, e->discharged_uah.high};
    r1_battery_int usable[2] = {e->usable_uah.low, e->usable_uah.high};
    r1_battery_int width, lo, hi;
    int i, j, ret;
    *why = R1_BATTERY_REASON_ARITHMETIC;
    if ((ret = difference(q[1], q[0], &width))) return ret;
    if (width > p->max_charge_width_uah) {
        *why = R1_BATTERY_REASON_WIDTH; return R1_BATTERY_INVALID;
    }
    e->soc_bp.low = LIMIT64;
    e->soc_bp.high = MIN64;
    for (i = 0; i < 2; i++) for (j = 0; j < 2; j++) {
        if ((ret = soc(q[i], usable[j], 0, &lo)) ||
            (ret = soc(q[i], usable[j], 1, &hi))) return ret;
        if (lo < e->soc_bp.low) e->soc_bp.low = lo;
        if (hi > e->soc_bp.high) e->soc_bp.high = hi;
    }
    if ((ret = difference(e->soc_bp.high, e->soc_bp.low, &width))) return ret;
    if (width > p->max_soc_width_bp) {
        *why = R1_BATTERY_REASON_WIDTH; return R1_BATTERY_INVALID;
    }
    if ((ret = midpoint(e->discharged_uah, &e->discharged_point_uah)) ||
        (ret = midpoint(e->usable_uah, &e->usable_point_uah)) ||
        (ret = soc(e->discharged_point_uah, e->usable_point_uah, 0,
                   &e->soc_point_bp))) return ret;
    if (e->soc_point_bp < 0) e->soc_point_bp = 0;
    if (e->soc_point_bp > 10000) e->soc_point_bp = 10000;
    return 0;
}

static void invalidate(struct r1_battery_session *s, enum r1_battery_reason why,
                        int error, enum r1_battery_event event)
{
    s->active = 0;
    s->estimate.validity = s->has_history ? R1_BATTERY_HISTORICAL : R1_BATTERY_UNAVAILABLE;
    s->estimate.reason = why;
    s->estimate.detail_error = error;
    s->estimate.event = event;
}

int r1_battery_session_step(struct r1_battery_session *state,
                            const struct r1_battery_policy *p,
                            const struct r1_battery_observation *o,
                            struct r1_battery_estimate *out)
{
    struct r1_battery_session next;
    struct r1_battery_estimate e = {0};
    struct r1_battery_interval predicted;
    enum r1_battery_reason why = R1_BATTERY_REASON_INITIAL, cause;
    r1_battery_int value, duration, delta, epoch_age;
    int i, ret = 0, possible;

    if (!state || !out || !o || !valid_policy(p) ||
        o->event < R1_BATTERY_EVENT_NONE || o->event > R1_BATTERY_EVENT_ENGINE_OFF)
        return R1_BATTERY_INVALID;
    next = *state;
    why = R1_BATTERY_REASON_ORDER;
    if (o->generation <= 0 || o->started_ns <= 0 ||
        o->finished_ns < o->started_ns || o->generation < next.seen_generation ||
        o->started_ns < next.seen_finished_ns) {
        if (o->event != R1_BATTERY_EVENT_NONE) next.barrier_missing = 1;
        goto failed;
    }
    if (o->event != R1_BATTERY_EVENT_NONE) {
        /* Reject a previously queued capture even if it was not processed
         * before this invalidation. Events do not fabricate FG sequences. */
        if (o->generation != next.seen_generation) next.seen_sequence = 0;
        next.seen_generation = o->generation;
        next.seen_finished_ns = o->finished_ns;
        next.event_barrier_ns = o->finished_ns;
        next.barrier_missing = 0;
        why = R1_BATTERY_REASON_EVENT;
        goto failed;
    }
    /* Equal clock values cannot prove that acquisition followed an event.
     * Ordinary adjacent captures may still share their boundary timestamp. */
    if (next.barrier_missing || o->started_ns <= next.event_barrier_ns ||
        o->sequence <= 0 || (o->generation == next.seen_generation &&
                            o->sequence <= next.seen_sequence)) goto failed;
    next.seen_generation = o->generation;
    next.seen_sequence = o->sequence;
    next.seen_finished_ns = o->finished_ns;
    why = R1_BATTERY_REASON_ACQUISITION;
    ret = o->error;
    if (ret) goto failed;
    for (i = 0; i < 2; i++) {
        ret = o->endpoint[i].error;
        if (ret) goto failed;
    }
    for (i = 0; i < 2; i++) {
        const struct r1_battery_endpoint *v = &o->endpoint[i];
        why = R1_BATTERY_REASON_ABSENT;
        if (!v->present) goto failed;
        why = R1_BATTERY_REASON_INPUT;
        if (v->present != 1 || !valid_endpoint(v)) goto failed;
        why = R1_BATTERY_REASON_TEMPERATURE;
        if (v->temperature_decic < -100 || v->temperature_decic > 500) goto failed;
        why = R1_BATTERY_REASON_ARITHMETIC;
        if ((ret = distance(v->current_ua, 0, &value))) goto failed;
        why = R1_BATTERY_REASON_LIMIT;
        if (value > p->observed_current_limit_ua) goto failed;
    }
    why = R1_BATTERY_REASON_TEMPERATURE;
    if (r1_battery_whole_celsius(o->endpoint[0].temperature_decic) !=
        r1_battery_whole_celsius(o->endpoint[1].temperature_decic)) goto failed;
    duration = o->finished_ns - o->started_ns;
    why = R1_BATTERY_REASON_LIMIT;
    if (duration > p->max_capture_ns) goto failed;
#define MOTION(field, limit) do { \
    why = R1_BATTERY_REASON_ARITHMETIC; \
    if ((ret = distance(o->endpoint[0].field, o->endpoint[1].field, &value))) goto failed; \
    why = R1_BATTERY_REASON_LIMIT; \
    if (value > p->limit) goto failed; \
} while (0)
    MOTION(voltage_uv, voltage_motion_uv);
    MOTION(current_ua, current_motion_ua);
    MOTION(temperature_decic, temperature_motion_decic);
#undef MOTION
    why = R1_BATTERY_REASON_COUNTER_AMBIGUOUS;
    if (car_class(o->endpoint[0].raw_car) != car_class(o->endpoint[1].raw_car)) goto failed;
    why = R1_BATTERY_REASON_ARITHMETIC;
    if ((ret = possible_delta(o->endpoint[0].car_uah, o->endpoint[1].car_uah,
                              duration, p, &possible))) goto failed;
    why = R1_BATTERY_REASON_COUNTER_RATE;
    if (!possible) goto failed;
    ret = candidate(p, o, &e, &why);
    if (ret) goto failed;

    cause = R1_BATTERY_REASON_TRACKED;
    if (!next.active) cause = R1_BATTERY_REASON_SEEDED;
    else if (o->generation != next.estimate.generation) cause = R1_BATTERY_REASON_GENERATION;
    else if (!same_policy(p, &next.policy)) cause = R1_BATTERY_REASON_POLICY;
    else if (e.temperature_c != next.estimate.temperature_c) cause = R1_BATTERY_REASON_TEMPERATURE;
    else if (o->finished_ns - next.estimate.accepted_ns > p->max_gap_ns)
        cause = R1_BATTERY_REASON_GAP;
    else if (o->finished_ns - next.seed_ns > p->max_epoch_ns)
        cause = R1_BATTERY_REASON_EPOCH_AGE;
    else if (car_class(o->endpoint[0].raw_car) != car_class(next.last_raw_car))
        cause = R1_BATTERY_REASON_COUNTER_AMBIGUOUS;

    if (cause == R1_BATTERY_REASON_TRACKED) {
        why = R1_BATTERY_REASON_ARITHMETIC;
        epoch_age = o->finished_ns - next.seed_ns;
        if ((ret = possible_delta(next.last_car_uah, o->endpoint[1].car_uah,
                                  o->finished_ns - next.estimate.accepted_ns, p, &possible)))
            goto failed;
        why = R1_BATTERY_REASON_COUNTER_RATE;
        if (!possible) goto failed;
        why = R1_BATTERY_REASON_ARITHMETIC;
        if ((ret = possible_delta(next.seed_car_uah, o->endpoint[1].car_uah,
                                  epoch_age, p, &possible))) goto failed;
        why = R1_BATTERY_REASON_COUNTER_RATE;
        if (!possible) goto failed;
        why = R1_BATTERY_REASON_ARITHMETIC;
        if ((ret = difference(o->endpoint[1].car_uah, next.seed_car_uah, &delta)) ||
            (ret = difference(next.seed_discharged_uah.low, delta, &predicted.low)) ||
            (ret = difference(predicted.low, p->car_delta_error_uah, &predicted.low)) ||
            (ret = difference(next.seed_discharged_uah.high, delta, &predicted.high)) ||
            (ret = sum(predicted.high, p->car_delta_error_uah, &predicted.high))) goto failed;
        why = R1_BATTERY_REASON_DISAGREEMENT;
        if (!intersect(e.discharged_uah, predicted, &e.discharged_uah)) goto failed;
        /* Recomputed from identical frozen inputs; retain epoch capacity. */
        e.usable_uah = next.estimate.usable_uah;
        e.epoch = next.estimate.epoch;
    } else {
        why = R1_BATTERY_REASON_ARITHMETIC;
        if ((ret = sum(next.estimate.epoch, 1, &e.epoch))) goto failed;
        next.seed_discharged_uah = e.discharged_uah;
        next.seed_car_uah = o->endpoint[1].car_uah;
        next.seed_ns = o->finished_ns;
    }
    ret = normalize(&e, p, &why);
    if (ret) goto failed;
    why = R1_BATTERY_REASON_ARITHMETIC;
    if ((ret = sum(o->finished_ns, p->valid_for_ns, &e.expires_ns)) ||
        (ret = sum(next.seed_ns, p->max_epoch_ns, &value))) goto failed;
    if (value < e.expires_ns) e.expires_ns = value;
    e.validity = R1_BATTERY_FRESH;
    e.reason = cause;
    e.generation = o->generation;
    e.sequence = o->sequence;
    e.accepted_ns = o->finished_ns;
    next.estimate = e;
    next.active = next.has_history = 1;
    next.last_car_uah = o->endpoint[1].car_uah;
    next.last_raw_car = o->endpoint[1].raw_car;
    next.policy = *p;
    goto publish;
failed:
    invalidate(&next, why, ret, o->event);
publish:
    *state = next;
    *out = next.estimate;
    return 0;
}

int r1_battery_session_read(const struct r1_battery_session *state,
                            r1_battery_int now_ns, struct r1_battery_estimate *out)
{
    struct r1_battery_estimate result;
    enum r1_battery_reason why = R1_BATTERY_REASON_ARITHMETIC;
    r1_battery_int movement;
    int ret = 0;
    if (!state || !out || now_ns <= 0) return R1_BATTERY_INVALID;
    result = state->estimate;
    if (result.validity == R1_BATTERY_FRESH) {
        if (now_ns < result.accepted_ns || now_ns > result.expires_ns) {
            why = now_ns < result.accepted_ns ? R1_BATTERY_REASON_ORDER :
                                               R1_BATTERY_REASON_EXPIRED;
            goto historical;
        }
        /* No new electrical observation occurred. Widen both charge bounds
         * using the externally justified unsampled-current envelope, without
         * renewing accepted_ns, expiry or the reducer's CAR baseline. */
        if ((ret = charge_bound(state->policy.interval_current_bound_ua,
                                now_ns - result.accepted_ns, &movement)) ||
            (ret = difference(result.discharged_uah.low, movement,
                               &result.discharged_uah.low)) ||
            (ret = sum(result.discharged_uah.high, movement,
                        &result.discharged_uah.high)) ||
            (ret = normalize(&result, &state->policy, &why))) goto historical;
    }
    *out = result;
    return 0;
historical:
    result = state->estimate;
    result.validity = R1_BATTERY_HISTORICAL;
    result.reason = why;
    result.detail_error = ret;
    *out = result;
    return 0;
}
