#!/usr/bin/env python3
"""Host regression checks of MT6370 ADC selection and the MIVR work function."""
import os
from pathlib import Path
import re
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
source = ROOT/'src/mainline/drivers/power/supply/mt6370-charger.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT):
        raise SystemExit('Source must be under /rabbitr1')
s = source.read_text()


def function(prefix):
    start = s.index(prefix)
    return s[start:s.index('\n}', start)+2] + '\n'


prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#define MILLI 1000
#define IIO_CURRENT 1
#define IIO_VOLTAGE 2
#define F_CHG_MIVR_STAT 0
#define MT6370_IRQ_MIVR 0
#define container_of(p, t, m) ((t *)((char *)(p) - offsetof(t, m)))
#define dev_err(...) ((void)0)
struct iio_chan_spec { int channel, type; };
struct iio_channel { void *indio_dev; const struct iio_chan_spec *channel; };
struct work_struct { int unused; };
struct mt6370_priv {
    struct iio_channel *iio_ibus;
    struct { struct work_struct work; } mivr_dwork;
    unsigned int irq_nums[1];
    void *dev;
};
static struct iio_channel *expected_adc;
static int current_ua, adc_error, status_error, toggle_error;
static unsigned int active, reads, toggles, reenabled, relaxed;
static int mt6370_chg_field_get(struct mt6370_priv *priv, int field, unsigned int *value)
{
    (void)priv;
    assert(field == F_CHG_MIVR_STAT);
    *value = active;
    return status_error;
}
static int iio_read_channel_processed_scale(struct iio_channel *chan, int *value,
                                            unsigned int scale)
{
    assert(chan == expected_adc);
    reads++;
    /* IIO's current ABI is milliamps; preserve the fractional part when scaling. */
    *value = (long long)current_ua * scale / 1000;
    return adc_error;
}
static int mt6370_chg_toggle_cfo(struct mt6370_priv *priv)
{
    (void)priv;
    toggles++;
    return toggle_error;
}
static void enable_irq(unsigned int irq) { assert(irq == 42); reenabled++; }
static void pm_relax(void *dev) { assert(dev == (void *)1); relaxed++; }
'''
header = ROOT/'src/mainline/include/dt-bindings/iio/adc/mediatek,mt6370_adc.h'
prelude += f'#include "{header}"\n'
prelude += re.search(r'^#define MT6370_MIVR_IBUS_THRESHOLD_UA[^\n]+', s, re.M)[0] + '\n'
body = function('static struct iio_channel *mt6370_chg_find_ibus(')
body += function('static void mt6370_chg_mivr_dwork_func(')
checks = r'''
static void run(struct mt6370_priv *priv, unsigned int want_reads, unsigned int want_toggles)
{
    reads = toggles = reenabled = relaxed = 0;
    mt6370_chg_mivr_dwork_func(&priv->mivr_dwork.work);
    assert(reads == want_reads && toggles == want_toggles);
    assert(reenabled == 1 && relaxed == 1);
}
int main(void)
{
    const struct iio_chan_spec ibus = {MT6370_CHAN_IBUS, IIO_CURRENT};
    const struct iio_chan_spec ibat = {MT6370_CHAN_IBAT, IIO_CURRENT};
    const struct iio_chan_spec wrong_type = {MT6370_CHAN_IBUS, IIO_VOLTAGE};
    struct iio_channel single[] = {{(void *)1, &ibus}, {0}};
    struct iio_channel missing[] = {{(void *)1, &ibat}, {0}};
    struct iio_channel empty[] = {{0}};
    struct iio_channel mixed[] = {
        {(void *)1, &wrong_type}, {(void *)1, &ibat}, {(void *)1, &ibus}, {0}
    };
    assert(mt6370_chg_find_ibus(single) == &single[0]);
    assert(mt6370_chg_find_ibus(missing) == NULL);
    assert(mt6370_chg_find_ibus(empty) == NULL);
    assert(mt6370_chg_find_ibus(mixed) == &mixed[2]);
    /* The legacy complete list and every position in a compact list. */
    struct iio_chan_spec specs[MT6370_CHAN_MAX];
    struct iio_channel full[MT6370_CHAN_MAX + 1] = {0};
    for (int i = 0; i < MT6370_CHAN_MAX; i++) {
        specs[i] = (struct iio_chan_spec){i, i == MT6370_CHAN_IBUS || i == MT6370_CHAN_IBAT ?
                                          IIO_CURRENT : IIO_VOLTAGE};
        full[i] = (struct iio_channel){(void *)1, &specs[i]};
    }
    assert(mt6370_chg_find_ibus(full) == &full[MT6370_CHAN_IBUS]);
    for (int pos = 0; pos < MT6370_CHAN_MAX; pos++) {
        for (int i = 0; i < MT6370_CHAN_MAX; i++)
            full[i].channel = i == pos ? &ibus : &ibat;
        assert(mt6370_chg_find_ibus(full) == &full[pos]);
    }
    expected_adc = &single[0];
    struct mt6370_priv priv = {.iio_ibus = expected_adc, .irq_nums = {42}, .dev = (void *)1};
    active = 1;
    int currents[] = {-1000, 0, 50000, 99500, 100000, 100500, 500000, 1000000};
    for (unsigned int i = 0; i < sizeof(currents) / sizeof(currents[0]); i++) {
        current_ua = currents[i];
        run(&priv, 1, current_ua >= 0 && current_ua < 100000);
    }
    active = 0;
    run(&priv, 0, 0);
    active = 1;
    status_error = -EIO;
    run(&priv, 0, 0);
    status_error = 0;
    adc_error = -ETIMEDOUT;
    run(&priv, 1, 0);
    adc_error = 0;
    current_ua = 50000;
    toggle_error = -EIO;
    run(&priv, 1, 1);
    puts("PASS: MT6370 compact/full/reordered ADC lists, missing IBUS, current threshold and error cleanup");
    return 0;
}
'''
out = ROOT/'out/mt6370-tests'
out.mkdir(parents=True, exist_ok=True)
(out/'charger.c').write_text(prelude + body + checks)
subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O2',
                str(out/'charger.c'), '-o', str(out/'charger')], check=True)
subprocess.run([str(out/'charger')], check=True)
print('Production selection/work code with IIO/regmap stubs; no charger or hardware was accessed.')
