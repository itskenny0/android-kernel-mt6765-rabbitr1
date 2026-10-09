/* SPDX-License-Identifier: GPL-2.0-only */
#include "profile.h"

#define BATT_MAX 9223372036854775807LL
#define BATT_MIN (-BATT_MAX - 1LL)

static int add(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if ((b > 0 && a > BATT_MAX - b) || (b < 0 && a < BATT_MIN - b))
        return R1_BATTERY_OVERFLOW;
    *out = a + b;
    return R1_BATTERY_OK;
}

static int subtract(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if ((b > 0 && a < BATT_MIN + b) || (b < 0 && a > BATT_MAX + b))
        return R1_BATTERY_OVERFLOW;
    *out = a - b;
    return R1_BATTERY_OK;
}

static int multiply(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if (a > 0) {
        if ((b > 0 && a > BATT_MAX / b) || (b < 0 && b < BATT_MIN / a))
            return R1_BATTERY_OVERFLOW;
    } else if (a < 0) {
        if ((b > 0 && a < BATT_MIN / b) || (b < 0 && a < BATT_MAX / b))
            return R1_BATTERY_OVERFLOW;
    }
    *out = a * b;
    return R1_BATTERY_OK;
}

static int divide(r1_battery_int a, r1_battery_int b, r1_battery_int *out)
{
    if (!b) return R1_BATTERY_INVALID;
    if (a == BATT_MIN && b == -1) return R1_BATTERY_OVERFLOW;
    *out = a / b; /* C99 signed division truncates toward zero. */
    return R1_BATTERY_OK;
}

static int interpolate(r1_battery_int x0, r1_battery_int y0,
                       r1_battery_int x1, r1_battery_int y1,
                       r1_battery_int x, r1_battery_int *out)
{
    r1_battery_int dx, dy, position, product, change;
    int ret;
    if ((ret = subtract(x1, x0, &dx)) || (ret = subtract(y1, y0, &dy)) ||
        (ret = subtract(x, x0, &position)) ||
        (ret = multiply(dy, position, &product)) ||
        (ret = divide(product, dx, &change))) return ret;
    return add(y0, change, out);
}

static int percentage(r1_battery_int charge, r1_battery_int usable, r1_battery_int *out)
{
    r1_battery_int scaled;
    int ret = multiply(charge, R1_BATTERY_PERCENT_SCALE, &scaled);
    if (ret) return ret;
    return divide(scaled, usable, out);
}

static int validate_curve(const struct r1_battery_table *table)
{
    size_t i;
    if (!table || table->count < 2 || table->count > R1_BATTERY_ROWS ||
        table->qmax_01mah <= 0 || table->qmax_high_01mah <= 0)
        return R1_BATTERY_INVALID;
    for (i = 0; i < table->count; ++i) {
        const struct r1_battery_point *point = &table->points[i];
        if (point->charge_01mah < 0 || point->voltage_01mv <= 0 ||
            point->resistance_01mohm <= 0) return R1_BATTERY_INVALID;
        if (i && (point->charge_01mah < table->points[i - 1].charge_01mah ||
                  point->voltage_01mv > table->points[i - 1].voltage_01mv))
            return R1_BATTERY_INVALID;
    }
    return table->points[table->count - 1].charge_01mah > 0 ?
           R1_BATTERY_OK : R1_BATTERY_INVALID;
}

r1_battery_int r1_battery_whole_celsius(r1_battery_int temperature_decic)
{
    return temperature_decic / 10;
}

int r1_battery_validate_tables(const struct r1_battery_table *tables, size_t count)
{
    size_t i;
    int ascending, ret;
    if (!tables || count < 2 || count > R1_BATTERY_TABLES) return R1_BATTERY_INVALID;
    ascending = tables[1].temperature_c > tables[0].temperature_c;
    for (i = 0; i < count; ++i) {
        if ((ret = validate_curve(&tables[i]))) return ret;
        if (tables[i].count != tables[0].count) return R1_BATTERY_INVALID;
        if (i && (ascending ? tables[i].temperature_c <= tables[i - 1].temperature_c :
                             tables[i].temperature_c >= tables[i - 1].temperature_c))
            return R1_BATTERY_INVALID;
    }
    return R1_BATTERY_OK;
}

