// SPDX-License-Identifier: GPL-2.0-only
/* MT6357 battery current, charge counter, voltage and thermistor sensing. */

#include <linux/atomic.h>
#include <linux/bitfield.h>
#include <linux/cleanup.h>
#include <linux/iio/consumer.h>
#include <linux/iio/iio.h>
#include <linux/ktime.h>
#include <linux/math64.h>
#include <linux/mfd/mt6357/registers.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/property.h>
#include <linux/regmap.h>
#include <linux/workqueue.h>

#define MT6357_FG_ON		BIT(0)
#define MT6357_FG_CLOCK_PD	(BIT(3) | BIT(4))
#define MT6357_FG_READ_PRE	BIT(0)
#define MT6357_FG_CLEAR		BIT(3)
#define MT6357_FG_LATCH_READY	BIT(15)
#define MT6357_FG_CURRENT_MASK	GENMASK(15, 0)
#define MT6357_FG_CAR_MASK	GENMASK(30, 11)
#define MT6357_FG_CAR_SIGN	BIT(31)
#define MT6357_FG_POLL_US	100
#define MT6357_FG_TIMEOUT_US	20000
#define MT6357_THERMISTOR_MAX_POINTS 64
#define MT6357_STATUS_POLL_INTERVAL (5 * HZ)

struct mt6357_thermistor_point {
	s32 temperature_mc;
	u32 resistance;
};

/* Private observations, not a power-supply ABI or an accepted SOC seed. */
struct mt6357_live_value {
	int value;
	int error;
};

enum mt6357_fg_word {
	MT6357_LIVE_ENGINE, MT6357_LIVE_CLOCK, MT6357_LIVE_CURRENT,
	MT6357_LIVE_CAR_LOW, MT6357_LIVE_CAR_HIGH, MT6357_LIVE_BATON,
	MT6357_LIVE_WORDS,
};

struct mt6357_live_fg {
	u64 started_ns, ready_ns, finished_ns;
	struct mt6357_live_value words[MT6357_LIVE_WORDS];
	int prepare_error, latch_error, release_error;
	struct mt6357_live_value current_ua, charge_uah, present;
};

struct mt6357_live_adc {
	u64 started_ns, finished_ns;
	struct mt6357_live_value raw, mv;
	int scale_type, scale_numerator, scale_denominator, scale_error;
};

struct mt6357_live_observation {
	u64 generation, sequence, started_ns, finished_ns;
	struct mt6357_live_fg fg[2];
	/* ISENSE, BAT_TEMP, VBIF in each bracketed ADC set. */
	struct mt6357_live_adc adc[2][3];
	struct mt6357_live_value temperature_decic[2];
	int error;
};

#define MT6357_READ_CURRENT BIT(0)
#define MT6357_READ_CAR BIT(1)
#define MT6357_READ_BATON BIT(2)

struct mt6357_gauge {
	struct device *dev;
	struct regmap *regmap;
	struct mutex lock;
	u32 shunt_uohms;
	u32 gain_permille;
	bool needs_release;
	u64 live_generation, live_sequence;
	const struct power_supply_desc *desc;
	struct notifier_block status_nb;
	struct power_supply *charger;
	struct power_supply *psy;
	struct mutex status_lock;
	struct delayed_work status_work;
	bool status_active;
	bool status_seen;
	bool status_valid;
	int status;
	struct iio_channel *voltage;
	struct iio_channel *thermistor;
	struct iio_channel *reference;
	struct mt6357_thermistor_point *table;
	unsigned int num_points;
	u32 pullup_ohms;
	u32 series_uohms;
};

static atomic64_t mt6357_live_generation = ATOMIC64_INIT(0);

static int mt6357_gauge_convert(struct mt6357_gauge *gauge, unsigned int raw,
				int *current_ua)
{
	unsigned int sample = FIELD_GET(MT6357_FG_CURRENT_MASK, raw);
	bool negative = sample & BIT(15);
	u64 magnitude;

	/* The current register is ones' complement, including negative zero. */
	magnitude = negative ? 0xffff - sample : sample;
	/* Preserve stock rounding in 0.1 mA before shunt and gain correction. */
	magnitude = div_u64(magnitude * 314331, 100000);
	magnitude = div_u64(magnitude * 10000, gauge->shunt_uohms);
	magnitude = div_u64(magnitude * gauge->gain_permille, 1000);
	if (magnitude > INT_MAX / 100)
		return -ERANGE;

	*current_ua = negative ? -(int)magnitude * 100 : (int)magnitude * 100;
	return 0;
}

