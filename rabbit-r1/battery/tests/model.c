/* SPDX-License-Identifier: GPL-2.0-only */
#include "profile.h"
#include <assert.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

static unsigned checks;

static struct r1_battery_profile curve(const struct r1_battery_point *points, size_t count)
{
    struct r1_battery_profile p = {0};
    assert(count <= R1_BATTERY_ROWS);
    p.curve.count = count;
    p.curve.qmax_01mah = 1; p.curve.qmax_high_01mah = 1;
    memcpy(p.curve.points, points, count * sizeof(*points));
    /* Deliberately unusable old normalization; model must ignore these. */
    p.usable_01mah = INT64_MIN; p.dod[0] = INT64_MAX;
    return p;
}

static void seed(const struct r1_battery_profile *p, r1_battery_int v,
                 const struct r1_battery_model_parameters *m, int error,
                 r1_battery_int q, r1_battery_int ocv, r1_battery_int r)
{
    struct r1_battery_model_point out, saved;
    struct r1_battery_profile before;
    struct r1_battery_model_parameters prior;
    int actual;
    if (p) before = *p;
    if (m) prior = *m;
    memset(&out, 0x5a, sizeof(out)); saved = out;
    actual = r1_battery_model_seed(p, v, m, &out);
    if (actual != error) fprintf(stderr, "seed check %u: %d != %d\n", checks, actual, error);
    assert(actual == error);
    if (error) assert(!memcmp(&out, &saved, sizeof(out)));
    else {
        if (out.charge_01mah != q || out.ocv_01mv != ocv || out.resistance_01mohm != r)
            fprintf(stderr, "seed check %u: got %" PRId64 ",%" PRId64 ",%" PRId64
                    " expected %" PRId64 ",%" PRId64 ",%" PRId64 "\n", checks,
                    out.charge_01mah, out.ocv_01mv, out.resistance_01mohm, q, ocv, r);
        assert(out.charge_01mah == q && out.ocv_01mv == ocv && out.resistance_01mohm == r);
    }
    if (p) assert(!memcmp(p, &before, sizeof(before)));
    if (m) assert(!memcmp(m, &prior, sizeof(prior)));
    ++checks;
}

static void cutoff(const struct r1_battery_profile *p, r1_battery_int v,
                   const struct r1_battery_model_parameters *m, int error,
                   r1_battery_int q, r1_battery_int ocv, r1_battery_int r, int boundary)
{
    struct r1_battery_model_capacity out, saved;
    struct r1_battery_profile before;
    struct r1_battery_model_parameters prior;
    int actual;
    if (p) before = *p;
    if (m) prior = *m;
    memset(&out, 0x5a, sizeof(out)); saved = out;
    actual = r1_battery_model_cutoff(p, v, m, &out);
    if (actual != error) fprintf(stderr, "cutoff check %u: %d != %d\n", checks, actual, error);
    assert(actual == error);
    if (error) assert(!memcmp(&out, &saved, sizeof(out)));
    else {
        if (out.point.charge_01mah != q || out.point.ocv_01mv != ocv ||
            out.point.resistance_01mohm != r || (int)out.boundary != boundary)
            fprintf(stderr, "cutoff check %u: got %" PRId64 ",%" PRId64 ",%" PRId64 ",%d"
                    " expected %" PRId64 ",%" PRId64 ",%" PRId64 ",%d\n", checks,
                    out.point.charge_01mah, out.point.ocv_01mv, out.point.resistance_01mohm,
                    (int)out.boundary, q, ocv, r, boundary);
        assert(out.point.charge_01mah == q && out.point.ocv_01mv == ocv &&
               out.point.resistance_01mohm == r && (int)out.boundary == boundary);
    }
    if (p) assert(!memcmp(p, &before, sizeof(before)));
    if (m) assert(!memcmp(m, &prior, sizeof(prior)));
    ++checks;
}

static void reference(const char *path)
{
    FILE *f = fopen(path, "r");
    char operation;
    assert(f);
    while (fscanf(f, " %c", &operation) == 1) {
        struct r1_battery_profile p;
        struct r1_battery_model_parameters m;
        r1_battery_int temp, v, q = 0, ocv = 0, r = 0;
        int error, boundary = 0;
        assert(fscanf(f, " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %" SCNd64 " %d",
                      &temp, &v, &m.current_01ma, &m.shunt_01mohm, &m.meter_01mohm,
                      &m.dc_ratio_percent, &error) == 7);
        assert(!r1_battery_profile_at(r1_battery_stock_tables, R1_BATTERY_TABLES, temp, &p));
        if (!error) {
            assert(fscanf(f, " %" SCNd64 " %" SCNd64 " %" SCNd64, &q, &ocv, &r) == 3);
            if (operation == 'C') assert(fscanf(f, " %d", &boundary) == 1);
        }
        if (operation == 'S') seed(&p, v, &m, error, q, ocv, r);
        else { assert(operation == 'C'); cutoff(&p, v, &m, error, q, ocv, r, boundary); }
    }
    assert(feof(f) && !ferror(f));
    fclose(f);
}

