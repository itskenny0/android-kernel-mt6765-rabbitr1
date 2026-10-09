/* SPDX-License-Identifier: GPL-2.0-only */
#include "profile.h"
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

static struct r1_battery_profile at(r1_battery_int temperature)
{
    struct r1_battery_profile profile;
    assert(!r1_battery_profile_at(r1_battery_stock_tables, R1_BATTERY_TABLES, temperature, &profile));
    return profile;
}

static unsigned oracles(const char *path)
{
    FILE *file = fopen(path, "r");
    unsigned cases = 0;
    r1_battery_int temperature;
    assert(file);
    while (fscanf(file, " %" SCNd64, &temperature) == 1) {
        struct r1_battery_profile profile = at(temperature);
        struct r1_battery_load load;
        struct r1_battery_capacity output, saved;
        int expected_error, error;
        size_t i;
        assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %d",
                      &load.minimum_01mv, &load.discharge_01ma, &load.rac_01mohm,
                      &load.shunt_01mohm, &load.meter_01mohm, &load.dc_ratio_percent,
                      &expected_error) == 7);
        memset(&output, 0x5a, sizeof(output)); saved = output;
        error = r1_battery_calculate_usable(&profile, &load, &output);
        if (error != expected_error) {
            fprintf(stderr, "case %u T=%" PRId64 " min=%" PRId64 " I=%" PRId64
                    " rac=%" PRId64 " dc=%" PRId64 ": error %d != %d\n",
                    cases, temperature, load.minimum_01mv, load.discharge_01ma,
                    load.rac_01mohm, load.dc_ratio_percent, error, expected_error);
        }
        assert(error == expected_error);
        if (error) {
            assert(!memcmp(&output, &saved, sizeof(output)));
        } else {
            r1_battery_int initial, initial_capacity, cutoff, capacity, expected;
            assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64,
                          &initial, &initial_capacity, &cutoff, &capacity) == 4);
            if (output.initial_cutoff_01mv != initial || output.initial_usable_01mah != initial_capacity ||
                output.cutoff_01mv != cutoff || output.profile.usable_01mah != capacity) {
                fprintf(stderr, "case %u: got %" PRId64 ",%" PRId64 ",%" PRId64 ",%" PRId64
                        " expected %" PRId64 ",%" PRId64 ",%" PRId64 ",%" PRId64 "\n", cases,
                        output.initial_cutoff_01mv, output.initial_usable_01mah,
                        output.cutoff_01mv, output.profile.usable_01mah,
                        initial, initial_capacity, cutoff, capacity);
            }
            assert(output.initial_cutoff_01mv == initial);
            assert(output.initial_usable_01mah == initial_capacity);
            assert(output.cutoff_01mv == cutoff && output.profile.usable_01mah == capacity);
            assert(!r1_battery_validate_profile(&output.profile));
            assert(!memcmp(&profile.curve, &output.profile.curve, sizeof(profile.curve)));
            for (i = 0; i < profile.curve.count; ++i) {
                assert(fscanf(file, " %" SCNd64, &expected) == 1);
                if (output.profile.dod[i] != expected) fprintf(stderr, "case %u row %zu: DOD %" PRId64 " != %" PRId64 " capacity=%" PRId64 "\n", cases, i, output.profile.dod[i], expected, output.profile.usable_01mah);
                assert(output.profile.dod[i] == expected);
            }
            /* The result may contain the input profile; stale normalization
             * never substitutes metadata or becomes an implicit input. */
            saved = output;
            output.profile.usable_01mah = -1;
            output.profile.dod[0] = INT64_MIN;
            assert(!r1_battery_calculate_usable(&output.profile, &load, &output));
            assert(!memcmp(&output, &saved, sizeof(output)));
        }
        ++cases;
    }
    assert(feof(file) && !ferror(file));
    fclose(file);
    return cases;
}