static int mt6357_gauge_convert_charge(struct mt6357_gauge *gauge, unsigned int raw,
				      int *charge_uah)
{
	unsigned int sample = FIELD_GET(MT6357_FG_CAR_MASK, raw);
	bool negative = raw & MT6357_FG_CAR_SIGN;
	u64 magnitude;

	/* Stock treats both all-zero and all-one magnitude fields as zero. */
	if (!sample || sample == 0xfffff)
		magnitude = 0;
	else
		magnitude = negative ? 0xfffff - sample : sample;

	/* Preserve stock rounding in 0.1 mAh before shunt and gain correction. */
	magnitude = div_u64(magnitude * 11176, 10000);
	magnitude = div_u64(magnitude + 5, 10);
	magnitude = div_u64(magnitude * 10000, gauge->shunt_uohms);
	magnitude = div_u64(magnitude * gauge->gain_permille, 1000);
	if (magnitude > INT_MAX / 100)
		return -ERANGE;

	/* Signed accumulation since hardware reset, not remaining capacity. */
	*charge_uah = negative ? -(int)magnitude * 100 : (int)magnitude * 100;
	return 0;
}

static int mt6357_gauge_release(struct mt6357_gauge *gauge)
{
	unsigned int state;
	int ret, err;

	ret = regmap_update_bits(gauge->regmap, MT6357_FGADC_CON1,
				 MT6357_FG_CLEAR, MT6357_FG_CLEAR);
	err = regmap_update_bits(gauge->regmap, MT6357_FGADC_CON1,
				 MT6357_FG_READ_PRE, 0);
	if (!ret)
		ret = err;

	err = regmap_read_poll_timeout(gauge->regmap, MT6357_FGADC_CON1, state,
				       !(state & MT6357_FG_LATCH_READY),
				       MT6357_FG_POLL_US, MT6357_FG_TIMEOUT_US);
	if (!ret)
		ret = err;

	/* Attempt every cleanup operation, including when a prior write failed. */
	err = regmap_update_bits(gauge->regmap, MT6357_FGADC_CON1,
				 MT6357_FG_CLEAR, 0);
	if (!ret)
		ret = err;

	gauge->needs_release = !!ret;
	return ret;
}

static int mt6357_gauge_live_word(struct mt6357_gauge *gauge, unsigned int reg,
				 struct mt6357_live_value *word)
{
	unsigned int raw;

	word->error = regmap_read(gauge->regmap, reg, &raw);
	if (!word->error)
		word->value = raw & 0xffff;
	return word->error;
}

static void mt6357_gauge_init_fg(struct mt6357_live_fg *sample)
{
	unsigned int i;

	*sample = (struct mt6357_live_fg) {};
	for (i = 0; i < MT6357_LIVE_WORDS; i++)
		sample->words[i].error = -ENODATA;
	sample->prepare_error = sample->latch_error = sample->release_error = -ENODATA;
	sample->current_ua.error = sample->charge_uah.error = sample->present.error = -ENODATA;
}

/* The same transaction serves ordinary properties and paired live samples. */
static int mt6357_gauge_latch_locked(struct mt6357_gauge *gauge, unsigned int fields,
				    struct mt6357_live_fg *sample)
{
	unsigned int state;
	int ret, err;

	lockdep_assert_held(&gauge->lock);
	mt6357_gauge_init_fg(sample);
	sample->started_ns = ktime_get_boottime_ns();
	ret = mt6357_gauge_live_word(gauge, MT6357_FGADC_CON0,
				     &sample->words[MT6357_LIVE_ENGINE]);
	if (!ret && !(sample->words[MT6357_LIVE_ENGINE].value & MT6357_FG_ON))
		ret = -EAGAIN;
	if (!ret)
		ret = mt6357_gauge_live_word(gauge, MT6357_BM_TOP_CKPDN_CON0,
					     &sample->words[MT6357_LIVE_CLOCK]);
	if (!ret && (sample->words[MT6357_LIVE_CLOCK].value & MT6357_FG_CLOCK_PD))
		ret = -EAGAIN;
	if (!ret && gauge->needs_release)
		ret = mt6357_gauge_release(gauge);
	sample->prepare_error = ret;
	if (ret)
		goto done;

	gauge->needs_release = true;
	ret = regmap_update_bits(gauge->regmap, MT6357_FGADC_CON1,
				 MT6357_FG_READ_PRE, MT6357_FG_READ_PRE);
	if (!ret)
		ret = regmap_read_poll_timeout(gauge->regmap, MT6357_FGADC_CON1, state,
					       state & MT6357_FG_LATCH_READY,
					       MT6357_FG_POLL_US, MT6357_FG_TIMEOUT_US);
	sample->latch_error = ret;
	if (!ret) {
		sample->ready_ns = ktime_get_boottime_ns();
		if (fields & MT6357_READ_CURRENT)
			ret = mt6357_gauge_live_word(gauge, MT6357_FGADC_CUR_CON0,
						     &sample->words[MT6357_LIVE_CURRENT]);
		if (fields & MT6357_READ_CAR) {
			err = mt6357_gauge_live_word(gauge, MT6357_FGADC_CAR_CON0,
						     &sample->words[MT6357_LIVE_CAR_LOW]);
			if (!ret)
				ret = err;
			/* Preserve the legacy single-counter failure/read ordering. */
			if (!err || (fields & MT6357_READ_CURRENT)) {
				err = mt6357_gauge_live_word(gauge, MT6357_FGADC_CAR_CON1,
							     &sample->words[MT6357_LIVE_CAR_HIGH]);
				if (!ret)
					ret = err;
			}
		}
		if (fields & MT6357_READ_BATON) {
			err = mt6357_gauge_live_word(gauge, MT6357_BATON_ANA_CON0,
						     &sample->words[MT6357_LIVE_BATON]);
			sample->present.error = err;
			if (!err) {
				state = sample->words[MT6357_LIVE_BATON].value;
				if (!(state & BIT(0)))
					sample->present.error = -EAGAIN;
				else
					sample->present.value = !(state & BIT(1));
			}
		}
	}

