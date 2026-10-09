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

/* Stock's capacity interpolation is anchored at the lower-voltage row. This
 * differs by one unit from reversing the endpoints before signed division. */
static int capacity_at_cutoff(const struct r1_battery_table *curve,
                              r1_battery_int cutoff, r1_battery_int *capacity)
{
    size_t i;
    int ret;
    for (i = 0; i < curve->count; ++i)
        if (curve->points[i].voltage_01mv < cutoff) break;
    if (!i) *capacity = curve->points[0].charge_01mah;
    else if (i == curve->count) *capacity = curve->points[i - 1].charge_01mah;
    else {
        ret = interpolate(curve->points[i].voltage_01mv, curve->points[i].charge_01mah,
                          curve->points[i - 1].voltage_01mv,
                          curve->points[i - 1].charge_01mah, cutoff, capacity);
        if (ret) return ret;
    }
    return *capacity > 0 ? R1_BATTERY_OK : R1_BATTERY_NO_CUTOFF;
}

static int loaded_voltage(r1_battery_int voltage, r1_battery_int resistance,
                           const struct r1_battery_load *load, r1_battery_int *out)
{
    r1_battery_int scaled, total, compensation;
    int ret;
    if ((ret = multiply(resistance, load->dc_ratio_percent, &scaled)) ||
        (ret = add(scaled / 100, load->shunt_01mohm, &total)) ||
        (ret = add(total, load->meter_01mohm, &total)) ||
        (ret = multiply(-load->discharge_01ma, total, &compensation))) return ret;
    /* Preserve both stock divisions, including +5 for negative compensation. */
    compensation /= 1000;
    if ((ret = add(compensation, 5, &compensation))) return ret;
    return add(voltage, compensation / 10, out);
}

static int cutoff_from_load(const struct r1_battery_profile *profile,
                             const struct r1_battery_load *load, r1_battery_int *cutoff)
{
    size_t high;
    r1_battery_int loaded, span, dod, voltage, resistance;
    int ret;
    for (high = profile->curve.count - 1; high > 0; --high)
        if (profile->dod[high - 1] < R1_BATTERY_PERCENT_SCALE) break;
    for (; high > 0; --high) {
        const struct r1_battery_point *point = &profile->curve.points[high - 1];
        if ((ret = loaded_voltage(point->voltage_01mv, point->resistance_01mohm,
                                   load, &loaded))) return ret;
        if (loaded > load->minimum_01mv) break;
    }
    if (!high) return R1_BATTERY_NO_CUTOFF;
    /* No index -1, division by zero, or unbounded loop on a malformed bracket. */
    if ((ret = subtract(profile->dod[high], profile->dod[high - 1], &span))) return ret;
    if (span <= 0) return R1_BATTERY_NO_CUTOFF;
    if (span > R1_BATTERY_PERCENT_SCALE) return R1_BATTERY_SEARCH_LIMIT;
    for (dod = profile->dod[high]; ; dod -= 10) {
        if ((ret = interpolate(profile->dod[high - 1],
                               profile->curve.points[high - 1].voltage_01mv,
                               profile->dod[high], profile->curve.points[high].voltage_01mv,
                               dod, &voltage)) ||
            (ret = interpolate(profile->dod[high - 1],
                               profile->curve.points[high - 1].resistance_01mohm,
                               profile->dod[high], profile->curve.points[high].resistance_01mohm,
                               dod, &resistance)) ||
            (ret = loaded_voltage(voltage, resistance, load, &loaded))) return ret;
        if (loaded > load->minimum_01mv) {
            *cutoff = voltage;
            return R1_BATTERY_OK;
        }
        /* The stock grid starts at this bracket's upper DOD, not a global
         * multiple of ten. Do not invent a successful lower-endpoint sample. */
        if (dod - profile->dod[high - 1] < 10) return R1_BATTERY_NO_CUTOFF;
    }
}

int r1_battery_calculate_usable(const struct r1_battery_profile *profile,
                                const struct r1_battery_load *load,
                                struct r1_battery_capacity *out)
{
    struct r1_battery_capacity result;
    r1_battery_int drop, capacity;
    int ret;
    if (!profile || !load || !out || load->minimum_01mv <= 0 ||
        load->discharge_01ma < 0 || load->rac_01mohm < 0 ||
        load->shunt_01mohm < 0 || load->meter_01mohm < 0 ||
        load->dc_ratio_percent <= 0) return R1_BATTERY_INVALID;
    if ((ret = validate_curve(&profile->curve)) ||
        (ret = multiply(load->discharge_01ma, load->rac_01mohm, &drop)) ||
        (ret = add(load->minimum_01mv, drop / 10000, &result.initial_cutoff_01mv)) ||
        (ret = capacity_at_cutoff(&profile->curve, result.initial_cutoff_01mv,
                                  &result.initial_usable_01mah)) ||
        (ret = r1_battery_assign_dod(profile, result.initial_usable_01mah, &result.profile)) ||
        (ret = cutoff_from_load(&result.profile, load, &result.cutoff_01mv)) ||
        (ret = capacity_at_cutoff(&result.profile.curve, result.cutoff_01mv, &capacity)) ||
        (ret = r1_battery_assign_dod(&result.profile, capacity, &result.profile))) return ret;
    *out = result;
    return R1_BATTERY_OK;
}