static unsigned boundaries(void)
{
    struct r1_battery_profile profile, original = at(25);
    const struct r1_battery_load defaults = {33500, 5000, 1500, 100, 75, 100};
    struct r1_battery_load load;
    struct r1_battery_capacity output, saved;
    unsigned cases = 0;
    memset(&output, 0x5a, sizeof(output)); saved = output;
#define REJECT(change, expected) do { \
    profile = original; load = defaults; change; \
    assert(r1_battery_calculate_usable(&profile, &load, &output) == (expected)); \
    assert(!memcmp(&output, &saved, sizeof(output))); ++cases; \
} while (0)
    assert(r1_battery_calculate_usable(NULL, &defaults, &output) == R1_BATTERY_INVALID);
    assert(r1_battery_calculate_usable(&original, NULL, &output) == R1_BATTERY_INVALID);
    assert(r1_battery_calculate_usable(&original, &defaults, NULL) == R1_BATTERY_INVALID);
    assert(!memcmp(&output, &saved, sizeof(output))); ++cases;
    REJECT(load.minimum_01mv = 0, R1_BATTERY_INVALID);
    REJECT(load.minimum_01mv = -1, R1_BATTERY_INVALID);
    REJECT(load.discharge_01ma = -1, R1_BATTERY_INVALID);
    REJECT(load.discharge_01ma = INT64_MIN, R1_BATTERY_INVALID);
    REJECT(load.rac_01mohm = -1, R1_BATTERY_INVALID);
    REJECT(load.shunt_01mohm = -1, R1_BATTERY_INVALID);
    REJECT(load.meter_01mohm = -1, R1_BATTERY_INVALID);
    REJECT(load.dc_ratio_percent = 0, R1_BATTERY_INVALID);
    REJECT(load.dc_ratio_percent = -1, R1_BATTERY_INVALID);
    REJECT(profile.curve.count = 1, R1_BATTERY_INVALID);
    REJECT(profile.curve.count = 54, R1_BATTERY_INVALID);
    REJECT(profile.curve.points[2].charge_01mah = 0, R1_BATTERY_INVALID);
    REJECT(profile.curve.points[1].voltage_01mv = profile.curve.points[0].voltage_01mv + 1,
           R1_BATTERY_INVALID);
    REJECT(profile.curve.points[52].voltage_01mv = 0, R1_BATTERY_INVALID);
    REJECT(profile.curve.points[0].resistance_01mohm = 0, R1_BATTERY_INVALID);
    REJECT(load.minimum_01mv = 50000, R1_BATTERY_NO_CUTOFF);
    REJECT(load.rac_01mohm = 0; load.minimum_01mv = 40000; load.discharge_01ma = 20000,
           R1_BATTERY_NO_CUTOFF);
    REJECT(load.rac_01mohm = INT64_MAX, R1_BATTERY_OVERFLOW);
    REJECT(load.minimum_01mv = INT64_MAX, R1_BATTERY_OVERFLOW);
    REJECT(load.dc_ratio_percent = INT64_MAX, R1_BATTERY_OVERFLOW);
    REJECT(load.discharge_01ma = INT64_MAX; load.rac_01mohm = 0, R1_BATTERY_OVERFLOW);
    REJECT(load.shunt_01mohm = INT64_MAX, R1_BATTERY_OVERFLOW);
    REJECT(load.meter_01mohm = INT64_MAX, R1_BATTERY_OVERFLOW);
    REJECT(load.shunt_01mohm = INT64_MAX / 10, R1_BATTERY_OVERFLOW);
    REJECT(profile.curve.points[52].charge_01mah = INT64_MAX, R1_BATTERY_OVERFLOW);

    /* A two-point curve is allowed by the existing generic API; no hidden
     * entries after count may influence cutoff. 53 is the stock valid count. */
    original.curve.count = 2;
    original.curve.points[0] = ((struct r1_battery_point){0, 40000, 1000});
    original.curve.points[1] = ((struct r1_battery_point){10000, 30000, 1000});
    REJECT(load.minimum_01mv = 39999; load.discharge_01ma = 0; load.rac_01mohm = 0,
           R1_BATTERY_SEARCH_LIMIT);
    /* Exhaust the permitted 1001 samples, reach a strict crossing only at
     * the first row, then reject zero FINAL capacity without publication. */
    REJECT(profile.curve.points[0].resistance_01mohm = 20000;
           profile.curve.points[1].resistance_01mohm = 20000;
           load.minimum_01mv = 30000; load.discharge_01ma = 5000; load.rac_01mohm = 0;
           load.shunt_01mohm = 0; load.meter_01mohm = 0,
           R1_BATTERY_NO_CUTOFF);
    /* Positive initial capacity but selected DOD coordinates coincide. */
    REJECT(profile.curve.points[0] = ((struct r1_battery_point){0, 40000, 1000});
           profile.curve.points[1] = ((struct r1_battery_point){0, 30000, 30000});
           profile.curve.count = 3;
           profile.curve.points[2] = ((struct r1_battery_point){10000, 20000, 1000});
           load.minimum_01mv = 20000; load.discharge_01ma = 5000; load.rac_01mohm = 0,
           R1_BATTERY_NO_CUTOFF);
    /* The lower endpoint crosses, but the stock upper-anchored 0.1% grid
     * misses it: fail instead of returning the last non-crossing sample. */
    REJECT(profile.curve.points[0] = ((struct r1_battery_point){0, 40000, 10000});
           profile.curve.points[1] = ((struct r1_battery_point){9999, 30000, 1000});
           profile.curve.count = 3;
           profile.curve.points[2] = ((struct r1_battery_point){10000, 20000, 1000});
           load.minimum_01mv = 20000; load.discharge_01ma = 9999; load.rac_01mohm = 0;
           load.shunt_01mohm = 0; load.meter_01mohm = 0,
           R1_BATTERY_NO_CUTOFF);