	sample->release_error = mt6357_gauge_release(gauge);
	if (sample->release_error)
		dev_err_ratelimited(gauge->dev, "Failed to release gauge latch: %d\n",
				    sample->release_error);
	if (!ret)
		ret = sample->release_error;
done:
	sample->finished_ns = ktime_get_boottime_ns();
	return ret;
}

static int mt6357_gauge_read_raw(struct mt6357_gauge *gauge, bool counter,
				  unsigned int *raw)
{
	struct mt6357_live_fg sample;
	int ret;

	guard(mutex)(&gauge->lock);
	ret = mt6357_gauge_latch_locked(gauge,
			counter ? MT6357_READ_CAR : MT6357_READ_CURRENT, &sample);
	if (ret)
		return ret;
	/* Publish only a sample whose complete transaction succeeded. */
	*raw = counter ? (unsigned int)sample.words[MT6357_LIVE_CAR_LOW].value |
			 ((unsigned int)sample.words[MT6357_LIVE_CAR_HIGH].value << 16) :
			 (unsigned int)sample.words[MT6357_LIVE_CURRENT].value;
	return 0;
}

static int mt6357_gauge_read_current(struct mt6357_gauge *gauge, int *current_ua)
{
	unsigned int raw;
	int ret;

	ret = mt6357_gauge_read_raw(gauge, false, &raw);
	if (ret)
		return ret;
	return mt6357_gauge_convert(gauge, raw, current_ua);
}

static int mt6357_gauge_read_charge(struct mt6357_gauge *gauge, int *charge_uah)
{
	unsigned int raw;
	int ret;

	ret = mt6357_gauge_read_raw(gauge, true, &raw);
	if (ret)
		return ret;
	return mt6357_gauge_convert_charge(gauge, raw, charge_uah);
}

static int mt6357_gauge_read_voltage(struct mt6357_gauge *gauge, int *voltage_uv)
{
	int ret, voltage_mv;

	if (!gauge->voltage)
		return -ENODATA;
	ret = iio_read_channel_processed(gauge->voltage, &voltage_mv);
	if (ret < 0)
		return ret;
	if (voltage_mv <= 0 || voltage_mv > INT_MAX / 1000)
		return -ERANGE;

	*voltage_uv = voltage_mv * 1000;
	return 0;
}

static int mt6357_gauge_temperature(struct mt6357_gauge *gauge, int voltage_mv,
				    int reference_mv, int current_ua, int *temperature)
{
	const struct mt6357_thermistor_point *cold, *hot;
	s64 correction, voltage, reference, weighted;
	u64 resistance;
	unsigned int i;

	if (voltage_mv <= 0 || reference_mv <= voltage_mv)
		return -ERANGE;

	/* Match the stock integer-mA and then integer-mV compensation steps. */
	correction = -div_s64((s64)(current_ua / 1000) * gauge->series_uohms, 1000000);
	voltage = voltage_mv + correction;
	reference = reference_mv + correction;
	if (voltage <= 0 || reference > INT_MAX)
		return -ERANGE;

	/* Both voltages include the same return-path drop. It cancels here. */
	resistance = div_u64((u64)gauge->pullup_ohms * voltage, reference - voltage);
	if (resistance >= gauge->table[0].resistance) {
		*temperature = gauge->table[0].temperature_mc / 100;
		return 0;
	}
	for (i = 1; i < gauge->num_points; i++) {
		cold = &gauge->table[i - 1];
		hot = &gauge->table[i];
		if (resistance < hot->resistance)
			continue;

		weighted = (s64)(resistance - hot->resistance) * cold->temperature_mc +
			   (s64)(cold->resistance - resistance) * hot->temperature_mc;
		*temperature = div64_s64(weighted,
					(s64)(cold->resistance - hot->resistance) * 100);
		return 0;
	}
	*temperature = gauge->table[gauge->num_points - 1].temperature_mc / 100;
	return 0;
}

static int mt6357_gauge_live_pair(struct mt6357_gauge *gauge,
				 struct mt6357_live_fg *sample)
{
	unsigned int raw;
	int ret;

