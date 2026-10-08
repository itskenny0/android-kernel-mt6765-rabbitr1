// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (C) 2022 Richtek Technology Corp.
 *
 * Author: ChiaEn Wu <chiaen_wu@richtek.com>
 */

#include <linux/bitfield.h>
#include <linux/bits.h>
#include <linux/cleanup.h>
#include <linux/delay.h>
#include <linux/devm-helpers.h>
#include <linux/gpio/consumer.h>
#include <linux/iio/consumer.h>
#include <linux/iio/iio.h>
#include <linux/init.h>
#include <linux/interrupt.h>
#include <linux/kernel.h>
#include <linux/linear_range.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/regmap.h>
#include <linux/regulator/driver.h>
#include <linux/units.h>
#include <linux/workqueue.h>

#include <dt-bindings/iio/adc/mediatek,mt6370_adc.h>

#define MT6370_REG_DEV_INFO		0x100
#define MT6370_REG_HIDDEN_PASSCODE	0x107
#define MT6370_REG_CHG_CTRL1		0x111
#define MT6370_REG_CHG_CTRL2		0x112
#define MT6370_REG_CHG_CTRL3		0x113
#define MT6370_REG_CHG_CTRL4		0x114
#define MT6370_REG_CHG_CTRL5		0x115
#define MT6370_REG_CHG_CTRL6		0x116
#define MT6370_REG_CHG_CTRL7		0x117
#define MT6370_REG_CHG_CTRL8		0x118
#define MT6370_REG_CHG_CTRL9		0x119
#define MT6370_REG_CHG_CTRL10		0x11A
#define MT6370_REG_DEVICE_TYPE		0x122
#define MT6370_REG_USB_STATUS1		0x127
#define MT6370_REG_CHG_HIDDEN_CTRL7	0x136
#define MT6370_REG_CHG_STAT		0x14A
#define MT6370_REG_FLED_EN		0x17E
#define MT6370_REG_CHG_STAT1		0X1D0
#define MT6370_REG_OVPCTRL_STAT		0x1D8

#define MT6370_VOBST_MASK		GENMASK(7, 2)
#define MT6370_OTG_PIN_EN_MASK		BIT(1)
#define MT6370_OPA_MODE_MASK		BIT(0)
#define MT6370_OTG_OC_MASK		GENMASK(2, 0)

#define MT6370_MIVR_IBUS_THRESHOLD_UA	100000
#define MT6370_VENID_MASK		GENMASK(7, 4)
#define MT6370_VSYS_SHORT_MASK		GENMASK(6, 5)
#define MT6370_VSYS_SHORT_ENABLE		BIT(6)

enum mt6370_chg_reg_field {
	/* MT6370_REG_CHG_CTRL1 */
	F_FORCE_SLEEP,
	/* MT6370_REG_CHG_CTRL2 */
	F_IINLMTSEL, F_CFO_EN, F_CHG_EN,
	/* MT6370_REG_CHG_CTRL3 */
	F_IAICR, F_AICR_EN, F_ILIM_EN,
	/* MT6370_REG_CHG_CTRL4 */
	F_VOREG,
	/* MT6370_REG_CHG_CTRL6 */
	F_VMIVR,
	/* MT6370_REG_CHG_CTRL7 */
	F_ICHG,
	/* MT6370_REG_CHG_CTRL8 */
	F_IPREC,
	/* MT6370_REG_CHG_CTRL9 */
	F_IEOC,
	/* MT6370_REG_DEVICE_TYPE */
	F_USBCHGEN,
	/* MT6370_REG_USB_STATUS1 */
	F_USB_STAT, F_CHGDET,
	/* MT6370_REG_CHG_STAT */
	F_CHG_STAT, F_BOOST_STAT, F_VBAT_LVL,
	/* MT6370_REG_FLED_EN */
	F_FL_STROBE,
	/* MT6370_REG_CHG_STAT1 */
	F_CHG_MIVR_STAT,
	/* MT6370_REG_OVPCTRL_STAT */
	F_UVP_D_STAT,
	F_MAX
};

enum mt6370_irq {
	MT6370_IRQ_ATTACH_I = 0,
	MT6370_IRQ_UVP_D_EVT,
	MT6370_IRQ_MIVR,
	MT6370_IRQ_MAX
};

struct mt6370_priv {
	struct device *dev;
	struct iio_channel *iio_ibus;
	struct mutex attach_lock;
	struct mutex ichg_lock;
	struct mutex psy_lock;
	struct power_supply *psy;
	struct power_supply_desc psy_desc;
	struct regmap *regmap;
	struct regmap_field *rmap_fields[F_MAX];
	struct regulator_dev *rdev;
	struct workqueue_struct *wq;
	struct work_struct bc12_work;
	struct delayed_work mivr_dwork;
	unsigned int irq_nums[MT6370_IRQ_MAX];
	unsigned int num_irqs;
	bool stopping;
	int attach;
	int psy_usb_type;
	bool pwr_rdy;
	bool ichg_workaround;
	unsigned int ichg_min;
	unsigned int ichg_request;
	bool ichg_valid;
	bool input_suspended;
	bool mivr_irq_paused;
	unsigned int mivr_request;
};

enum mt6370_usb_status {
	MT6370_USB_STAT_NO_VBUS = 0,
	MT6370_USB_STAT_VBUS_FLOW_IS_UNDER_GOING,
	MT6370_USB_STAT_SDP,
	MT6370_USB_STAT_SDP_NSTD,
	MT6370_USB_STAT_DCP,
	MT6370_USB_STAT_CDP,
	MT6370_USB_STAT_MAX
};

struct mt6370_chg_field {
	const char *name;
	const struct linear_range *range;
	struct reg_field field;
};

enum {
	MT6370_RANGE_F_IAICR = 0,
	MT6370_RANGE_F_VOREG,
	MT6370_RANGE_F_VMIVR,
	MT6370_RANGE_F_ICHG,
	MT6370_RANGE_F_IPREC,
	MT6370_RANGE_F_IEOC,
	MT6370_RANGE_F_MAX
};

static const struct linear_range mt6370_chg_ranges[MT6370_RANGE_F_MAX] = {
	LINEAR_RANGE_IDX(MT6370_RANGE_F_IAICR, 100000, 0x0, 0x3F, 50000),
	LINEAR_RANGE_IDX(MT6370_RANGE_F_VOREG, 3900000, 0x0, 0x51, 10000),
	LINEAR_RANGE_IDX(MT6370_RANGE_F_VMIVR, 3900000, 0x0, 0x5F, 100000),
	LINEAR_RANGE_IDX(MT6370_RANGE_F_ICHG, 500000, 0x04, 0x31, 100000),
	LINEAR_RANGE_IDX(MT6370_RANGE_F_IPREC, 100000, 0x0, 0x0F, 50000),
	LINEAR_RANGE_IDX(MT6370_RANGE_F_IEOC, 100000, 0x0, 0x0F, 50000),
};