#undef REJECT
    profile = at(25);
    load = (struct r1_battery_load){33500, 0, 0, 0, 0, 100};
    assert(!r1_battery_calculate_usable(&profile, &load, &output));
    assert(output.initial_cutoff_01mv == 33500);
    assert(output.initial_usable_01mah == profile.curve.points[52].charge_01mah);
    ++cases;
    /* Unit contract: 500mA * 150mOhm gives 75mV, not 7.5mV. */
    load = defaults;
    assert(!r1_battery_calculate_usable(&profile, &load, &output));
    assert(output.initial_cutoff_01mv == 34250); ++cases;
    /* Strict crossing keeps repeated voltage rows and the lower-voltage
     * interpolation anchor: reversing that anchor would yield 1000, not 1001. */
    profile.curve.count = 3;
    profile.curve.points[0] = ((struct r1_battery_point){0, 40000, 1000});
    profile.curve.points[1] = ((struct r1_battery_point){1000, 40000, 1000});
    profile.curve.points[2] = ((struct r1_battery_point){2000, 30000, 1000});
    load = (struct r1_battery_load){39999, 0, 0, 0, 0, 100};
    assert(!r1_battery_calculate_usable(&profile, &load, &output));
    assert(output.initial_usable_01mah == 1001 && output.cutoff_01mv == 40000);
    assert(output.profile.usable_01mah == 1000); ++cases;
    /* Repeated charge is retained; the first full-DOD point wins the search. */
    profile.curve.points[1].voltage_01mv = 35000;
    profile.curve.points[2].charge_01mah = 1000;
    load.minimum_01mv = 34000;
    assert(!r1_battery_calculate_usable(&profile, &load, &output));
    assert(output.cutoff_01mv == 35000 && output.profile.usable_01mah == 1000);
    assert(output.profile.dod[1] == 10000 && output.profile.dod[2] == 10000); ++cases;
    /* No caller input, including malformed old normalization, is mutated. */
    original = profile; saved = output;
    assert(!r1_battery_calculate_usable(&profile, &load, &output));
    assert(!memcmp(&profile, &original, sizeof(profile)));
    assert(!memcmp(&output, &saved, sizeof(output))); ++cases;
    return cases;
}

int main(int argc, char **argv)
{
    unsigned count, checks;
    assert(argc == 2);
    count = oracles(argv[1]);
    checks = boundaries();
    printf("PASS: %u stock cutoff instruction cases and %u validity/boundary scenarios\n", count, checks);
    return 0;
}