	ret = mt6357_gauge_latch_locked(gauge, MT6357_READ_CURRENT | MT6357_READ_CAR |
				       MT6357_READ_BATON, sample);
	sample->current_ua.error = sample->charge_uah.error = ret;
	if (ret)
		return ret;
	sample->current_ua.error = mt6357_gauge_convert(gauge,
		sample->words[MT6357_LIVE_CURRENT].value, &sample->current_ua.value);
	raw = (unsigned int)sample->words[MT6357_LIVE_CAR_LOW].value |
	      ((unsigned int)sample->words[MT6357_LIVE_CAR_HIGH].value << 16);
	sample->charge_uah.error = mt6357_gauge_convert_charge(gauge, raw,
							    &sample->charge_uah.value);
	return sample->current_ua.error ?: sample->charge_uah.error;
}

static void mt6357_gauge_init_adc_sample(struct mt6357_live_adc *sample)
{
	*sample = (struct mt6357_live_adc) {};
	sample->raw.error = sample->mv.error = sample->scale_error = -ENODATA;
}

static int mt6357_gauge_live_adc(struct iio_channel *channel,
				struct mt6357_live_adc *sample)
{
	const struct iio_chan_spec *spec;
	int ret, raw, numerator = 0, denominator = 0;
	s64 mv;

	mt6357_gauge_init_adc_sample(sample);
	sample->started_ns = ktime_get_boottime_ns();
	ret = -ENODATA;
	if (!channel)
		goto done;
	spec = channel->channel;
	/* This bounded reader supports the actual unsigned, offset-free r1 ADCs. */
	ret = -EOPNOTSUPP;
	if (spec->scan_type.sign != 'u' || !spec->scan_type.realbits ||
	    spec->scan_type.realbits > 31 || iio_channel_has_info(spec, IIO_CHAN_INFO_OFFSET))
		goto done;
	ret = iio_read_channel_raw(channel, &raw);
	sample->raw.error = ret < 0 ? ret : (ret == IIO_VAL_INT ? 0 : -EOPNOTSUPP);
	ret = sample->raw.error;
	if (ret)
		goto done;
	sample->raw.value = raw;
	ret = -ERANGE;
	if (raw < 0 || (unsigned int)raw >= (1U << spec->scan_type.realbits))
		goto done;
	ret = iio_read_channel_scale(channel, &numerator, &denominator);
	sample->scale_type = ret;
	sample->scale_error = ret < 0 ? ret :
			      (ret == IIO_VAL_FRACTIONAL ? 0 : -EOPNOTSUPP);
	if (ret >= 0) {
		sample->scale_numerator = numerator;
		sample->scale_denominator = denominator;
	}
	ret = sample->scale_error;
	if (ret)
		goto done;
	ret = -ERANGE;
	if (numerator <= 0 || denominator <= 0)
		goto done;
	/* Two nonnegative signed-int operands fit the signed 64-bit product. */
	mv = div_s64((s64)raw * numerator, denominator);
	if (mv > INT_MAX / 1000)
		goto done;
	sample->mv.value = mv;
	ret = 0;
done:
	sample->mv.error = ret;
	sample->finished_ns = ktime_get_boottime_ns();
	return ret;
}

/*
 * Publish diagnostic evidence even on error, never a partial successful bundle.
 * Callers own the driver lifetime (probe now, drained in-driver work later).
 */
static int mt6357_gauge_collect_live(struct mt6357_gauge *gauge,
				    struct mt6357_live_observation *out)
{
	struct mt6357_live_observation sample = {};
	struct iio_channel *channels[] = { gauge->voltage, gauge->thermistor, gauge->reference };
	unsigned int set, channel;
	int ret, err;

	if (!out)
		return -EINVAL;
	sample.started_ns = ktime_get_boottime_ns();
	for (set = 0; set < 2; set++) {
		mt6357_gauge_init_fg(&sample.fg[set]);
		sample.temperature_decic[set].error = -ENODATA;
		for (channel = 0; channel < 3; channel++)
			mt6357_gauge_init_adc_sample(&sample.adc[set][channel]);
	}
	mutex_lock(&gauge->lock);
	sample.generation = gauge->live_generation;
	ret = -EOVERFLOW;
	if (gauge->live_sequence == U64_MAX)
		goto done;
	sample.sequence = ++gauge->live_sequence;
	ret = mt6357_gauge_live_pair(gauge, &sample.fg[0]);
	if (ret)
		goto done;
	for (set = 0; set < 2; set++) {
		for (channel = 0; channel < 3; channel++) {
			unsigned int index = set ? 2 - channel : channel;

			err = mt6357_gauge_live_adc(channels[index], &sample.adc[set][index]);
			if (!err && !index && !sample.adc[set][index].mv.value)
				err = sample.adc[set][index].mv.error = -ERANGE;
			if (!ret)
				ret = err;
		}
	}
	err = mt6357_gauge_live_pair(gauge, &sample.fg[1]);
	if (!ret)
		ret = err;
	for (set = 0; set < 2; set++) {
		/* Use only this bracket's current, never a hidden third latch. */
		err = sample.fg[set].current_ua.error ?: sample.adc[set][1].mv.error ?:
		      sample.adc[set][2].mv.error;
		if (!err)
			err = mt6357_gauge_temperature(gauge, sample.adc[set][1].mv.value,
					sample.adc[set][2].mv.value, sample.fg[set].current_ua.value,
					&sample.temperature_decic[set].value);
		sample.temperature_decic[set].error = err;
		if (!ret)
			ret = err;
		/* PRESENT is independent: successful absence remains value zero. */
		if (!ret)
			ret = sample.fg[set].present.error;
	}
done:
	sample.error = ret;
	sample.finished_ns = ktime_get_boottime_ns();
	mutex_unlock(&gauge->lock);
	*out = sample;
	return ret;
}