int r1_battery_profile_at(const struct r1_battery_table *tables, size_t count,
                          r1_battery_int temperature_c, struct r1_battery_profile *out)
{
    struct r1_battery_profile result = {0};
    const struct r1_battery_table *low, *high;
    size_t i, bracket;
    int ascending, ret;
    if (!out) return R1_BATTERY_INVALID;
    if ((ret = r1_battery_validate_tables(tables, count))) return ret;
    ascending = tables[1].temperature_c > tables[0].temperature_c;
    for (bracket = 1; bracket < count - 1; ++bracket)
        if (ascending ? temperature_c <= tables[bracket].temperature_c :
                        temperature_c >= tables[bracket].temperature_c) break;
    low = &tables[ascending ? bracket - 1 : bracket];
    high = &tables[ascending ? bracket : bracket - 1];
    if (temperature_c < low->temperature_c) temperature_c = low->temperature_c;
    if (temperature_c > high->temperature_c) temperature_c = high->temperature_c;
    result.curve.temperature_c = temperature_c;
    result.curve.count = low->count;
    for (i = 0; i < low->count; ++i) {
        struct r1_battery_point *point = &result.curve.points[i];
        if ((ret = interpolate(low->temperature_c, low->points[i].charge_01mah,
                               high->temperature_c, high->points[i].charge_01mah,
                               temperature_c, &point->charge_01mah)) ||
            (ret = interpolate(low->temperature_c, low->points[i].voltage_01mv,
                               high->temperature_c, high->points[i].voltage_01mv,
                               temperature_c, &point->voltage_01mv)) ||
            (ret = interpolate(low->temperature_c, low->points[i].resistance_01mohm,
                               high->temperature_c, high->points[i].resistance_01mohm,
                               temperature_c, &point->resistance_01mohm))) return ret;
    }
    if ((ret = interpolate(low->temperature_c, low->qmax_01mah,
                           high->temperature_c, high->qmax_01mah, temperature_c,
                           &result.curve.qmax_01mah)) ||
        (ret = interpolate(low->temperature_c, low->qmax_high_01mah,
                           high->temperature_c, high->qmax_high_01mah, temperature_c,
                           &result.curve.qmax_high_01mah)) ||
        (ret = validate_curve(&result.curve))) return ret;
    *out = result;
    return R1_BATTERY_OK;
}

int r1_battery_assign_dod(const struct r1_battery_profile *profile,
                          r1_battery_int usable_01mah, struct r1_battery_profile *out)
{
    struct r1_battery_profile result;
    size_t i;
    int ret;
    if (!profile || !out || usable_01mah <= 0) return R1_BATTERY_INVALID;
    if ((ret = validate_curve(&profile->curve))) return ret;
    result = *profile;
    result.usable_01mah = usable_01mah;
    for (i = 0; i < profile->curve.count; ++i)
        if ((ret = percentage(profile->curve.points[i].charge_01mah, usable_01mah,
                              &result.dod[i]))) return ret;
    *out = result;
    return R1_BATTERY_OK;
}

int r1_battery_validate_profile(const struct r1_battery_profile *profile)
{
    size_t i;
    r1_battery_int expected;
    int ret;
    if (!profile) return R1_BATTERY_INVALID;
    if ((ret = validate_curve(&profile->curve))) return ret;
    if (profile->usable_01mah <= 0) return R1_BATTERY_UNNORMALIZED;
    for (i = 0; i < profile->curve.count; ++i) {
        if ((ret = percentage(profile->curve.points[i].charge_01mah,
                              profile->usable_01mah, &expected))) return ret;
        if (profile->dod[i] != expected) return R1_BATTERY_INVALID;
    }
    return R1_BATTERY_OK;
}

int r1_battery_ocv_to_dod(const struct r1_battery_profile *profile,
                          r1_battery_int ocv_01mv, r1_battery_int *dod)
{
    size_t i;
    int ret;
    if (!dod || ocv_01mv <= 0) return R1_BATTERY_INVALID;
    if ((ret = r1_battery_validate_profile(profile))) return ret;
    /* The first crossing preserves cold-profile repeated charge/voltage rows. */
    for (i = 0; i < profile->curve.count; ++i)
        if (profile->curve.points[i].voltage_01mv <= ocv_01mv) break;
    if (!i) { *dod = profile->dod[0]; return R1_BATTERY_OK; }
    if (i == profile->curve.count) { *dod = profile->dod[i - 1]; return R1_BATTERY_OK; }
    return interpolate(profile->curve.points[i - 1].voltage_01mv, profile->dod[i - 1],
                       profile->curve.points[i].voltage_01mv, profile->dod[i], ocv_01mv, dod);
}

int r1_battery_dod_to_ocv(const struct r1_battery_profile *profile,
                          r1_battery_int dod, r1_battery_int *ocv_01mv)
{
    size_t i;
    int ret;
    if (!ocv_01mv) return R1_BATTERY_INVALID;
    if ((ret = r1_battery_validate_profile(profile))) return ret;
    for (i = 0; i < profile->curve.count; ++i)
        if (profile->dod[i] >= dod) break;
    if (!i) { *ocv_01mv = profile->curve.points[0].voltage_01mv; return R1_BATTERY_OK; }
    if (i == profile->curve.count) {
        *ocv_01mv = profile->curve.points[i - 1].voltage_01mv;
        return R1_BATTERY_OK;
    }
    return interpolate(profile->dod[i - 1], profile->curve.points[i - 1].voltage_01mv,
                       profile->dod[i], profile->curve.points[i].voltage_01mv, dod, ocv_01mv);
}

int r1_battery_update(const struct r1_battery_profile *profile,
                      r1_battery_int reference_ocv_01mv, r1_battery_int delta_01mah,
                      struct r1_battery_state *out)
{
    struct r1_battery_state result;
    r1_battery_int reference_dod, change;
    int ret;
    if (!out) return R1_BATTERY_INVALID;
    if ((ret = r1_battery_ocv_to_dod(profile, reference_ocv_01mv, &reference_dod)) ||
        (ret = percentage(delta_01mah, profile->usable_01mah, &change)) ||
        (ret = subtract(reference_dod, change, &result.dod)) ||
        (ret = subtract(R1_BATTERY_PERCENT_SCALE, result.dod, &result.soc))) return ret;
    *out = result;
    return R1_BATTERY_OK;
}
