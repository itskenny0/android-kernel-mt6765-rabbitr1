/* SPDX-License-Identifier: GPL-2.0-only */
#include "profile.h"
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct r1_battery_profile at(r1_battery_int temperature, r1_battery_int usable)
{
    struct r1_battery_profile profile;
    assert(!r1_battery_profile_at(r1_battery_stock_tables, R1_BATTERY_TABLES, temperature, &profile));
    if (usable) assert(!r1_battery_assign_dod(&profile, usable, &profile));
    return profile;
}

static unsigned oracle_tests(const char *path)
{
    FILE *file = fopen(path, "r");
    char type;
    unsigned cases = 0;
    assert(file);
    while (fscanf(file, " %c", &type) == 1) {
        r1_battery_int temperature, qmax, high, input, expected, delta, expected_soc;
        struct r1_battery_profile profile;
        struct r1_battery_state state;
        r1_battery_int actual;
        size_t i;
        if (type == 'T') {
            assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64, &temperature, &qmax, &high) == 3);
            profile = at(temperature, 0);
            assert(profile.curve.count == 53 && profile.curve.qmax_01mah == qmax);
            assert(profile.curve.qmax_high_01mah == high && profile.usable_01mah == 0);
            assert(r1_battery_validate_profile(&profile) == R1_BATTERY_UNNORMALIZED);
            for (i = 0; i < 53; ++i) {
                struct r1_battery_point point;
                assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64,
                              &point.charge_01mah, &point.voltage_01mv, &point.resistance_01mohm) == 3);
                assert(point.charge_01mah == profile.curve.points[i].charge_01mah);
                assert(point.voltage_01mv == profile.curve.points[i].voltage_01mv);
                assert(point.resistance_01mohm == profile.curve.points[i].resistance_01mohm);
            }
        } else if (type == 'O' || type == 'D') {
            assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64, &temperature, &input, &expected) == 3);
            profile = at(temperature, 0);
            assert(!r1_battery_assign_dod(&profile, profile.curve.qmax_01mah, &profile));
            if (type == 'O') {
                assert(!r1_battery_ocv_to_dod(&profile, input, &actual));
                assert(10000 - actual == expected);
            } else {
                assert(!r1_battery_dod_to_ocv(&profile, 10000 - input, &actual));
                assert(actual == expected);
            }
        } else if (type == 'N') {
            assert(fscanf(file, " %" SCNd64 " %" SCNd64, &temperature, &qmax) == 2);
            profile = at(temperature, qmax);
            for (i = 0; i < 53; ++i) {
                assert(fscanf(file, " %" SCNd64, &expected) == 1);
                assert(profile.dod[i] == expected);
            }
        } else if (type == 'B') {
            assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64,
                          &temperature, &qmax, &input, &expected) == 4);
            profile = at(temperature, qmax);
            assert(!r1_battery_dod_to_ocv(&profile, input, &actual) && actual == expected);
        } else if (type == 'L') {
            assert(fscanf(file, " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64,
                          &temperature, &input, &qmax, &delta, &expected, &expected_soc) == 6);
            profile = at(temperature, qmax);
            assert(!r1_battery_update(&profile, input, delta, &state));
            assert(state.dod == expected && state.soc == expected_soc);
        } else assert(!"unknown fixture type");
        ++cases;
    }
    assert(feof(file) && !ferror(file));
    fclose(file);
    return cases;
}

static unsigned rejection_tests(void)
{
    struct r1_battery_table tables[4];
    struct r1_battery_profile profile, output, unchanged;
    struct r1_battery_state state = {123, 456};
    r1_battery_int value = 9876;
    unsigned cases = 0;
    size_t i;

    memset(&output, 0x5a, sizeof(output));
    unchanged = output;
    assert(r1_battery_profile_at(NULL, 4, 25, &output) == R1_BATTERY_INVALID);
    assert(!memcmp(&output, &unchanged, sizeof(output)));
    assert(r1_battery_validate_tables(r1_battery_stock_tables, 0) == R1_BATTERY_INVALID);
    assert(r1_battery_validate_tables(r1_battery_stock_tables, 5) == R1_BATTERY_INVALID);
    assert(r1_battery_profile_at(r1_battery_stock_tables, 4, 25, NULL) == R1_BATTERY_INVALID);
    ++cases;

#define BAD_TABLE(change) do { \
    memcpy(tables, r1_battery_stock_tables, sizeof(tables)); \
    change; \
    assert(r1_battery_profile_at(tables, 4, 25, &output) == R1_BATTERY_INVALID); \
    assert(!memcmp(&output, &unchanged, sizeof(output))); \
    ++cases; \
} while (0)
    BAD_TABLE(tables[1].temperature_c = tables[0].temperature_c);
    BAD_TABLE(tables[2].temperature_c = 30);
    BAD_TABLE(tables[1].count = 52);
    BAD_TABLE(tables[0].count = 54);
    BAD_TABLE(tables[0].count = 1);
    BAD_TABLE(tables[0].qmax_01mah = 0);
    BAD_TABLE(tables[0].qmax_high_01mah = -1);
    BAD_TABLE(tables[0].points[0].charge_01mah = -1);
    BAD_TABLE(tables[0].points[2].charge_01mah = 0);
    BAD_TABLE(tables[0].points[1].voltage_01mv = tables[0].points[0].voltage_01mv + 1);
    BAD_TABLE(tables[0].points[0].voltage_01mv = 0);
    BAD_TABLE(tables[0].points[0].resistance_01mohm = 0);
