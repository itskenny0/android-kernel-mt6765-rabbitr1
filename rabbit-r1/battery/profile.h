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
