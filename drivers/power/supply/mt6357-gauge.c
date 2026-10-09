// SPDX-License-Identifier: GPL-2.0-only
/* MT6357 battery current, charge counter, voltage and thermistor sensing. */

#include <linux/bitfield.h>
#include <linux/cleanup.h>
#include <linux/iio/consumer.h>
#include <linux/math64.h>
#include <linux/mfd/mt6357/registers.h>
#include <linux/mfd/mt6397/core.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/property.h>
#include <linux/regmap.h>

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

struct mt6357_thermistor_point {
	s32 temperature_mc;
	u32 resistance;
};

struct mt6357_gauge {
	struct device *dev;
	struct regmap *regmap;
	struct mutex lock;
	u32 shunt_uohms;
	u32 gain_permille;
	bool needs_release;
	struct power_supply_desc desc;
	struct iio_channel *voltage;
	struct iio_channel *thermistor;
	struct iio_channel *reference;
	struct mt6357_thermistor_point *table;
	unsigned int num_points;
	u32 pullup_ohms;
	u32 series_uohms;
};

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

static int mt6357_gauge_read_raw(struct mt6357_gauge *gauge, bool counter,
				  unsigned int *raw)
{
	unsigned int state, low, high = 0;
	int ret, release_ret;

	guard(mutex)(&gauge->lock);

	/* Do not report retained samples from a stopped measurement engine. */
	ret = regmap_read(gauge->regmap, MT6357_FGADC_CON0, &state);
	if (ret)
		return ret;
	if (!(state & MT6357_FG_ON))
		return -EAGAIN;
	ret = regmap_read(gauge->regmap, MT6357_BM_TOP_CKPDN_CON0, &state);
	if (ret)
		return ret;
	if (state & MT6357_FG_CLOCK_PD)
		return -EAGAIN;

	/* Clear inherited state, or recover a previous incomplete release. */
	if (gauge->needs_release) {
		ret = mt6357_gauge_release(gauge);
		if (ret)
			return ret;
	}

	gauge->needs_release = true;
	ret = regmap_update_bits(gauge->regmap, MT6357_FGADC_CON1,
				 MT6357_FG_READ_PRE, MT6357_FG_READ_PRE);
	if (!ret)
		ret = regmap_read_poll_timeout(gauge->regmap, MT6357_FGADC_CON1, state,
					       state & MT6357_FG_LATCH_READY,
					       MT6357_FG_POLL_US, MT6357_FG_TIMEOUT_US);
	if (!ret)
		ret = regmap_read(gauge->regmap,
				  counter ? MT6357_FGADC_CAR_CON0 : MT6357_FGADC_CUR_CON0,
				  &low);
	if (!ret && counter)
		ret = regmap_read(gauge->regmap, MT6357_FGADC_CAR_CON1, &high);

	release_ret = mt6357_gauge_release(gauge);
	if (release_ret)
		dev_err_ratelimited(gauge->dev, "Failed to release gauge latch: %d\n",
				    release_ret);
	if (ret)
		return ret;
	if (release_ret)
		return release_ret;

	/* Publish only a sample whose complete transaction succeeded. */
	*raw = (low & 0xffff) | (high << 16);
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

static int mt6357_gauge_get_property(struct power_supply *psy,
				     enum power_supply_property psp,
				     union power_supply_propval *val)
{
	struct mt6357_gauge *gauge = power_supply_get_drvdata(psy);

	switch (psp) {
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

static const struct power_supply_desc mt6357_gauge_desc = {
	.name = "mt6357-battery",
	.type = POWER_SUPPLY_TYPE_BATTERY,
	.properties = mt6357_gauge_properties,
	/* The ADC properties are added only when all their inputs are supplied. */
	.num_properties = 3,
	.get_property = mt6357_gauge_get_property,
};

static int mt6357_gauge_get_channel(struct device *dev, const char *name,
				   struct iio_channel **channel)
{
	struct iio_channel *chan;
	enum iio_chan_type type;
	int ret;

	chan = devm_iio_channel_get(dev, name);
	if (IS_ERR(chan))
		return PTR_ERR(chan);
	ret = iio_get_channel_type(chan, &type);
	if (ret)
		return ret;
	if (type != IIO_VOLTAGE)
		return -EINVAL;
	*channel = chan;
	return 0;
}

static int mt6357_gauge_init_adc(struct mt6357_gauge *gauge)
{
	const char *table_name = "mediatek,thermistor-resistance-table";
	struct device *dev = gauge->dev;
	unsigned int i;
	int ret, count;

	if (!device_property_present(dev, "io-channels"))
		return 0;
	ret = mt6357_gauge_get_channel(dev, "battery-voltage", &gauge->voltage);
	if (ret)
		return ret;
	ret = mt6357_gauge_get_channel(dev, "battery-thermistor", &gauge->thermistor);
	if (ret)
		return ret;
	ret = mt6357_gauge_get_channel(dev, "thermistor-reference", &gauge->reference);
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

	gauge->desc.num_properties = ARRAY_SIZE(mt6357_gauge_properties);
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

	gauge->desc = mt6357_gauge_desc;
	ret = mt6357_gauge_init_adc(gauge);
	if (ret)
		return dev_err_probe(dev, ret, "Invalid battery ADC inputs\n");
	ret = devm_mutex_init(dev, &gauge->lock);
	if (ret)
		return ret;
	gauge->needs_release = true;
	config.drv_data = gauge;
	config.fwnode = dev_fwnode(dev);
	psy = devm_power_supply_register(dev, &gauge->desc, &config);
	return PTR_ERR_OR_ZERO(psy);
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
