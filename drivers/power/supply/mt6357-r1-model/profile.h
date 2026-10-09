/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef R1_BATTERY_PROFILE_H
#define R1_BATTERY_PROFILE_H

#ifdef __KERNEL__
#include <linux/types.h>
typedef s64 r1_battery_int;
#else
#include <stddef.h>
#include <stdint.h>
typedef int64_t r1_battery_int;
#endif

#define R1_BATTERY_ROWS 53
#define R1_BATTERY_TABLES 4
#define R1_BATTERY_PERCENT_SCALE 10000

enum r1_battery_error {
    R1_BATTERY_OK = 0,
    R1_BATTERY_INVALID = -1,
    R1_BATTERY_OVERFLOW = -2,
    R1_BATTERY_UNNORMALIZED = -3,
    R1_BATTERY_NO_CUTOFF = -4,
    R1_BATTERY_SEARCH_LIMIT = -5,
    R1_BATTERY_NO_ROOT = -6,
    R1_BATTERY_AMBIGUOUS = -7,
};

struct r1_battery_point {
    r1_battery_int charge_01mah;
    r1_battery_int voltage_01mv;
    r1_battery_int resistance_01mohm;
};

struct r1_battery_table {
    r1_battery_int temperature_c;
    r1_battery_int qmax_01mah;
    r1_battery_int qmax_high_01mah;
    size_t count;
    struct r1_battery_point points[R1_BATTERY_ROWS];
};

struct r1_battery_profile {
    struct r1_battery_table curve;
    /* Zero until the caller explicitly supplies a usable capacity. */
    r1_battery_int usable_01mah;
    r1_battery_int dod[R1_BATTERY_ROWS];
};

/* All inputs are explicit; none is obtained from Qmax metadata or a default.
 * RAC uses the same 0.1 mOhm unit as the profile, NOT raw stock PTIM mOhm. */
struct r1_battery_load {
    r1_battery_int minimum_01mv;
    r1_battery_int discharge_01ma;
    r1_battery_int rac_01mohm;
    r1_battery_int shunt_01mohm;
    r1_battery_int meter_01mohm;
    r1_battery_int dc_ratio_percent;
};

struct r1_battery_capacity {
    struct r1_battery_profile profile;
    r1_battery_int initial_cutoff_01mv;
    r1_battery_int initial_usable_01mah;
    r1_battery_int cutoff_01mv;
};

/* Rational loaded-profile model, independent of the stock RAC search.
 * Positive current means charging. No parameter has an implicit default. */
struct r1_battery_model_parameters {
    r1_battery_int current_01ma;
    r1_battery_int shunt_01mohm;
    r1_battery_int meter_01mohm;
    r1_battery_int dc_ratio_percent;
};

struct r1_battery_model_point {
    r1_battery_int charge_01mah;
    r1_battery_int ocv_01mv;
    r1_battery_int resistance_01mohm;
};

enum r1_battery_model_boundary {
    R1_BATTERY_MODEL_CROSSING,
    R1_BATTERY_MODEL_DISCONTINUITY,
    R1_BATTERY_MODEL_PROFILE_END,
};

struct r1_battery_model_capacity {
    struct r1_battery_model_point point;
    enum r1_battery_model_boundary boundary;
};

struct r1_battery_state {
    /* Internal model coordinates, deliberately not restricted to 0..10000. */
    r1_battery_int dod;
    r1_battery_int soc;
};

extern const struct r1_battery_table r1_battery_stock_tables[R1_BATTERY_TABLES];

/* Explicit adapter for mainline TEMP: signed truncation toward zero. */
r1_battery_int r1_battery_whole_celsius(r1_battery_int temperature_decic);

/* Validate all supplied rows, counts, metadata and strictly ordered temperatures.
 * Both ascending and descending temperature order are accepted. Profiles clamp
 * to the outer temperatures; resistance need not be monotonic. */
int r1_battery_validate_tables(const struct r1_battery_table *tables, size_t count);
int r1_battery_profile_at(const struct r1_battery_table *tables, size_t count,
                          r1_battery_int temperature_c, struct r1_battery_profile *out);

/* Qmax metadata is never silently used as usable capacity. In-place operation is
 * supported. Failed operations leave every caller-owned output unchanged. */
int r1_battery_assign_dod(const struct r1_battery_profile *profile,
                          r1_battery_int usable_01mah, struct r1_battery_profile *out);
int r1_battery_validate_profile(const struct r1_battery_profile *profile);

/* Calculate cutoff and normalize only the explicit valid curve rows. Existing
 * DOD/usable fields are ignored. Positive minimum voltage/DC ratio and
 * nonnegative load/resistances are required. The stock 0.1%-DOD search is
 * bounded to a bracket spanning at most 10000 units (1001 samples). Missing
 * crossings, zero capacity and selected repeated DOD coordinates fail closed.
 * out may contain the input profile; failed operations publish nothing. */
int r1_battery_calculate_usable(const struct r1_battery_profile *profile,
                                const struct r1_battery_load *load,
                                struct r1_battery_capacity *out);

/* Solve terminal = OCV + current * (Rcell * DC / 100 + shunt + meter)
 * / 10000 as an exact piecewise-linear rational model on explicit curve rows.
 * Usable/DOD fields are ignored. Voltage and DC ratio must be positive; extra
 * resistances must be nonnegative. Return a MODEL ESTIMATE, not measured OCV.
 * Missing/multiple roots fail. Uniqueness is decided before flooring outputs;
 * identical repeated points/shared vertices count once, but a root on a flat
 * interval or a nonidentical zero-charge-width segment is ambiguous.
 * Failed operations leave the whole output unchanged. */
int r1_battery_model_seed(const struct r1_battery_profile *profile,
                          r1_battery_int terminal_01mv,
                          const struct r1_battery_model_parameters *parameters,
                          struct r1_battery_model_point *out);

/* Same model under current <= 0. Require first charge zero. The earliest
 * at/below-minimum boundary ends discharge; later recovery cannot extend it.
 * A zero-width drop returns DISCONTINUITY, not a uniquely usable seed point.
 * No crossing returns PROFILE_END: only a truncated-domain bound, not evidence
 * that the minimum was reached. Starting at/below minimum or a capacity that
 * floors to zero fails NO_CUTOFF. No implicit normalization or Qmax fallback.
 * All returned positive physical quantities are floored in their named units. */
int r1_battery_model_cutoff(const struct r1_battery_profile *profile,
                            r1_battery_int minimum_01mv,
                            const struct r1_battery_model_parameters *parameters,
                            struct r1_battery_model_capacity *out);

/* Positive OCV input is required. These are curve conversions, not evidence
 * that a loaded terminal-voltage measurement is a valid OCV reference. */
int r1_battery_ocv_to_dod(const struct r1_battery_profile *profile,
                          r1_battery_int ocv_01mv, r1_battery_int *dod);
int r1_battery_dod_to_ocv(const struct r1_battery_profile *profile,
                          r1_battery_int dod, r1_battery_int *ocv_01mv);

/* Positive relative charge means charging. The caller must establish a valid
 * reference OCV and counter continuity before calling this pure function.
 * Delta is in 0.1 mAh (100 microamp-hours), not raw CHARGE_COUNTER units. */
int r1_battery_update(const struct r1_battery_profile *profile,
                      r1_battery_int reference_ocv_01mv, r1_battery_int delta_01mah,
                      struct r1_battery_state *out);
#endif