static void mt6357_gauge_live_diagnostic(struct mt6357_gauge *gauge)
{
	struct mt6357_live_observation sample;
	unsigned int set, channel;

	if (!IS_ENABLED(CONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS))
		return;
	mt6357_gauge_collect_live(gauge, &sample);
	dev_info(gauge->dev, "live acquisition generation=%llu sequence=%llu ns=%llu..%llu error=%d (not SOC)\n",
		 sample.generation, sample.sequence, sample.started_ns, sample.finished_ns, sample.error);
	for (set = 0; set < 2; set++) {
		const struct mt6357_live_fg *fg = &sample.fg[set];

		dev_info(gauge->dev, "live FG%u ns=%llu/%llu/%llu prepare/latch/release=%d/%d/%d current_uA=%d/%d charge_uAh=%d/%d present=%d/%d temp_deciC=%d/%d\n",
			 set, fg->started_ns, fg->ready_ns, fg->finished_ns,
			 fg->prepare_error, fg->latch_error, fg->release_error,
			 fg->current_ua.value, fg->current_ua.error, fg->charge_uah.value,
			 fg->charge_uah.error, fg->present.value, fg->present.error,
			 sample.temperature_decic[set].value, sample.temperature_decic[set].error);
		for (channel = 0; channel < MT6357_LIVE_WORDS; channel++)
			dev_info(gauge->dev, "live FG%u word%u=%04x/%d (raw/errno)\n",
				 set, channel, fg->words[channel].value, fg->words[channel].error);
		for (channel = 0; channel < 3; channel++) {
			const struct mt6357_live_adc *adc = &sample.adc[set][channel];

			dev_info(gauge->dev, "live ADC%u/%u ns=%llu..%llu raw=%d/%d scale=%d:%d/%d error=%d mV=%d/%d\n",
				 set, channel, adc->started_ns, adc->finished_ns, adc->raw.value,
				 adc->raw.error, adc->scale_type, adc->scale_numerator,
				 adc->scale_denominator, adc->scale_error, adc->mv.value, adc->mv.error);
		}
	}
}

static int mt6357_gauge_read_temperature(struct mt6357_gauge *gauge, int *temperature)
{
	int ret, voltage_mv, reference_mv, current_ua;

	if (!gauge->thermistor)
		return -ENODATA;
	ret = iio_read_channel_processed(gauge->thermistor, &voltage_mv);
	if (ret < 0)
		return ret;
	ret = mt6357_gauge_read_current(gauge, &current_ua);
	if (ret)
		return ret;
	ret = iio_read_channel_processed(gauge->reference, &reference_mv);
	if (ret < 0)
		return ret;

	return mt6357_gauge_temperature(gauge, voltage_mv, reference_mv, current_ua, temperature);
}

static int mt6357_gauge_read_present(struct mt6357_gauge *gauge, int *present)
{
	unsigned int value;
	int ret;

	guard(mutex)(&gauge->lock);

	ret = regmap_read(gauge->regmap, MT6357_BATON_ANA_CON0, &value);
	if (ret)
		return ret;
	/* Stock initializes BATON_EN. A disabled detector is not evidence. */
	if (!(value & BIT(0)))
		return -EAGAIN;
	*present = !(value & BIT(1));
	return 0;
}

static int mt6357_gauge_read_status(struct mt6357_gauge *gauge, int *status)
{
	union power_supply_propval val;
	int ret;

	if (!gauge->charger)
		return -ENODATA;
	ret = power_supply_get_property(gauge->charger, POWER_SUPPLY_PROP_STATUS, &val);
	if (ret)
		return ret;
	if (val.intval < POWER_SUPPLY_STATUS_UNKNOWN || val.intval > POWER_SUPPLY_STATUS_FULL)
		return -ERANGE;
	*status = val.intval;
	return 0;
}

