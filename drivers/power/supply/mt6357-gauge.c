// SPDX-License-Identifier: GPL-2.0-only
/* MT6357 battery current sensing. */

#include <linux/bitfield.h>
#include <linux/cleanup.h>
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
#define MT6357_FG_POLL_US	100
#define MT6357_FG_TIMEOUT_US	20000

struct mt6357_gauge {
	struct device *dev;
	struct regmap *regmap;
	struct mutex lock;
	u32 shunt_uohms;
	u32 gain_permille;
	bool needs_release;
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

static int mt6357_gauge_read_current(struct mt6357_gauge *gauge, int *current_ua)
{
	unsigned int state, raw;
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
		ret = regmap_read(gauge->regmap, MT6357_FGADC_CUR_CON0, &raw);

	release_ret = mt6357_gauge_release(gauge);
	if (release_ret)
		dev_err_ratelimited(gauge->dev, "Failed to release current latch: %d\n",
				    release_ret);
	if (ret)
		return ret;
	if (release_ret)
		return release_ret;

	/* Publish only a sample whose complete transaction succeeded. */
	return mt6357_gauge_convert(gauge, raw, current_ua);
}

static int mt6357_gauge_get_property(struct power_supply *psy,
				     enum power_supply_property psp,
				     union power_supply_propval *val)
{
	struct mt6357_gauge *gauge = power_supply_get_drvdata(psy);

	if (psp != POWER_SUPPLY_PROP_CURRENT_NOW)
		return -EINVAL;

	return mt6357_gauge_read_current(gauge, &val->intval);
}

static const enum power_supply_property mt6357_gauge_properties[] = {
	POWER_SUPPLY_PROP_CURRENT_NOW,
};

static const struct power_supply_desc mt6357_gauge_desc = {
	.name = "mt6357-battery",
	.type = POWER_SUPPLY_TYPE_BATTERY,
	.properties = mt6357_gauge_properties,
	.num_properties = ARRAY_SIZE(mt6357_gauge_properties),
	.get_property = mt6357_gauge_get_property,
};

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

	ret = devm_mutex_init(dev, &gauge->lock);
	if (ret)
		return ret;
	gauge->needs_release = true;
	config.drv_data = gauge;
	config.fwnode = dev_fwnode(dev);
	psy = devm_power_supply_register(dev, &mt6357_gauge_desc, &config);
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

MODULE_DESCRIPTION("MediaTek MT6357 battery current sensing");
MODULE_LICENSE("GPL");