static void boundaries(void)
{
    const struct r1_battery_point simple[] = {{0,40000,1000},{1000,30000,2000}};
    struct r1_battery_profile p = curve(simple, 2), original = p;
    const struct r1_battery_model_parameters discharge = {-10000,0,0,100};
    struct r1_battery_model_parameters m = discharge;
    /* Hand calculation: loaded endpoints 39000 and 28000; midpoint 33500. */
    seed(&p, 33500, &m, 0, 500,35000,1500);
    cutoff(&p, 33500, &m, 0, 500,35000,1500,0);
    /* Fraction = 5499/11000; floor each positive quantity separately. */
    seed(&p, 33501, &m, 0, 499,35000,1499);
    cutoff(&p, 33501, &m, 0, 499,35000,1499,0);
    m.current_01ma = 10000;
    seed(&p, 36500, &m, 0, 500,35000,1500); /* Charge raises terminal V. */
    cutoff(&p, 36500, &m, R1_BATTERY_INVALID, 0,0,0,0);
    m = discharge; m.dc_ratio_percent = 50; m.shunt_01mohm = 100; m.meter_01mohm = 200;
    seed(&p, 33950, &m, 0, 500,35000,1500);
    m = discharge;
    seed(&p, 39000, &m, 0, 0,40000,1000);
    seed(&p, 28000, &m, 0, 1000,30000,2000);
    seed(&p, 39001, &m, R1_BATTERY_NO_ROOT, 0,0,0);
    seed(&p, 27999, &m, R1_BATTERY_NO_ROOT, 0,0,0);
    cutoff(&p, 39000, &m, R1_BATTERY_NO_CUTOFF, 0,0,0,0);
    cutoff(&p, 40000, &m, R1_BATTERY_NO_CUTOFF, 0,0,0,0);
    cutoff(&p, 28000, &m, 0, 1000,30000,2000,0);
    cutoff(&p, 27999, &m, 0, 1000,30000,2000,2);
    cutoff(&p, 38999, &m, R1_BATTERY_NO_CUTOFF, 0,0,0,0); /* q=1/11 <1. */
    p.curve.points[0].resistance_01mohm = 3000;
    p.curve.points[1].resistance_01mohm = 1000;
    seed(&p, 33500, &m, 0, 437,35625,2125); /* Decreasing R, exact q=437.5. */
    {
        const struct r1_battery_point points[] = {{0,40000,1000},{500,35000,1500},
                                                  {500,35000,1500},{1000,30000,2000}};
        p = curve(points, 4);
        seed(&p, 33500, &m, 0, 500,35000,1500); /* Shared/duplicate vertex once. */
        seed(&p, 33501, &m, 0, 499,35000,1499);
        p.curve.points[2].voltage_01mv = 34000;
        p.curve.points[2].resistance_01mohm = 1600;
        seed(&p, 33000, &m, R1_BATTERY_AMBIGUOUS, 0,0,0);
        seed(&p, 33500, &m, R1_BATTERY_AMBIGUOUS, 0,0,0); /* Jump endpoint. */
        seed(&p, 32400, &m, R1_BATTERY_AMBIGUOUS, 0,0,0);
        cutoff(&p, 33000, &m, 0, 500,34545,1545,1);
        cutoff(&p, 32400, &m, 0, 500,34000,1600,1);
        cutoff(&p, 33500, &m, 0, 500,35000,1500,0); /* First reach before jump. */
    }
    {
        const struct r1_battery_point points[] = {{0,40000,1000},{1000,39000,5000},
                                                  {2000,38000,1000},{3000,30000,1000}};
        p = curve(points, 4);
        /* Loaded 39000,34000,37000,29000: first loss cannot be restored. */
        cutoff(&p, 35000, &m, 0, 800,39200,4200,0);
        seed(&p, 35000, &m, R1_BATTERY_AMBIGUOUS, 0,0,0);
    }
    {
        const struct r1_battery_point flat[] = {{0,40000,1000},{1000,39000,2000}};
        p = curve(flat, 2); m.current_01ma = 10000;
        seed(&p, 41000, &m, R1_BATTERY_AMBIGUOUS, 0,0,0); /* Exact IR cancellation. */
        seed(&p, 41001, &m, R1_BATTERY_NO_ROOT, 0,0,0);
        p.curve.points[1].voltage_01mv = 40000; m.current_01ma = 0;
        seed(&p, 40000, &m, R1_BATTERY_AMBIGUOUS, 0,0,0);
    }
    p = original; m = discharge;
    seed(NULL,33500,&m,R1_BATTERY_INVALID,0,0,0);
    seed(&p,33500,NULL,R1_BATTERY_INVALID,0,0,0);
    cutoff(NULL,33500,&m,R1_BATTERY_INVALID,0,0,0,0);
    cutoff(&p,33500,NULL,R1_BATTERY_INVALID,0,0,0,0);
    assert(r1_battery_model_seed(&p,33500,&m,NULL) == R1_BATTERY_INVALID);
    assert(r1_battery_model_cutoff(&p,33500,&m,NULL) == R1_BATTERY_INVALID); checks += 2;
#define BAD(change, expected) do { \
    p = original; m = discharge; change; \
    seed(&p,33500,&m,expected,0,0,0); cutoff(&p,33500,&m,expected,0,0,0,0); \
} while (0)
    BAD(m.shunt_01mohm = -1, R1_BATTERY_INVALID);
    BAD(m.meter_01mohm = -1, R1_BATTERY_INVALID);
    BAD(m.dc_ratio_percent = 0, R1_BATTERY_INVALID);
    BAD(m.dc_ratio_percent = -1, R1_BATTERY_INVALID);
    BAD(p.curve.count = 1, R1_BATTERY_INVALID);
    BAD(p.curve.count = R1_BATTERY_ROWS + 1, R1_BATTERY_INVALID);
    BAD(p.curve.qmax_01mah = 0, R1_BATTERY_INVALID);
    BAD(p.curve.points[0].charge_01mah = -1, R1_BATTERY_INVALID);
    BAD(p.curve.points[1].charge_01mah = 0, R1_BATTERY_INVALID);
    BAD(p.curve.points[1].voltage_01mv = 40001, R1_BATTERY_INVALID);
    BAD(p.curve.points[1].resistance_01mohm = 0, R1_BATTERY_INVALID);
    BAD(p.curve.points[1].resistance_01mohm = -1, R1_BATTERY_INVALID);
    BAD(m.shunt_01mohm = INT64_MAX; m.meter_01mohm = 1, R1_BATTERY_OVERFLOW);
    BAD(m.shunt_01mohm = INT64_MAX, R1_BATTERY_OVERFLOW);
    BAD(m.dc_ratio_percent = INT64_MAX, R1_BATTERY_OVERFLOW);
    BAD(m.current_01ma = INT64_MIN, R1_BATTERY_OVERFLOW);
    BAD(p.curve.points[0].voltage_01mv = INT64_MAX, R1_BATTERY_OVERFLOW);
    BAD(p.curve.points[1].charge_01mah = INT64_MAX, R1_BATTERY_OVERFLOW);
    BAD(p.curve.points[1].resistance_01mohm = INT64_MAX / 100;
        m.current_01ma = 0, R1_BATTERY_OVERFLOW); /* Interpolation product only. */