static void mt6357_gauge_status_work(struct work_struct *work)
{
	struct mt6357_gauge *gauge = container_of(to_delayed_work(work),
						 struct mt6357_gauge, status_work);
	bool valid, changed;
	int status = POWER_SUPPLY_STATUS_UNKNOWN;

	/* Supplier I/O never holds the measurement or notification mutex. */
	valid = !mt6357_gauge_read_status(gauge, &status);
	mutex_lock(&gauge->status_lock);
	if (!gauge->status_active) {
		mutex_unlock(&gauge->status_lock);
		return;
	}
	changed = !gauge->status_seen || valid != gauge->status_valid ||
		  (valid && status != gauge->status);
	gauge->status_seen = true;
	gauge->status_valid = valid;
	gauge->status = status;
	/* Do not postpone an immediate supplier event queued during this read. */
	queue_delayed_work(system_power_efficient_wq, &gauge->status_work,
			   MT6357_STATUS_POLL_INTERVAL);
	mutex_unlock(&gauge->status_lock);
	if (changed)
		power_supply_changed(gauge->psy);
}

static int mt6357_gauge_status_notify(struct notifier_block *nb,
				       unsigned long event, void *data)
{
	struct mt6357_gauge *gauge = container_of(nb, struct mt6357_gauge, status_nb);

	if (event != PSY_EVENT_PROP_CHANGED || data != gauge->charger)
		return NOTIFY_DONE;
	mutex_lock(&gauge->status_lock);
	if (gauge->status_active)
		mod_delayed_work(system_power_efficient_wq, &gauge->status_work, 0);
	mutex_unlock(&gauge->status_lock);
	return NOTIFY_DONE;
}

static void mt6357_gauge_stop_status(void *data)
{
	struct mt6357_gauge *gauge = data;

	mutex_lock(&gauge->status_lock);
	gauge->status_active = false;
	mutex_unlock(&gauge->status_lock);
	/* Drain callbacks running on the supplier's workqueue before freeing us. */
	power_supply_unreg_notifier(&gauge->status_nb);
	cancel_delayed_work_sync(&gauge->status_work);
}

static int mt6357_gauge_get_property(struct power_supply *psy,
				     enum power_supply_property psp,
				     union power_supply_propval *val)
{
	struct mt6357_gauge *gauge = power_supply_get_drvdata(psy);

	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
		return mt6357_gauge_read_status(gauge, &val->intval);
	case POWER_SUPPLY_PROP_PRESENT:
		return mt6357_gauge_read_present(gauge, &val->intval);
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		return mt6357_gauge_read_current(gauge, &val->intval);
	case POWER_SUPPLY_PROP_CHARGE_COUNTER:
		return mt6357_gauge_read_charge(gauge, &val->intval);
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		return mt6357_gauge_read_voltage(gauge, &val->intval);
	case POWER_SUPPLY_PROP_TEMP:
		return mt6357_gauge_read_temperature(gauge, &val->intval);
	default:
		return -EINVAL;
	}
}

static const enum power_supply_property mt6357_gauge_properties[] = {
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_CURRENT_NOW,
	POWER_SUPPLY_PROP_CHARGE_COUNTER,
	POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_TEMP,
};

static const enum power_supply_property mt6357_gauge_status_properties[] = {
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_CURRENT_NOW,
	POWER_SUPPLY_PROP_CHARGE_COUNTER,
	POWER_SUPPLY_PROP_STATUS,
};

static const enum power_supply_property mt6357_gauge_adc_status_properties[] = {
	POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_CURRENT_NOW,
	POWER_SUPPLY_PROP_CHARGE_COUNTER,
	POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_TEMP,
	POWER_SUPPLY_PROP_STATUS,
};

/* A class iterator may retain psy after unregister, so keep its descriptor static. */
#define MT6357_GAUGE_DESC(_properties, _count) { \
	.name = "mt6357-battery", \
	.type = POWER_SUPPLY_TYPE_BATTERY, \
	.properties = (_properties), \
	.num_properties = (_count), \
	.get_property = mt6357_gauge_get_property, \
}

static const struct power_supply_desc mt6357_gauge_desc =
	MT6357_GAUGE_DESC(mt6357_gauge_properties, 3);
static const struct power_supply_desc mt6357_gauge_adc_desc =
	MT6357_GAUGE_DESC(mt6357_gauge_properties, ARRAY_SIZE(mt6357_gauge_properties));
static const struct power_supply_desc mt6357_gauge_status_desc =
	MT6357_GAUGE_DESC(mt6357_gauge_status_properties,
			  ARRAY_SIZE(mt6357_gauge_status_properties));
static const struct power_supply_desc mt6357_gauge_adc_status_desc =
	MT6357_GAUGE_DESC(mt6357_gauge_adc_status_properties,
			  ARRAY_SIZE(mt6357_gauge_adc_status_properties));

#undef MT6357_GAUGE_DESC

static int mt6357_gauge_get_channel(struct mt6357_gauge *gauge, const char *name,
				    struct iio_channel **channel)
{
	struct device *dev = gauge->dev;
	struct of_phandle_args args;
	struct platform_device *provider;
	struct iio_channel *chan;
	enum iio_chan_type type;
	int ret, index;