#define MT6370_CHG_FIELD(_fd, _reg, _lsb, _msb)				\
[_fd] = {								\
	.name = #_fd,							\
	.range = NULL,							\
	.field = REG_FIELD(_reg, _lsb, _msb),				\
}

#define MT6370_CHG_FIELD_RANGE(_fd, _reg, _lsb, _msb)			\
[_fd] = {								\
	.name = #_fd,							\
	.range = &mt6370_chg_ranges[MT6370_RANGE_##_fd],		\
	.field = REG_FIELD(_reg, _lsb, _msb),				\
}

static const struct mt6370_chg_field mt6370_chg_fields[F_MAX] = {
	MT6370_CHG_FIELD(F_FORCE_SLEEP, MT6370_REG_CHG_CTRL1, 3, 3),
	MT6370_CHG_FIELD(F_IINLMTSEL, MT6370_REG_CHG_CTRL2, 2, 3),
	MT6370_CHG_FIELD(F_CFO_EN, MT6370_REG_CHG_CTRL2, 1, 1),
	MT6370_CHG_FIELD(F_CHG_EN, MT6370_REG_CHG_CTRL2, 0, 0),
	MT6370_CHG_FIELD_RANGE(F_IAICR, MT6370_REG_CHG_CTRL3, 2, 7),
	MT6370_CHG_FIELD(F_AICR_EN, MT6370_REG_CHG_CTRL3, 1, 1),
	MT6370_CHG_FIELD(F_ILIM_EN, MT6370_REG_CHG_CTRL3, 0, 0),
	MT6370_CHG_FIELD_RANGE(F_VOREG, MT6370_REG_CHG_CTRL4, 1, 7),
	MT6370_CHG_FIELD_RANGE(F_VMIVR, MT6370_REG_CHG_CTRL6, 1, 7),
	MT6370_CHG_FIELD_RANGE(F_ICHG, MT6370_REG_CHG_CTRL7, 2, 7),
	MT6370_CHG_FIELD_RANGE(F_IPREC, MT6370_REG_CHG_CTRL8, 0, 3),
	MT6370_CHG_FIELD_RANGE(F_IEOC, MT6370_REG_CHG_CTRL9, 4, 7),
	MT6370_CHG_FIELD(F_USBCHGEN, MT6370_REG_DEVICE_TYPE, 7, 7),
	MT6370_CHG_FIELD(F_USB_STAT, MT6370_REG_USB_STATUS1, 4, 6),
	MT6370_CHG_FIELD(F_CHGDET, MT6370_REG_USB_STATUS1, 3, 3),
	MT6370_CHG_FIELD(F_CHG_STAT, MT6370_REG_CHG_STAT, 6, 7),
	MT6370_CHG_FIELD(F_BOOST_STAT, MT6370_REG_CHG_STAT, 3, 3),
	MT6370_CHG_FIELD(F_VBAT_LVL, MT6370_REG_CHG_STAT, 5, 5),
	MT6370_CHG_FIELD(F_FL_STROBE, MT6370_REG_FLED_EN, 2, 2),
	MT6370_CHG_FIELD(F_CHG_MIVR_STAT, MT6370_REG_CHG_STAT1, 6, 6),
	MT6370_CHG_FIELD(F_UVP_D_STAT, MT6370_REG_OVPCTRL_STAT, 4, 4),
};

static inline int mt6370_chg_field_get(struct mt6370_priv *priv,
				       enum mt6370_chg_reg_field fd,
				       unsigned int *val)
{
	int ret;
	unsigned int reg_val;

	ret = regmap_field_read(priv->rmap_fields[fd], &reg_val);
	if (ret)
		return ret;

	if (mt6370_chg_fields[fd].range)
		return linear_range_get_value(mt6370_chg_fields[fd].range,
					       reg_val, val);

	*val = reg_val;
	return 0;
}

static inline int mt6370_chg_field_set(struct mt6370_priv *priv,
				       enum mt6370_chg_reg_field fd,
				       unsigned int val)
{
	int ret;
	bool f;
	const struct linear_range *r;

	if (mt6370_chg_fields[fd].range) {
		r = mt6370_chg_fields[fd].range;

		if (fd == F_VMIVR) {
			ret = linear_range_get_selector_high(r, val, &val, &f);
			if (ret)
				val = r->max_sel;
		} else {
			linear_range_get_selector_within(r, val, &val);
		}
	}

	return regmap_field_write(priv->rmap_fields[fd], val);
}

static int mt6370_chg_init_ichg(struct mt6370_priv *priv)
{
	unsigned int info;
	int ret;

	priv->ichg_min = 900000;
	priv->ichg_workaround = false;
	ret = regmap_read(priv->regmap, MT6370_REG_DEV_INFO, &info);
	if (ret)
		return ret;

	switch (FIELD_GET(MT6370_VENID_MASK, info)) {
	case 0x8: /* RT5081 */
	case 0xe: /* MT6370 */
		priv->ichg_min = 500000;
		priv->ichg_workaround = true;
		break;
	case 0xa: /* RT5081A */
	case 0xf: /* MT6371 */
	case 0x9: /* MT6372P */
	case 0xb: /* MT6372CP */
		/* Keep the previous minimum until low-current behavior is verified. */
		break;
	default:
		return -ENODEV;
	}

	return 0;
}

/* Caller holds ichg_lock. Also used after a failed current transition. */
static int mt6370_chg_stop(struct mt6370_priv *priv)
{
	const struct linear_range *range = &mt6370_chg_ranges[MT6370_RANGE_F_ICHG];
	unsigned int enabled, selector, ramp_us;
	int ret, err;

	lockdep_assert_held(&priv->ichg_lock);
	ret = mt6370_chg_field_get(priv, F_CHG_EN, &enabled);
	if (!ret && !enabled)
		return 0;

	/* Read actual hardware state; firmware and failed writes can invalidate a cache. */
	err = regmap_field_read(priv->rmap_fields[F_ICHG], &selector);
	if (err) {
		if (!ret)
			ret = err;
		goto disable;
	}
	if (selector > range->max_sel) {
		if (!ret)
			ret = -ERANGE;
		goto disable;
	}
	if (selector > range->min_sel) {
		/* Stock VSYS overshoot workaround: 2 ms per 50 mA down to 500 mA. */
		ramp_us = (selector - range->min_sel) * range->step / 50000 * 2000;
		err = mt6370_chg_field_set(priv, F_ICHG, range->min);
		if (!ret)
			ret = err;
		/* A write reporting failure may still have started the ramp. */
		usleep_range(ramp_us, ramp_us + 1000);
	}
disable:
	/* Even failed reads or ramp writes must not prevent an attempt to stop. */
	err = mt6370_chg_field_set(priv, F_CHG_EN, 0);
	if (err)
		dev_err(priv->dev, "Failed to inhibit charging: %d\n", err);
	if (!ret)
		ret = err;
	if (ret)
		priv->ichg_valid = false;

	return ret;
}

/* Caller holds ichg_lock; the cached value is a request, not hardware readback. */
static int mt6370_chg_program_ichg(struct mt6370_priv *priv, unsigned int ua)
{
	static const u8 passcode[] = { 0x96, 0x69, 0xc3, 0x3c };
	unsigned int protection;
	int ret, lock_ret, stop_ret;

	lockdep_assert_held(&priv->ichg_lock);
	if (ua < priv->ichg_min ||
	    ua > linear_range_get_max_value(&mt6370_chg_ranges[MT6370_RANGE_F_ICHG]))
		return -ERANGE;

	priv->ichg_valid = false;
	if (!priv->ichg_workaround) {
		ret = mt6370_chg_field_set(priv, F_ICHG, ua);
		goto complete;
	}

	/* Establish a closed gate even if firmware or a failed write left it open. */
	ret = regmap_write(priv->regmap, MT6370_REG_HIDDEN_PASSCODE, 0);
	if (ret)
		goto lock;
	ret = regmap_bulk_write(priv->regmap, MT6370_REG_HIDDEN_PASSCODE,
				passcode, ARRAY_SIZE(passcode));
	if (ret)
		goto lock;

	/* Set the target state every time; a cached current can be stale after I/O errors. */
	protection = ua < 900000 ? 0 : MT6370_VSYS_SHORT_ENABLE;
	ret = regmap_update_bits(priv->regmap, MT6370_REG_CHG_HIDDEN_CTRL7,
				 MT6370_VSYS_SHORT_MASK, protection);
lock:
	/* Attempt to close the gate even after a partial passcode write. */
	lock_ret = regmap_write(priv->regmap, MT6370_REG_HIDDEN_PASSCODE, 0);
	if (lock_ret)
		dev_err(priv->dev, "Failed to close hidden register gate: %d\n", lock_ret);
	if (!ret)
		ret = lock_ret;
	if (!ret)
		ret = mt6370_chg_field_set(priv, F_ICHG, ua);
complete:
	if (!ret) {
		priv->ichg_request = ua;
		priv->ichg_valid = true;
		return 0;
	}
	if (!priv->ichg_workaround)
		return ret;

	/* A failed transition may leave current and protection settings mismatched. */
	stop_ret = mt6370_chg_stop(priv);
	if (stop_ret)
		dev_err(priv->dev, "Failed to disable charging after current error: %d\n",
			stop_ret);

	return ret;
}

static int mt6370_chg_set_ichg(struct mt6370_priv *priv, unsigned int ua)
{
	guard(mutex)(&priv->ichg_lock);

	return mt6370_chg_program_ichg(priv, ua);
}

static int mt6370_chg_set_behaviour(struct mt6370_priv *priv, int behaviour)
{
	unsigned int asleep;
	int ret, stop_ret;

	if (behaviour != POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO &&
	    behaviour != POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE)
		return -EINVAL;
	if (!priv->ichg_workaround)
		return -EOPNOTSUPP;

	guard(mutex)(&priv->ichg_lock);
	if (behaviour == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE)
		return mt6370_chg_stop(priv);
	if (!priv->ichg_valid)
		return -EAGAIN;
	if (priv->input_suspended)
		return -EAGAIN;
	ret = mt6370_chg_field_get(priv, F_FORCE_SLEEP, &asleep);
	if (ret || asleep) {
		if (ret)
			priv->ichg_valid = false;
		else
			priv->input_suspended = true;
		stop_ret = mt6370_chg_stop(priv);
		if (stop_ret)
			dev_err(priv->dev, "Failed to inhibit with unavailable input: %d\n",
				stop_ret);
		return ret ? ret : -EAGAIN;
	}

	/* Restore the last successful request and its protection before enabling. */
	ret = mt6370_chg_program_ichg(priv, priv->ichg_request);
	if (ret)
		return ret;
	ret = mt6370_chg_field_set(priv, F_CHG_EN, 1);
	if (!ret)
		return 0;

	/* The failed enable write may have reached the device. */
	priv->ichg_valid = false;
	stop_ret = mt6370_chg_stop(priv);
	if (stop_ret)
		dev_err(priv->dev, "Failed to inhibit after enable error: %d\n", stop_ret);
	return ret;
}

static int mt6370_chg_get_behaviour(struct mt6370_priv *priv, int *behaviour)
{
	unsigned int enabled;
	int ret;

	if (!priv->ichg_workaround)
		return -EOPNOTSUPP;
	guard(mutex)(&priv->ichg_lock);
	ret = mt6370_chg_field_get(priv, F_CHG_EN, &enabled);
	if (ret)
		return ret;
	*behaviour = enabled ? POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO :
			      POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE;
	return 0;
}

static void mt6370_chg_cancel_mivr(struct mt6370_priv *priv)
{
	if (cancel_delayed_work_sync(&priv->mivr_dwork)) {
		/* Balance the IRQ mask and wake reference acquired by the handler. */
		enable_irq(priv->irq_nums[MT6370_IRQ_MIVR]);
		pm_relax(priv->dev);
	}
}

/* Caller serializes power-supply requests, including IRQ registration. */
static void mt6370_chg_pause_mivr(struct mt6370_priv *priv)
{
	if (priv->num_irqs <= MT6370_IRQ_MIVR || priv->mivr_irq_paused)
		return;

	/* This handler never enters the power-supply API. */
	disable_irq(priv->irq_nums[MT6370_IRQ_MIVR]);
	priv->mivr_irq_paused = true;
	mt6370_chg_cancel_mivr(priv);
}

static int mt6370_chg_suspend_input(struct mt6370_priv *priv)
{
	int ret, err;

	priv->input_suspended = true;
	/* Withdraw input before waiting for work or the charge-current ramp. */
	ret = mt6370_chg_field_set(priv, F_FORCE_SLEEP, 1);
	mt6370_chg_pause_mivr(priv);

	guard(mutex)(&priv->ichg_lock);
	/* Failed isolation must not prevent an attempt to inhibit charging. */
	err = mt6370_chg_stop(priv);
	if (!ret)
		ret = err;
	err = mt6370_chg_field_set(priv, F_VMIVR,
		linear_range_get_max_value(&mt6370_chg_ranges[MT6370_RANGE_F_VMIVR]));
	if (!ret)
		ret = err;
	if (ret)
		dev_err(priv->dev, "Failed to suspend charger input: %d\n", ret);
	return ret;
}

static int mt6370_chg_set_input(struct mt6370_priv *priv, unsigned int ua)
{
	unsigned int asleep;
	int ret, err;

	ret = mt6370_chg_field_get(priv, F_FORCE_SLEEP, &asleep);
	if (ret)
		goto suspend;
	if (asleep)
		priv->input_suspended = true;
	if (priv->input_suspended) {
		/* Keep work paused and charging off throughout input-path recovery. */
		ret = mt6370_chg_suspend_input(priv);
		if (ret)
			return ret;
	}

	ret = mt6370_chg_field_set(priv, F_IAICR, ua);
	if (ret)
		goto suspend;
	if (!priv->input_suspended)
		return 0;

	ret = mt6370_chg_field_set(priv, F_AICR_EN, 1);
	if (ret)
		goto suspend;
	ret = mt6370_chg_field_set(priv, F_IINLMTSEL, 2);
	if (ret)
		goto suspend;
	ret = mt6370_chg_field_set(priv, F_VMIVR, priv->mivr_request);
	if (ret)
		goto suspend;
	ret = mt6370_chg_field_set(priv, F_FORCE_SLEEP, 0);
	if (ret)
		goto suspend;

	priv->input_suspended = false;
	if (priv->mivr_irq_paused) {
		priv->mivr_irq_paused = false;
		enable_irq(priv->irq_nums[MT6370_IRQ_MIVR]);
	}
	return 0;

suspend:
	err = mt6370_chg_suspend_input(priv);
	if (err)
		dev_err(priv->dev, "Failed to suspend input after limit error: %d\n", err);
	return ret;
}

static int mt6370_chg_get_input(struct mt6370_priv *priv, unsigned int *ua)
{
	unsigned int asleep;
	int ret;

	ret = mt6370_chg_field_get(priv, F_FORCE_SLEEP, &asleep);
	if (ret)
		return ret;
	if (asleep) {
		*ua = 0;
		return 0;
	}
	return mt6370_chg_field_get(priv, F_IAICR, ua);
}

static int mt6370_chg_set_mivr(struct mt6370_priv *priv, unsigned int uv)
{
	unsigned int selector;
	bool found;
	int ret;
	const struct linear_range *range = &mt6370_chg_ranges[MT6370_RANGE_F_VMIVR];

	ret = linear_range_get_selector_high(range, uv, &selector, &found);
	if (ret)
		return ret;
	if (!priv->input_suspended) {
		ret = regmap_field_write(priv->rmap_fields[F_VMIVR], selector);
		if (ret)
			return ret;
	}
	return linear_range_get_value(range, selector, &priv->mivr_request);
}

enum {
	MT6370_CHG_STAT_READY = 0,
	MT6370_CHG_STAT_CHARGE_IN_PROGRESS,
	MT6370_CHG_STAT_DONE,
	MT6370_CHG_STAT_FAULT,
	MT6370_CHG_STAT_MAX
};

enum {
	MT6370_ATTACH_STAT_DETACH = 0,
	MT6370_ATTACH_STAT_ATTACH_WAIT_FOR_BC12,
	MT6370_ATTACH_STAT_ATTACH_BC12_DONE,
	MT6370_ATTACH_STAT_ATTACH_MAX
};

static int mt6370_chg_otg_of_parse_cb(struct device_node *of,
				      const struct regulator_desc *rdesc,
				      struct regulator_config *rcfg)
{
	struct mt6370_priv *priv = rcfg->driver_data;

	rcfg->ena_gpiod = fwnode_gpiod_get_index(of_fwnode_handle(of),
						 "enable", 0, GPIOD_OUT_LOW |
						 GPIOD_FLAGS_BIT_NONEXCLUSIVE,
						 rdesc->name);
	if (IS_ERR(rcfg->ena_gpiod)) {
		rcfg->ena_gpiod = NULL;
		return 0;
	}

	return regmap_update_bits(priv->regmap, MT6370_REG_CHG_CTRL1,
				  MT6370_OTG_PIN_EN_MASK,
				  MT6370_OTG_PIN_EN_MASK);
}

static void mt6370_chg_bc12_work_func(struct work_struct *work)
{
	struct mt6370_priv *priv = container_of(work, struct mt6370_priv,
						bc12_work);
	struct power_supply *psy;
	int ret;
	bool rpt_psy = false;
	unsigned int attach, usb_stat;

	mutex_lock(&priv->attach_lock);
	attach = priv->attach;

	switch (attach) {
	case MT6370_ATTACH_STAT_DETACH:
		usb_stat = 0;
		break;
	case MT6370_ATTACH_STAT_ATTACH_WAIT_FOR_BC12:
		ret = mt6370_chg_field_set(priv, F_USBCHGEN, attach);
		if (ret)
			dev_err(priv->dev, "Failed to enable USB CHG EN\n");
		goto bc12_work_func_out;
	case MT6370_ATTACH_STAT_ATTACH_BC12_DONE:
		ret = mt6370_chg_field_get(priv, F_USB_STAT, &usb_stat);
		if (ret) {
			dev_err(priv->dev, "Failed to get USB status\n");
			goto bc12_work_func_out;
		}
		break;
	default:
		dev_err(priv->dev, "Invalid attach state\n");
		goto bc12_work_func_out;
	}

	rpt_psy = true;

	switch (usb_stat) {
	case MT6370_USB_STAT_SDP:
	case MT6370_USB_STAT_SDP_NSTD:
		priv->psy_usb_type = POWER_SUPPLY_USB_TYPE_SDP;
		break;
	case MT6370_USB_STAT_DCP:
		priv->psy_usb_type = POWER_SUPPLY_USB_TYPE_DCP;
		break;
	case MT6370_USB_STAT_CDP:
		priv->psy_usb_type = POWER_SUPPLY_USB_TYPE_CDP;
		break;
	case MT6370_USB_STAT_NO_VBUS:
	case MT6370_USB_STAT_VBUS_FLOW_IS_UNDER_GOING:
	default:
		priv->psy_usb_type = POWER_SUPPLY_USB_TYPE_UNKNOWN;
		break;
	}

bc12_work_func_out:
	mutex_unlock(&priv->attach_lock);

	/* An ONLINE write can queue work before registration returns the handle. */
	psy = READ_ONCE(priv->psy);
	if (rpt_psy && psy)
		power_supply_changed(psy);
}

static int mt6370_chg_toggle_cfo(struct mt6370_priv *priv)
{
	int ret;
	unsigned int fl_strobe;

	/* check if flash led in strobe mode */
	ret = mt6370_chg_field_get(priv, F_FL_STROBE, &fl_strobe);
	if (ret) {
		dev_err(priv->dev, "Failed to get FL_STROBE_EN\n");
		return ret;
	}

	if (fl_strobe) {
		dev_err(priv->dev, "Flash led is still in strobe mode\n");
		return -EINVAL;
	}

	/* cfo off */
	ret = mt6370_chg_field_set(priv, F_CFO_EN, 0);
	if (ret) {
		dev_err(priv->dev, "Failed to disable CFO_EN\n");
		return ret;
	}

	/* cfo on */
	ret = mt6370_chg_field_set(priv, F_CFO_EN, 1);
	if (ret)
		dev_err(priv->dev, "Failed to enable CFO_EN\n");

	return ret;
}

static struct iio_channel *mt6370_chg_find_ibus(struct iio_channel *channels)
{
	struct iio_channel *channel;

	/* The MT6370 ADC channel ID is not its position in io-channels. */
	for (channel = channels; channel->indio_dev; channel++) {
		if (channel->channel->type == IIO_CURRENT &&
		    channel->channel->channel == MT6370_CHAN_IBUS)
			return channel;
	}

	return NULL;
}

static void mt6370_chg_mivr_dwork_func(struct work_struct *work)
{
	struct mt6370_priv *priv = container_of(work, struct mt6370_priv,
						mivr_dwork.work);
	int ret, ibus;
	unsigned int mivr_stat;

	ret = mt6370_chg_field_get(priv, F_CHG_MIVR_STAT, &mivr_stat);
	if (ret) {
		dev_err(priv->dev, "Failed to get mivr state\n");
		goto mivr_handler_out;
	}

	if (!mivr_stat)
		goto mivr_handler_out;

	/* IIO current is in mA; the charger workaround threshold is in uA. */
	ret = iio_read_channel_processed_scale(priv->iio_ibus, &ibus, MILLI);
	if (ret) {
		dev_err(priv->dev, "Failed to get ibus\n");
		goto mivr_handler_out;
	}

	if (ibus >= 0 && ibus < MT6370_MIVR_IBUS_THRESHOLD_UA) {
		ret = mt6370_chg_toggle_cfo(priv);
		if (ret)
			dev_err(priv->dev, "Failed to toggle cfo\n");
	}

mivr_handler_out:
	enable_irq(priv->irq_nums[MT6370_IRQ_MIVR]);
	pm_relax(priv->dev);
}

static void mt6370_chg_pwr_rdy_check(struct mt6370_priv *priv)
{
	int ret;
	unsigned int opposite_pwr_rdy, otg_en;
	union power_supply_propval val;

	/* Check in OTG mode or not */
	ret = mt6370_chg_field_get(priv, F_BOOST_STAT, &otg_en);
	if (ret) {
		dev_err(priv->dev, "Failed to get OTG state\n");
		return;
	}

	if (otg_en)
		return;

	ret = mt6370_chg_field_get(priv, F_UVP_D_STAT, &opposite_pwr_rdy);
	if (ret) {
		dev_err(priv->dev, "Failed to get opposite power ready state\n");
		return;
	}

	val.intval = opposite_pwr_rdy ?
		     MT6370_ATTACH_STAT_DETACH :
		     MT6370_ATTACH_STAT_ATTACH_WAIT_FOR_BC12;

	ret = power_supply_set_property(priv->psy, POWER_SUPPLY_PROP_ONLINE,
					&val);
	if (ret)
		dev_err(priv->dev, "Failed to start attach/detach flow\n");
}

static int mt6370_chg_get_online(struct mt6370_priv *priv,
				 union power_supply_propval *val)
{
	mutex_lock(&priv->attach_lock);
	val->intval = !!priv->attach;
	mutex_unlock(&priv->attach_lock);

	return 0;
}

static int mt6370_chg_get_status(struct mt6370_priv *priv,
				 union power_supply_propval *val)
{
	int ret;
	unsigned int chg_stat;
	union power_supply_propval online;

	mt6370_chg_get_online(priv, &online);

	if (!online.intval) {
		val->intval = POWER_SUPPLY_STATUS_DISCHARGING;
		return 0;
	}

	ret = mt6370_chg_field_get(priv, F_CHG_STAT, &chg_stat);
	if (ret)
		return ret;

	switch (chg_stat) {
	case MT6370_CHG_STAT_READY:
	case MT6370_CHG_STAT_FAULT:
		val->intval = POWER_SUPPLY_STATUS_NOT_CHARGING;
		return ret;
	case MT6370_CHG_STAT_CHARGE_IN_PROGRESS:
		val->intval = POWER_SUPPLY_STATUS_CHARGING;
		return ret;
	case MT6370_CHG_STAT_DONE:
		val->intval = POWER_SUPPLY_STATUS_FULL;
		return ret;
	default:
		val->intval = POWER_SUPPLY_STATUS_UNKNOWN;
		return ret;
	}
}

static int mt6370_chg_get_charge_type(struct mt6370_priv *priv,
				      union power_supply_propval *val)
{
	int type, ret;
	unsigned int chg_stat, vbat_lvl;

	ret = mt6370_chg_field_get(priv, F_CHG_STAT, &chg_stat);
	if (ret)
		return ret;

	ret = mt6370_chg_field_get(priv, F_VBAT_LVL, &vbat_lvl);
	if (ret)
		return ret;

	switch (chg_stat) {
	case MT6370_CHG_STAT_CHARGE_IN_PROGRESS:
		if (vbat_lvl)
			type = POWER_SUPPLY_CHARGE_TYPE_FAST;
		else
			type = POWER_SUPPLY_CHARGE_TYPE_TRICKLE;
		break;
	case MT6370_CHG_STAT_READY:
	case MT6370_CHG_STAT_DONE:
	case MT6370_CHG_STAT_FAULT:
	default:
		type = POWER_SUPPLY_CHARGE_TYPE_NONE;
		break;
	}

	val->intval = type;

	return 0;
}

static int mt6370_chg_set_online(struct mt6370_priv *priv,
				 const union power_supply_propval *val)
{
	bool pwr_rdy = !!val->intval;

	mutex_lock(&priv->attach_lock);
	if (pwr_rdy == !!priv->attach) {
		dev_err(priv->dev, "pwr_rdy is same(%d)\n", pwr_rdy);
		mutex_unlock(&priv->attach_lock);
		return 0;
	}

	priv->attach = pwr_rdy;
	mutex_unlock(&priv->attach_lock);

	if (!queue_work(priv->wq, &priv->bc12_work))
		dev_err(priv->dev, "bc12 work has already queued\n");

	return 0;
}

static int mt6370_chg_get_property(struct power_supply *psy,
				   enum power_supply_property psp,
				   union power_supply_propval *val)
{
	struct mt6370_priv *priv = power_supply_get_drvdata(psy);
	enum mt6370_chg_reg_field fd;
	unsigned int setting;
	int ret;

	guard(mutex)(&priv->psy_lock);
	if (priv->stopping)
		return -ESHUTDOWN;

	switch (psp) {
	case POWER_SUPPLY_PROP_ONLINE:
		return mt6370_chg_get_online(priv, val);
	case POWER_SUPPLY_PROP_STATUS:
		return mt6370_chg_get_status(priv, val);
	case POWER_SUPPLY_PROP_CHARGE_TYPE:
		return mt6370_chg_get_charge_type(priv, val);
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT:
		fd = F_ICHG;
		break;
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT_MAX:
		val->intval = linear_range_get_max_value(&mt6370_chg_ranges[MT6370_RANGE_F_ICHG]);
		return 0;
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE:
		fd = F_VOREG;
		break;
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE_MAX:
		val->intval = linear_range_get_max_value(&mt6370_chg_ranges[MT6370_RANGE_F_VOREG]);
		return 0;
	case POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT:
		if (priv->ichg_workaround) {
			ret = mt6370_chg_get_input(priv, &setting);
			if (!ret)
				val->intval = setting;
			return ret;
		}
		fd = F_IAICR;
		break;
	case POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT:
		fd = F_VMIVR;
		break;
	case POWER_SUPPLY_PROP_PRECHARGE_CURRENT:
		fd = F_IPREC;
		break;
	case POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT:
		fd = F_IEOC;
		break;
	case POWER_SUPPLY_PROP_USB_TYPE: {
		guard(mutex)(&priv->attach_lock);
		val->intval = priv->psy_usb_type;
		return 0;
	}
	case POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR:
		return mt6370_chg_get_behaviour(priv, &val->intval);
	default:
		return -EINVAL;
	}

	/* Do not expose the temporary current setting in the middle of a ramp. */
	guard(mutex)(&priv->ichg_lock);
	ret = mt6370_chg_field_get(priv, fd, &setting);
	if (!ret)
		val->intval = setting;
	return ret;
}

static int mt6370_chg_set_property(struct power_supply *psy,
				   enum power_supply_property psp,
				   const union power_supply_propval *val)
{
	struct mt6370_priv *priv = power_supply_get_drvdata(psy);
	const struct linear_range *range;
	enum mt6370_chg_reg_field fd;
	unsigned int setting;
	int ret;

	guard(mutex)(&priv->psy_lock);
	if (priv->stopping)
		return -ESHUTDOWN;

	if (val->intval < 0)
		return -EINVAL;

	switch (psp) {
	case POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR:
		ret = mt6370_chg_set_behaviour(priv, val->intval);
		if (priv->ichg_workaround &&
		    (val->intval == POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO ||
		     val->intval == POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE))
			power_supply_changed(psy);
		return ret;
	case POWER_SUPPLY_PROP_ONLINE:
		if (val->intval > 1)
			return -EINVAL;
		return mt6370_chg_set_online(priv, val);
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT:
		fd = F_ICHG;
		break;
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE:
		fd = F_VOREG;
		break;
	case POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT:
		if (priv->ichg_workaround && !val->intval) {
			ret = mt6370_chg_suspend_input(priv);
			power_supply_changed(psy);
			return ret;
		}
		fd = F_IAICR;
		break;
	case POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT:
		fd = F_VMIVR;
		break;
	case POWER_SUPPLY_PROP_PRECHARGE_CURRENT:
		fd = F_IPREC;
		break;
	case POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT:
		fd = F_IEOC;
		break;
	default:
		return -EINVAL;
	}

	setting = val->intval;
	range = mt6370_chg_fields[fd].range;
	/* Never silently raise a requested limit to the supported minimum. */
	if (setting < range->min || setting > linear_range_get_max_value(range))
		return -ERANGE;

	if (fd == F_ICHG) {
		if (setting < priv->ichg_min)
			return -ERANGE;
		ret = mt6370_chg_set_ichg(priv, setting);
		/* Errors can also change hardware state by inhibiting charging. */
		power_supply_changed(psy);
		return ret;
	}
	if (priv->ichg_workaround && fd == F_IAICR) {
		ret = mt6370_chg_set_input(priv, setting);
		power_supply_changed(psy);
		return ret;
	}
	if (priv->ichg_workaround && fd == F_VMIVR)
		return mt6370_chg_set_mivr(priv, setting);

	return mt6370_chg_field_set(priv, fd, setting);
}

static int mt6370_chg_property_is_writeable(struct power_supply *psy,
					    enum power_supply_property psp)
{
	struct mt6370_priv *priv = power_supply_get_drvdata(psy);

	switch (psp) {
	case POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR:
		return priv->ichg_workaround;
	case POWER_SUPPLY_PROP_ONLINE:
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT:
	case POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE:
	case POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT:
	case POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT:
	case POWER_SUPPLY_PROP_PRECHARGE_CURRENT:
	case POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT:
		return 1;
	default:
		return 0;
	}
}

static enum power_supply_property mt6370_chg_properties[] = {
	POWER_SUPPLY_PROP_ONLINE,
	POWER_SUPPLY_PROP_STATUS,
	POWER_SUPPLY_PROP_CHARGE_TYPE,
	POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT,
	POWER_SUPPLY_PROP_CONSTANT_CHARGE_CURRENT_MAX,
	POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE,
	POWER_SUPPLY_PROP_CONSTANT_CHARGE_VOLTAGE_MAX,
	POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,
	POWER_SUPPLY_PROP_INPUT_VOLTAGE_LIMIT,
	POWER_SUPPLY_PROP_PRECHARGE_CURRENT,
	POWER_SUPPLY_PROP_CHARGE_TERM_CURRENT,
	POWER_SUPPLY_PROP_USB_TYPE,
	/* Keep last: omitted on models whose disable sequence is not established. */
	POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,
};

static const struct power_supply_desc mt6370_chg_psy_desc = {
	.name = "mt6370-charger",
	.type = POWER_SUPPLY_TYPE_USB,
	.charge_behaviours = BIT(POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO) |
			     BIT(POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE),
	.properties = mt6370_chg_properties,
	.num_properties = ARRAY_SIZE(mt6370_chg_properties),
	.get_property = mt6370_chg_get_property,
	.set_property = mt6370_chg_set_property,
	.property_is_writeable = mt6370_chg_property_is_writeable,
	.usb_types = BIT(POWER_SUPPLY_USB_TYPE_SDP) |
		     BIT(POWER_SUPPLY_USB_TYPE_CDP) |
		     BIT(POWER_SUPPLY_USB_TYPE_DCP) |
		     BIT(POWER_SUPPLY_USB_TYPE_UNKNOWN),
};

static const struct regulator_ops mt6370_chg_otg_ops = {
	.list_voltage = regulator_list_voltage_linear,
	.enable = regulator_enable_regmap,
	.disable = regulator_disable_regmap,
	.is_enabled = regulator_is_enabled_regmap,
	.set_voltage_sel = regulator_set_voltage_sel_regmap,
	.get_voltage_sel = regulator_get_voltage_sel_regmap,
	.set_current_limit = regulator_set_current_limit_regmap,
	.get_current_limit = regulator_get_current_limit_regmap,
};

static const u32 mt6370_chg_otg_oc_ma[] = {
	500000, 700000, 1100000, 1300000, 1800000, 2100000, 2400000,
};

static const struct regulator_desc mt6370_chg_otg_rdesc = {
	.of_match = "usb-otg-vbus-regulator",
	.of_parse_cb = mt6370_chg_otg_of_parse_cb,
	.name = "mt6370-usb-otg-vbus",
	.ops = &mt6370_chg_otg_ops,
	.owner = THIS_MODULE,
	.type = REGULATOR_VOLTAGE,
	.min_uV = 4425000,
	.uV_step = 25000,
	.n_voltages = 57,
	.vsel_reg = MT6370_REG_CHG_CTRL5,
	.vsel_mask = MT6370_VOBST_MASK,
	.enable_reg = MT6370_REG_CHG_CTRL1,
	.enable_mask = MT6370_OPA_MODE_MASK,
	.curr_table = mt6370_chg_otg_oc_ma,
	.n_current_limits = ARRAY_SIZE(mt6370_chg_otg_oc_ma),
	.csel_reg = MT6370_REG_CHG_CTRL10,
	.csel_mask = MT6370_OTG_OC_MASK,
};

static int mt6370_chg_init_rmap_fields(struct mt6370_priv *priv)
{
	int i;
	const struct mt6370_chg_field *fds = mt6370_chg_fields;

	for (i = 0; i < F_MAX; i++) {
		priv->rmap_fields[i] = devm_regmap_field_alloc(priv->dev,
							       priv->regmap,
							       fds[i].field);
		if (IS_ERR(priv->rmap_fields[i]))
			return dev_err_probe(priv->dev,
					PTR_ERR(priv->rmap_fields[i]),
					"Failed to allocate regmapfield[%s]\n",
					fds[i].name);
	}

	return 0;
}

static int mt6370_chg_init_setting(struct mt6370_priv *priv)
{
	unsigned int asleep;
	int ret;

	if (priv->ichg_workaround) {
		/* Policy must explicitly enable charging after validating its inputs. */
		ret = mt6370_chg_set_behaviour(priv, POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE);
		if (ret)
			return ret;
		ret = mt6370_chg_field_get(priv, F_FORCE_SLEEP, &asleep);
		if (ret)
			return ret;
		priv->input_suspended = asleep;
		ret = mt6370_chg_field_get(priv, F_VMIVR, &priv->mivr_request);
		if (ret)
			return ret;
	}

	/* Disable usb_chg_en */
	ret = mt6370_chg_field_set(priv, F_USBCHGEN, 0);
	if (ret) {
		dev_err(priv->dev, "Failed to disable usb_chg_en\n");
		return ret;
	}

	/* Establish the minimum input budget before releasing the ILIM pin. */
	ret = mt6370_chg_field_set(priv, F_IAICR, 100000);
	if (ret)
		return dev_err_probe(priv->dev, ret, "Failed to set initial input current\n");

	ret = mt6370_chg_field_set(priv, F_AICR_EN, 1);
	if (ret)
		return dev_err_probe(priv->dev, ret, "Failed to enable AICR\n");

	ret = mt6370_chg_field_set(priv, F_IINLMTSEL, 2);
	if (ret)
		return dev_err_probe(priv->dev, ret, "Failed to select IAICR\n");

	/* Match the stock settling interval before disabling hardware ILIM. */
	usleep_range(5000, 6000);

	ret = mt6370_chg_field_set(priv, F_ILIM_EN, 0);
	if (ret) {
		dev_err(priv->dev, "Failed to disable input current limit\n");
		return ret;
	}

	/* Initialize current together with its model-specific protection setting. */
	ret = mt6370_chg_set_ichg(priv, 900000);
	if (ret) {
		dev_err(priv->dev, "Failed to set ICHG to 900mA");
		return ret;
	}

	return 0;
}

#define MT6370_CHG_DT_PROP_DECL(_name, _type, _field)	\
{							\
	.name = "mediatek,chg-" #_name,			\
	.type = MT6370_PARSE_TYPE_##_type,		\
	.fd = _field,					\
}

static int mt6370_chg_init_otg_regulator(struct mt6370_priv *priv)
{
	struct regulator_config rcfg = {
		.dev = priv->dev,
		.regmap = priv->regmap,
		.driver_data = priv,
	};

	priv->rdev = devm_regulator_register(priv->dev, &mt6370_chg_otg_rdesc,
					     &rcfg);

	return PTR_ERR_OR_ZERO(priv->rdev);
}

static int mt6370_chg_init_psy(struct mt6370_priv *priv)
{
	struct power_supply *psy;
	struct power_supply_config cfg = {
		.drv_data = priv,
		.fwnode = dev_fwnode(priv->dev),
	};

	priv->psy_desc = mt6370_chg_psy_desc;
	if (!priv->ichg_workaround) {
		priv->psy_desc.num_properties--;
		priv->psy_desc.charge_behaviours = 0;
	}
	psy = devm_power_supply_register(priv->dev, &priv->psy_desc, &cfg);
	if (IS_ERR(psy))
		return PTR_ERR(psy);
	WRITE_ONCE(priv->psy, psy);

	return 0;
}

static irqreturn_t mt6370_attach_i_handler(int irq, void *data)
{
	struct mt6370_priv *priv = data;
	unsigned int otg_en;
	int ret;

	/* Check in OTG mode or not */
	ret = mt6370_chg_field_get(priv, F_BOOST_STAT, &otg_en);
	if (ret) {
		dev_err(priv->dev, "Failed to get OTG state\n");
		return IRQ_NONE;
	}

	if (otg_en)
		return IRQ_HANDLED;

	mutex_lock(&priv->attach_lock);
	priv->attach = MT6370_ATTACH_STAT_ATTACH_BC12_DONE;
	mutex_unlock(&priv->attach_lock);

	if (!queue_work(priv->wq, &priv->bc12_work))
		dev_err(priv->dev, "bc12 work has already queued\n");

	return IRQ_HANDLED;
}

static irqreturn_t mt6370_uvp_d_evt_handler(int irq, void *data)
{
	struct mt6370_priv *priv = data;

	mt6370_chg_pwr_rdy_check(priv);

	return IRQ_HANDLED;
}

static irqreturn_t mt6370_mivr_handler(int irq, void *data)
{
	struct mt6370_priv *priv = data;

	pm_stay_awake(priv->dev);
	disable_irq_nosync(priv->irq_nums[MT6370_IRQ_MIVR]);
	schedule_delayed_work(&priv->mivr_dwork, msecs_to_jiffies(200));

	return IRQ_HANDLED;
}

#define MT6370_CHG_IRQ(_name)						\
{									\
	.name = #_name,							\
	.handler = mt6370_##_name##_handler,				\
}

static int mt6370_chg_init_irq(struct mt6370_priv *priv)
{
	unsigned int i;
	int ret;
	const struct {
		char *name;
		irq_handler_t handler;
	} mt6370_chg_irqs[] = {
		MT6370_CHG_IRQ(attach_i),
		MT6370_CHG_IRQ(uvp_d_evt),
		MT6370_CHG_IRQ(mivr),
	};

	guard(mutex)(&priv->psy_lock);
	for (i = 0; i < ARRAY_SIZE(mt6370_chg_irqs); i++) {
		ret = platform_get_irq_byname(to_platform_device(priv->dev),
					      mt6370_chg_irqs[i].name);
		if (ret < 0)
			return ret;

		priv->irq_nums[i] = ret;
		ret = devm_request_threaded_irq(priv->dev, ret, NULL,
						mt6370_chg_irqs[i].handler,
						IRQF_TRIGGER_FALLING,
						dev_name(priv->dev), priv);
		if (ret)
			return dev_err_probe(priv->dev, ret,
					     "Failed to request irq %s\n",
					     mt6370_chg_irqs[i].name);
		priv->num_irqs++;
	}
	if (priv->input_suspended)
		mt6370_chg_pause_mivr(priv);

	return 0;
}

static void mt6370_chg_cancel_work(void *data)
{
	struct mt6370_priv *priv = data;

	mt6370_chg_cancel_mivr(priv);
	cancel_work_sync(&priv->bc12_work);
}

static void mt6370_chg_inhibit(void *data)
{
	struct mt6370_priv *priv = data;
	int ret;

	ret = mt6370_chg_set_behaviour(priv, POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE);
	if (ret)
		dev_err(priv->dev, "Failed to inhibit charging during teardown: %d\n", ret);
}

static void mt6370_chg_quiesce(void *data)
{
	struct mt6370_priv *priv = data;
	unsigned int i;

	mutex_lock(&priv->psy_lock);
	if (priv->stopping) {
		mutex_unlock(&priv->psy_lock);
		return;
	}
	priv->stopping = true;
	mutex_unlock(&priv->psy_lock);

	/* IRQ handlers may call power_supply_set_property(), so release psy_lock. */
	for (i = 0; i < priv->num_irqs; i++)
		disable_irq(priv->irq_nums[i]);
	mt6370_chg_cancel_work(priv);
	if (priv->ichg_workaround)
		mt6370_chg_inhibit(priv);
}

static void mt6370_chg_shutdown(struct platform_device *pdev)
{
	mt6370_chg_quiesce(platform_get_drvdata(pdev));
}

static int mt6370_chg_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mt6370_priv *priv;
	struct iio_channel *channels;
	int ret;

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	priv->dev = &pdev->dev;

	priv->regmap = dev_get_regmap(pdev->dev.parent, NULL);
	if (!priv->regmap)
		return dev_err_probe(dev, -ENODEV, "Failed to get regmap\n");

	ret = mt6370_chg_init_rmap_fields(priv);
	if (ret)
		return dev_err_probe(dev, ret, "Failed to init regmap fields\n");

	platform_set_drvdata(pdev, priv);

	channels = devm_iio_channel_get_all(priv->dev);
	if (IS_ERR(channels))
		return dev_err_probe(dev, PTR_ERR(channels),
				     "Failed to get iio adc\n");
	priv->iio_ibus = mt6370_chg_find_ibus(channels);
	if (!priv->iio_ibus)
		return dev_err_probe(dev, -EINVAL, "Missing MT6370 IBUS channel\n");

	ret = devm_mutex_init(dev, &priv->attach_lock);
	if (ret)
		return ret;
	ret = devm_mutex_init(dev, &priv->ichg_lock);
	if (ret)
		return ret;
	ret = devm_mutex_init(dev, &priv->psy_lock);
	if (ret)
		return ret;

	ret = mt6370_chg_init_ichg(priv);
	if (ret)
		return dev_err_probe(dev, ret, "Failed to identify charger current limits\n");

	priv->attach = MT6370_ATTACH_STAT_DETACH;

	priv->wq = devm_alloc_ordered_workqueue(dev, "%s", 0, dev_name(priv->dev));
	if (!priv->wq)
		return -ENOMEM;

	INIT_WORK(&priv->bc12_work, mt6370_chg_bc12_work_func);
	INIT_DELAYED_WORK(&priv->mivr_dwork, mt6370_chg_mivr_dwork_func);

	ret = mt6370_chg_init_setting(priv);
	if (ret)
		return dev_err_probe(dev, ret,
				     "Failed to init mt6370 charger setting\n");

	if (priv->ichg_workaround) {
		ret = devm_add_action_or_reset(dev, mt6370_chg_inhibit, priv);
		if (ret)
			return ret;
	}

	ret = mt6370_chg_init_otg_regulator(priv);
	if (ret)
		return dev_err_probe(dev, ret, "Failed to init OTG regulator\n");

	/* Publish callbacks only after their locks, work items and limits exist. */
	ret = mt6370_chg_init_psy(priv);
	if (ret)
		return dev_err_probe(dev, ret, "Failed to init psy\n");

	ret = mt6370_chg_init_irq(priv);
	if (ret) {
		mt6370_chg_quiesce(priv);
		return ret;
	}

	/* Quiesce before the managed IRQs, power supply and workqueue disappear. */
	ret = devm_add_action_or_reset(dev, mt6370_chg_quiesce, priv);
	if (ret)
		return ret;

	mt6370_chg_pwr_rdy_check(priv);

	return 0;
}

static const struct of_device_id mt6370_chg_of_match[] = {
	{ .compatible = "mediatek,mt6370-charger", },
	{}
};
MODULE_DEVICE_TABLE(of, mt6370_chg_of_match);

static struct platform_driver mt6370_chg_driver = {
	.probe = mt6370_chg_probe,
	.shutdown = mt6370_chg_shutdown,
	.driver = {
		.name = "mt6370-charger",
		.of_match_table = mt6370_chg_of_match,
	},
};
module_platform_driver(mt6370_chg_driver);

MODULE_AUTHOR("ChiaEn Wu <chiaen_wu@richtek.com>");
MODULE_DESCRIPTION("MediaTek MT6370 Charger Driver");
MODULE_LICENSE("GPL v2");
