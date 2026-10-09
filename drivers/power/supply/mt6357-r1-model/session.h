/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef R1_BATTERY_SESSION_H
#define R1_BATTERY_SESSION_H
#include "profile.h"

/* Explicit experimental policy. No field has a board default. Error budgets
 * are assumptions supplied by the caller, not inferred from endpoint motion.
 * An epoch freezes this policy and one whole-C stock battery0 profile. */
struct r1_battery_policy {
    /* discharge_01ma is a positive reference-load magnitude, not signed I. */
    r1_battery_int minimum_01mv, discharge_01ma;
    r1_battery_int shunt_01mohm, meter_01mohm, dc_ratio_percent;
    r1_battery_int max_capture_ns, max_gap_ns, max_epoch_ns, valid_for_ns;
    r1_battery_int observed_current_limit_ua, interval_current_bound_ua;
    r1_battery_int voltage_motion_uv, current_motion_ua, temperature_motion_decic;
    r1_battery_int model_error_uah, cutoff_error_uah, car_delta_error_uah;
    r1_battery_int max_charge_width_uah, max_soc_width_bp;
};

struct r1_battery_endpoint {
    /* The adapter must fold ALL per-field, conversion and release errors into
     * error; zero means complete, independently valid PRESENT and V/I/T/CAR.
     * raw_car is the unsigned packed pair, represented in a signed64 carrier. */
    int error, present;
    r1_battery_int raw_car, car_uah, current_ua, voltage_uv, temperature_decic;
};

enum r1_battery_event {
    R1_BATTERY_EVENT_NONE,
    R1_BATTERY_EVENT_SUSPEND,
    R1_BATTERY_EVENT_RESET,
    R1_BATTERY_EVENT_REMOVAL,
    R1_BATTERY_EVENT_ENGINE_OFF,
};

struct r1_battery_observation {
    /* Positive, process-lifetime monotonic identities and boottime ns. The
     * adapter rejects u64 values above INT64_MAX instead of narrowing them. */
    r1_battery_int generation, sequence, started_ns, finished_ns;
    int error;
    /* Events require a real monotonic generation/time barrier; sequence and
     * endpoint fields are ignored. New captures must start strictly after it. */
    enum r1_battery_event event;
    struct r1_battery_endpoint endpoint[2];
};

struct r1_battery_interval { r1_battery_int low, high; };

enum r1_battery_validity {
    R1_BATTERY_UNAVAILABLE, /* No accepted estimate exists. Numeric fields unusable. */
    R1_BATTERY_FRESH,      /* Accepted observation or bounded, widened cache read. */
    R1_BATTERY_HISTORICAL,  /* Numeric fields are history, NOT publishable capacity. */
};

enum r1_battery_reason {
    R1_BATTERY_REASON_INITIAL,
    R1_BATTERY_REASON_SEEDED,
    R1_BATTERY_REASON_TRACKED,
    R1_BATTERY_REASON_EVENT,
    R1_BATTERY_REASON_ORDER,
    R1_BATTERY_REASON_ACQUISITION,
    R1_BATTERY_REASON_ABSENT,
    R1_BATTERY_REASON_INPUT,
    R1_BATTERY_REASON_LIMIT,
    R1_BATTERY_REASON_TEMPERATURE,
    R1_BATTERY_REASON_COUNTER_AMBIGUOUS,
    R1_BATTERY_REASON_COUNTER_RATE,
    R1_BATTERY_REASON_MODEL,
    R1_BATTERY_REASON_DISAGREEMENT,
    R1_BATTERY_REASON_CUTOFF,
    R1_BATTERY_REASON_WIDTH,
    R1_BATTERY_REASON_ARITHMETIC,
    R1_BATTERY_REASON_GENERATION,
    R1_BATTERY_REASON_POLICY,
    R1_BATTERY_REASON_GAP,
    R1_BATTERY_REASON_EPOCH_AGE,
    R1_BATTERY_REASON_EXPIRED,
};

struct r1_battery_estimate {
    enum r1_battery_validity validity;
    enum r1_battery_reason reason;
    int detail_error;
    enum r1_battery_event event;
    r1_battery_int epoch, generation, sequence, accepted_ns, expires_ns;
    r1_battery_int temperature_c;
    struct r1_battery_interval discharged_uah, usable_uah, soc_bp;
    /* Interval midpoints; SOC representative is floored then clamped to
     * 0..10000 basis points. Bounds remain unclamped for uncertainty review.
     * No integer Android percentage rounding policy is imposed here. */
    r1_battery_int discharged_point_uah, usable_point_uah, soc_point_bp;
    enum r1_battery_model_boundary boundary;
};

/* Caller-owned, zero-initialize once; thereafter only these functions mutate
 * it. No pointers or borrowed lifetime. Binding generation is not a PMIC reset
 * epoch. Serialize calls; use explicit suspend/reset/removal/off events. */
struct r1_battery_session {
    int active, has_history, barrier_missing;
    r1_battery_int seen_generation, seen_sequence, seen_finished_ns;
    r1_battery_int event_barrier_ns;
    r1_battery_int seed_ns, seed_car_uah, last_car_uah, last_raw_car;
    struct r1_battery_interval seed_discharged_uah;
    struct r1_battery_policy policy;
    struct r1_battery_estimate estimate;
};

/* Returns INVALID for malformed API/policy arguments, leaving state/out
 * unchanged. Observation failures are successful state transitions to
 * unavailable/history with a reason, never a manufactured fresh zero.
 * FRESH is as of accepted_ns: publish only through read(actual_now_ns).
 * State and output must not overlap other arguments. No dynamic allocation. */
int r1_battery_session_step(struct r1_battery_session *state,
                            const struct r1_battery_policy *policy,
                            const struct r1_battery_observation *observation,
                            struct r1_battery_estimate *out);

/* Pure publication check. Widen charge uncertainty by the explicit current
 * envelope over cache age, recheck width, never renew accepted time/expiry or
 * mutate state. Positive now_ns required. Failures produce labeled history. */
int r1_battery_session_read(const struct r1_battery_session *state,
                            r1_battery_int now_ns, struct r1_battery_estimate *out);
#endif