	/* Resolve the same named OF input as IIO, with an owned supplier ref. */
	index = of_property_match_string(dev->of_node, "io-channel-names", name);
	if (index < 0)
		return index;
	ret = of_parse_phandle_with_args(dev->of_node, "io-channels", "#io-channel-cells",
					 index, &args);
	if (ret)
		return ret;
	provider = of_find_device_by_node(args.np);
	of_node_put(args.np);
	if (!provider)
		return -EPROBE_DEFER;
	ret = -EINVAL;
	if (&provider->dev == dev)
		goto put_provider;
	/* Never wait for a supplier whose unbind may be waiting for this probe. */
	ret = -EPROBE_DEFER;
	if (!device_trylock(&provider->dev))
		goto put_provider;
	if (!device_is_bound(&provider->dev) ||
	    READ_ONCE(provider->dev.links.status) != DL_DEV_DRIVER_BOUND)
		goto unlock_provider;
	ret = -ENOMEM;
	if (!device_link_add(dev, &provider->dev, DL_FLAG_AUTOREMOVE_CONSUMER))
		goto unlock_provider;

	/*
	 * Link before lookup: neither initial metadata nor a raw parent is pinned
	 * by a retained IIO device reference alone after provider removal.
	 */
	chan = devm_iio_channel_get(dev, name);
	if (IS_ERR(chan)) {
		ret = PTR_ERR(chan);
		goto unlock_provider;
	}
	ret = -EINVAL;
	if (chan->indio_dev->dev.parent != &provider->dev)
		goto unlock_provider;
	ret = iio_get_channel_type(chan, &type);
	if (ret == -ENODEV)
		ret = -EPROBE_DEFER;
	if (!ret && type != IIO_VOLTAGE)
		ret = -EINVAL;
	if (!ret)
		*channel = chan;
unlock_provider:
	device_unlock(&provider->dev);
put_provider:
	put_device(&provider->dev);
	return ret;
}

static int mt6357_gauge_init_adc(struct mt6357_gauge *gauge)
{
	const char *table_name = "mediatek,thermistor-resistance-table";
	struct device *dev = gauge->dev;
	unsigned int i;
	int ret, count;

	if (!device_property_present(dev, "io-channels"))
		return 0;
	ret = mt6357_gauge_get_channel(gauge, "battery-voltage", &gauge->voltage);
	if (ret)
		return ret;
	ret = mt6357_gauge_get_channel(gauge, "battery-thermistor", &gauge->thermistor);
	if (ret)
		return ret;
	ret = mt6357_gauge_get_channel(gauge, "thermistor-reference", &gauge->reference);
	if (ret)
		return ret;
	ret = device_property_read_u32(dev, "mediatek,thermistor-pullup-ohms", &gauge->pullup_ohms);
	if (ret)
		return ret;
	ret = device_property_read_u32(dev, "mediatek,thermistor-series-micro-ohms",
				       &gauge->series_uohms);
	if (ret)
		return ret;
	if (!gauge->pullup_ohms)
		return -EINVAL;

	count = device_property_count_u32(dev, table_name);
	if (count < 0)
		return count;
	if (count < 4 || count > MT6357_THERMISTOR_MAX_POINTS * 2 || count % 2)
		return -EINVAL;
	gauge->num_points = count / 2;
	gauge->table = devm_kmalloc_array(dev, gauge->num_points, sizeof(*gauge->table),
					 GFP_KERNEL);
	if (!gauge->table)
		return -ENOMEM;
	ret = device_property_read_u32_array(dev, table_name, (u32 *)gauge->table, count);
	if (ret)
		return ret;
	for (i = 0; i < gauge->num_points; i++) {
		if (!gauge->table[i].resistance || gauge->table[i].resistance > INT_MAX)
			return -EINVAL;
		if (i && (gauge->table[i].temperature_mc <= gauge->table[i - 1].temperature_mc ||
			  gauge->table[i].resistance >= gauge->table[i - 1].resistance))
			return -EINVAL;
	}

	gauge->desc = &mt6357_gauge_adc_desc;
	return 0;
}