#undef BAD
    p = original; m = discharge;
    seed(&p,0,&m,R1_BATTERY_INVALID,0,0,0);
    seed(&p,-1,&m,R1_BATTERY_INVALID,0,0,0);
    cutoff(&p,0,&m,R1_BATTERY_INVALID,0,0,0,0);
    cutoff(&p,-1,&m,R1_BATTERY_INVALID,0,0,0,0);
    p.curve.points[0].charge_01mah = 1;
    cutoff(&p,33500,&m,R1_BATTERY_INVALID,0,0,0,0);
    p = original; m.current_01ma = 0;
    p.curve.points[0].voltage_01mv = 9300000000000LL;
    p.curve.points[1].voltage_01mv = 1;
    seed(&p,4650000000000LL,&m,R1_BATTERY_OVERFLOW,0,0,0);
    cutoff(&p,4650000000000LL,&m,R1_BATTERY_OVERFLOW,0,0,0,0);
    /* Same floored OCV at distinct exact roots remains ambiguous. */
    {
        const struct r1_battery_point points[] = {{0,40000,1000},{1,40000,3000},{2,40000,1000}};
        p = curve(points, 3); m.current_01ma = 10000;
        seed(&p,42000,&m,R1_BATTERY_AMBIGUOUS,0,0,0);
    }
}

int main(int argc, char **argv)
{
    unsigned count;
    assert(argc == 2);
    reference(argv[1]); count = checks;
    boundaries();
    printf("PASS: %u exact rational model cases + %u hand-derived/error boundaries (ASan/UBSan)\n",
           count, checks - count);
    return 0;
}