#undef BAD_TABLE

    profile = at(25, 0);
    assert(r1_battery_ocv_to_dod(&profile, 40000, &value) == R1_BATTERY_UNNORMALIZED);
    assert(value == 9876);
    assert(r1_battery_assign_dod(&profile, 0, &output) == R1_BATTERY_INVALID);
    assert(r1_battery_assign_dod(&profile, -1, &output) == R1_BATTERY_INVALID);
    assert(!memcmp(&output, &unchanged, sizeof(output)));
    ++cases;
    assert(!r1_battery_assign_dod(&profile, 10000, &profile));
    assert(r1_battery_ocv_to_dod(&profile, 0, &value) == R1_BATTERY_INVALID);
    assert(r1_battery_ocv_to_dod(&profile, -1, &value) == R1_BATTERY_INVALID);
    assert(r1_battery_dod_to_ocv(NULL, 0, &value) == R1_BATTERY_INVALID);
    assert(r1_battery_update(&profile, 40000, 0, NULL) == R1_BATTERY_INVALID);
    assert(value == 9876);
    ++cases;
    ++profile.dod[2];
    assert(r1_battery_validate_profile(&profile) == R1_BATTERY_INVALID);
    assert(r1_battery_ocv_to_dod(&profile, 40000, &value) == R1_BATTERY_INVALID);
    assert(value == 9876);
    ++cases;

    /* Reject overflow instead of copying ARM division/ushort wrap behavior. */
    memcpy(tables, r1_battery_stock_tables, sizeof(tables));
    tables[0].temperature_c = INT64_MAX;
    tables[1].temperature_c = INT64_MIN;
    assert(r1_battery_profile_at(tables, 2, 0, &output) == R1_BATTERY_OVERFLOW);
    assert(!memcmp(&output, &unchanged, sizeof(output)));
    ++cases;
    profile = at(25, 0);
    profile.curve.points[52].charge_01mah = INT64_MAX;
    assert(r1_battery_assign_dod(&profile, 10000, &output) == R1_BATTERY_OVERFLOW);
    assert(!memcmp(&output, &unchanged, sizeof(output)));
    ++cases;
    profile = at(25, 10000);
    assert(r1_battery_update(&profile, 40000, INT64_MAX, &state) == R1_BATTERY_OVERFLOW);
    assert(r1_battery_update(&profile, 40000, INT64_MIN, &state) == R1_BATTERY_OVERFLOW);
    assert(state.dod == 123 && state.soc == 456);
    ++cases;

    profile.curve.count = 2;
    profile.curve.points[0] = (struct r1_battery_point){0, 40000, 1000};
    profile.curve.points[1] = (struct r1_battery_point){INT64_MAX / 10000, 30000, 1000};
    assert(!r1_battery_assign_dod(&profile, 1, &profile));
    assert(r1_battery_ocv_to_dod(&profile, 35000, &value) == R1_BATTERY_OVERFLOW);
    assert(r1_battery_dod_to_ocv(&profile, INT64_MAX / 2, &value) == R1_BATTERY_OVERFLOW);
    assert(value == 9876);
    assert(r1_battery_update(&profile, 30000, -1, &state) == R1_BATTERY_OVERFLOW);
    assert(r1_battery_update(&profile, 45000, INT64_MAX / 10000, &state) == R1_BATTERY_OVERFLOW);
    assert(state.dod == 123 && state.soc == 456);
    ++cases;

    /* Ascending tables must retain the same cold-side interpolation anchor. */
    for (i = 0; i < 4; ++i) tables[i] = r1_battery_stock_tables[3 - i];
    assert(!r1_battery_profile_at(tables, 4, -5, &output));
    profile = at(-5, 0);
    for (i = 0; i < 53; ++i) {
        assert(output.curve.points[i].charge_01mah == profile.curve.points[i].charge_01mah);
        assert(output.curve.points[i].voltage_01mv == profile.curve.points[i].voltage_01mv);
        assert(output.curve.points[i].resistance_01mohm == profile.curve.points[i].resistance_01mohm);
    }
    ++cases;

    /* Explicit units/rounding contract for a later TEMP adapter. */
    assert(r1_battery_whole_celsius(-99) == -9);
    assert(r1_battery_whole_celsius(-10) == -1);
    assert(r1_battery_whole_celsius(-9) == 0);
    assert(r1_battery_whole_celsius(9) == 0);
    assert(r1_battery_whole_celsius(10) == 1);
    assert(r1_battery_whole_celsius(INT64_MIN) == INT64_MIN / 10);
    assert(r1_battery_whole_celsius(INT64_MAX) == INT64_MAX / 10);
    ++cases;

    /* These two verified rows have the same charge but different voltages. */
    profile = at(-10, 9200);
    assert(profile.dod[46] == 10004 && profile.dod[47] == 10004);
    assert(!r1_battery_dod_to_ocv(&profile, 10004, &value) && value == 36990);
    assert(!r1_battery_dod_to_ocv(&profile, 10005, &value) && value == 34000);
    assert(!r1_battery_ocv_to_dod(&profile, 34000, &value) && value == 10004);
    ++cases;

    /* A fresh temperature profile recomputes the same reference OCV's DOD. */
    profile = at(25, 10000);
    assert(!r1_battery_update(&profile, 40000, 0, &state) && state.dod == 3460);
    profile = at(0, 9800);
    assert(!r1_battery_update(&profile, 40000, 0, &state) && state.dod == 3250);
    ++cases;
    return cases;
}

int main(int argc, char **argv)
{
    unsigned oracles, rejections;
    assert(argc == 2);
    oracles = oracle_tests(argv[1]);
    rejections = rejection_tests();
    printf("PASS: %u stock instruction oracle cases and %u validity/boundary scenarios\n",
           oracles, rejections);
    return 0;
}