static int mt6357_gauge_init_status(struct mt6357_gauge *gauge)
{
	struct device *dev = gauge->dev;
	struct platform_device *provider;
	struct device_node *node;
	struct power_supply *charger;
	int ret;

	if (!device_property_present(dev, "power-supplies"))
		return 0;
	/* This OF binding describes exactly one charger with no arguments. */
	ret = of_count_phandle_with_args(dev->of_node, "power-supplies", NULL);
	if (ret != 1)
		return ret < 0 ? ret : -EINVAL;
	node = of_parse_phandle(dev->of_node, "power-supplies", 0);
	if (!node)
		return -EINVAL;
	provider = of_find_device_by_node(node);
	of_node_put(node);
	if (!provider)
		return -EPROBE_DEFER;
	ret = -EINVAL;
	if (&provider->dev == dev)
		goto put_provider;
	ret = -EPROBE_DEFER;
	if (!device_trylock(&provider->dev))
		goto put_provider;
	if (!device_is_bound(&provider->dev) ||
	    READ_ONCE(provider->dev.links.status) != DL_DEV_DRIVER_BOUND)
		goto unlock_provider;
	ret = -ENOMEM;
	if (!device_link_add(dev, &provider->dev, DL_FLAG_AUTOREMOVE_CONSUMER))
		goto unlock_provider;
	/* Retain the supplier before looking up or using its power supply. */
	charger = devm_power_supply_get_by_parent(dev, &provider->dev);
	if (IS_ERR(charger)) {
		ret = PTR_ERR(charger);
		goto unlock_provider;
	}
	ret = -EPROBE_DEFER;
	if (!charger)
		goto unlock_provider;
	gauge->charger = charger;
	ret = 0;
unlock_provider:
	device_unlock(&provider->dev);
put_provider:
	put_device(&provider->dev);
	if (ret)
		return ret;
	ret = devm_mutex_init(dev, &gauge->status_lock);
	if (ret)
		return ret;
	INIT_DELAYED_WORK(&gauge->status_work, mt6357_gauge_status_work);
	gauge->status_nb.notifier_call = mt6357_gauge_status_notify;
	gauge->desc = gauge->desc == &mt6357_gauge_adc_desc ?
		&mt6357_gauge_adc_status_desc : &mt6357_gauge_status_desc;
	return 0;
}

static int mt6357_gauge_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mt6397_chip *pmic = dev_get_drvdata(dev->parent);
	struct power_supply_config config = {};
	struct mt6357_gauge *gauge;
	struct power_supply *psy;
	int ret, full_scale;

	gauge = devm_kzalloc(dev, sizeof(*gauge), GFP_KERNEL);
	if (!gauge)
		return -ENOMEM;
	gauge->dev = dev;
	if (!pmic || !pmic->regmap)
		return dev_err_probe(dev, -ENODEV, "Missing PMIC regmap\n");
	/* The MFD borrows this map from the SoC's PMIC wrapper. */
	gauge->regmap = pmic->regmap;

	ret = device_property_read_u32(dev, "shunt-resistor-micro-ohms", &gauge->shunt_uohms);
	if (ret)
		return dev_err_probe(dev, ret, "Missing current shunt resistance\n");
	gauge->gain_permille = 1000;
	if (device_property_present(dev, "mediatek,current-gain-permille")) {
		ret = device_property_read_u32(dev, "mediatek,current-gain-permille",
					       &gauge->gain_permille);
		if (ret)
			return dev_err_probe(dev, ret, "Invalid current gain\n");
	}
	if (!gauge->shunt_uohms || !gauge->gain_permille)
		return dev_err_probe(dev, -EINVAL, "Current calibration must be nonzero\n");
	ret = mt6357_gauge_convert(gauge, 0x7fff, &full_scale);
	if (ret)
		return dev_err_probe(dev, ret, "Current calibration exceeds reported range\n");
	if (!full_scale)
		return dev_err_probe(dev, -EINVAL, "Current calibration loses all resolution\n");

	gauge->desc = &mt6357_gauge_desc;
	ret = mt6357_gauge_init_adc(gauge);
	if (ret)
		return dev_err_probe(dev, ret, "Invalid battery ADC inputs\n");
	ret = devm_mutex_init(dev, &gauge->lock);
	if (ret)
		return ret;
	ret = mt6357_gauge_init_status(gauge);
	if (ret)
		return dev_err_probe(dev, ret, "Invalid battery charger supplier\n");
	gauge->needs_release = true;
	gauge->live_generation = atomic64_inc_return(&mt6357_live_generation);
	mt6357_gauge_live_diagnostic(gauge);
	config.drv_data = gauge;
	config.fwnode = dev_fwnode(dev);
	psy = devm_power_supply_register(dev, gauge->desc, &config);
	if (IS_ERR(psy))
		return PTR_ERR(psy);
	if (!gauge->charger)
		return 0;
	gauge->psy = psy;
	ret = power_supply_reg_notifier(&gauge->status_nb);
	if (ret)
		return ret;
	/* Added after registration, so work stops before the supply is removed. */
	ret = devm_add_action_or_reset(dev, mt6357_gauge_stop_status, gauge);
	if (ret)
		return ret;
	mutex_lock(&gauge->status_lock);
	gauge->status_active = true;
	mod_delayed_work(system_power_efficient_wq, &gauge->status_work, 0);
	mutex_unlock(&gauge->status_lock);
	return 0;
}

static const struct of_device_id mt6357_gauge_of_match[] = {
	{ .compatible = "mediatek,mt6357-gauge" },
	{}
};
MODULE_DEVICE_TABLE(of, mt6357_gauge_of_match);

static struct platform_driver mt6357_gauge_driver = {
	.probe = mt6357_gauge_probe,
	.driver = {
		.name = "mt6357-gauge",
		.of_match_table = mt6357_gauge_of_match,
	},
};
module_platform_driver(mt6357_gauge_driver);

MODULE_DESCRIPTION("MediaTek MT6357 battery measurements");
MODULE_LICENSE("GPL");
