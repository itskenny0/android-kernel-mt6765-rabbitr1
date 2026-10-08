// SPDX-License-Identifier: GPL-2.0-only
/* Board policy for the r1's MT6357 battery and MT6370 charger. */
#include <linux/device.h>
#include <linux/jiffies.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/property.h>
#include <linux/workqueue.h>

#define R1_CHARGE_MIN_UA 500000
#define R1_CHARGE_MAX_UA 1000000
#define R1_SAMPLE_MAX_MS 1500
#define R1_SESSION_TIMEOUT (10 * 60 * 60 * HZ)

enum r1_charge_reason {
	R1_WAITING, R1_UNPLUGGED, R1_NO_BATTERY, R1_SENSOR_ERROR,
	R1_CHARGER_FAULT, R1_TIMER, R1_TEMPERATURE, R1_VOLTAGE,
	R1_USB_BUDGET, R1_USER_LIMIT, R1_THERMAL_LIMIT, R1_READY,
	R1_IO_ERROR, R1_STALE, R1_STOPPED,
};

static const char * const r1_reason_names[] = {
	"waiting", "unplugged", "no-battery", "sensor-error",
	"charger-fault", "safety-timer", "temperature", "voltage",
	"usb-budget", "user-limit", "thermal-limit", "ready",
	"io-error", "stale", "stopped",
};

struct r1_charge_sample {
	bool valid;
	int present, plugged, health, temperature, voltage, current_ua;
	int usb_type, gadget_ua;
	int typec_online, typec_type, typec_ua, typec_uv;
	bool timer_expired;
};

struct r1_charge_target {
	int input_ua, charge_ua, voltage_uv;
	enum r1_charge_reason reason;
};

struct r1_charge_policy {
	struct device *dev;
	struct power_supply *charger, *battery, *gadget, *typec;
	struct mutex lock;
	struct delayed_work work;
	struct notifier_block notifier;
	atomic_t generation;
	bool stopping, suspended, cold, hot, full, session, timeout;
	bool notifier_registered;
	unsigned long session_start;
	int requested_ua, measured_ua, error;
	struct r1_charge_target applied;
};

static bool r1_charge_limit_valid(int ua)
{
	return ua >= R1_CHARGE_MIN_UA && ua <= R1_CHARGE_MAX_UA && !(ua % 100000);
}

static struct r1_charge_target r1_charge_decide(struct r1_charge_policy *p,
					      const struct r1_charge_sample *s)
{
	struct r1_charge_target t = { .reason = R1_SENSOR_ERROR };
	int thermal_ua = R1_CHARGE_MAX_UA, budget = 0;

	if (!s->valid)
		return t;
	if (!s->plugged) {
		t.reason = R1_UNPLUGGED;
		return t;
	}
	if (!s->present) {
		t.reason = R1_NO_BATTERY;
		return t;
	}
	if (s->timer_expired || s->health == POWER_SUPPLY_HEALTH_SAFETY_TIMER_EXPIRE) {
		t.reason = R1_TIMER;
		return t;
	}
	if (s->health != POWER_SUPPLY_HEALTH_GOOD) {
		t.reason = R1_CHARGER_FAULT;
		return t;
	}
	/* Conservative bring-up limits; stock permits 0..55 C and 4.4 V. */
	p->cold = s->temperature < 50 || (p->cold && s->temperature < 70);
	p->hot = s->temperature >= 500 || (p->hot && s->temperature >= 480);
	if (p->cold || p->hot) {
		t.reason = R1_TEMPERATURE;
		return t;
	}
	t.voltage_uv = 4200000;
	if (s->temperature < 150 || s->temperature >= 430) {
		thermal_ua = 500000;
		if (s->temperature >= 430)
			t.voltage_uv = 4100000;
	}
	p->full = s->voltage >= t.voltage_uv ||
		  (p->full && s->voltage >= t.voltage_uv - 100000);
	if (s->voltage < 3000000 || s->voltage > 4400000 || p->full) {
		t.reason = R1_VOLTAGE;
		return t;
	}

	/* An established Type-C/PD budget takes precedence over BC1.2. */
	if (s->typec_online && s->typec_uv >= 4500000 && s->typec_uv <= 5500000 &&
	    ((s->typec_type == POWER_SUPPLY_USB_TYPE_C && s->typec_ua >= 1500000) ||
	     s->typec_type == POWER_SUPPLY_USB_TYPE_PD)) {
		budget = min(s->typec_ua, R1_CHARGE_MAX_UA);
	} else if (s->typec_online && s->typec_uv > 5500000) {
		/* No high-voltage negotiation in this board policy. */
		budget = 0;
	} else if (s->usb_type == POWER_SUPPLY_USB_TYPE_DCP ||
		   s->usb_type == POWER_SUPPLY_USB_TYPE_CDP) {
		budget = R1_CHARGE_MAX_UA;
	} else if (s->usb_type == POWER_SUPPLY_USB_TYPE_SDP) {
		budget = clamp(s->gadget_ua, 0, 500000);
	}
	/* IAICR cannot represent enumeration/reset/suspend budgets below 100 mA. */
	t.input_ua = budget >= 100000 ? budget / 50000 * 50000 : 0;
	t.charge_ua = min3(p->requested_ua, thermal_ua, budget) / 100000 * 100000;
	if (t.charge_ua < R1_CHARGE_MIN_UA) {
		t.charge_ua = 0;
		t.input_ua = 0;
	}
	if (budget < min(p->requested_ua, thermal_ua))
		t.reason = R1_USB_BUDGET;
	else if (thermal_ua < p->requested_ua)
		t.reason = R1_THERMAL_LIMIT;
	else if (p->requested_ua < R1_CHARGE_MAX_UA)
		t.reason = R1_USER_LIMIT;
	else
		t.reason = R1_READY;
	return t;
}

static int r1_get(struct power_supply *psy, enum power_supply_property prop, int *value)
{
	union power_supply_propval v;
	int ret = power_supply_get_property(psy, prop, &v);

	if (!ret)
		*value = v.intval;
	return ret;
}

static int r1_set(struct r1_charge_policy *p, enum power_supply_property prop, int value)
{
	union power_supply_propval v = { .intval = value };

	return power_supply_set_property(p->charger, prop, &v);
}

static int r1_charge_isolate(struct r1_charge_policy *p)
{
	int ret, err;

	/* FORCE_SLEEP first; the driver also attempts the charge ramp-down. */
	ret = r1_set(p, POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT, 0);
	err = r1_set(p, POWER_SUPPLY_PROP_CHARGE_BEHAVIOUR,
		     POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE);
	p->applied.input_ua = 0;
	p->applied.charge_ua = 0;
	p->applied.voltage_uv = 0;
	return ret ? ret : err;
}

static int r1_charge_sample(struct r1_charge_policy *p, struct r1_charge_sample *s)
{
	int ret;

#define READ_SAMPLE(psy, prop, field) do { \
	ret = r1_get(p->psy, POWER_SUPPLY_PROP_##prop, &s->field); \
	if (ret) \
		return ret; \
} while (0)
	READ_SAMPLE(charger, PRESENT, plugged);
	if (!s->plugged) {
		p->session = false;
		p->timeout = false;
		s->valid = true;
		return 0;
	}
	if (!p->session) {
		p->session_start = jiffies;
		p->session = true;
	}
	if (time_after_eq(jiffies, p->session_start + R1_SESSION_TIMEOUT))
		p->timeout = true;
	READ_SAMPLE(charger, HEALTH, health);
	if (s->health == POWER_SUPPLY_HEALTH_SAFETY_TIMER_EXPIRE)
		p->timeout = true;
	s->timer_expired = p->timeout;
	READ_SAMPLE(battery, PRESENT, present);
	READ_SAMPLE(battery, TEMP, temperature);
	READ_SAMPLE(battery, VOLTAGE_NOW, voltage);
	READ_SAMPLE(battery, CURRENT_NOW, current_ua);
	READ_SAMPLE(charger, USB_TYPE, usb_type);
	READ_SAMPLE(gadget, CURRENT_MAX, gadget_ua);
	if (p->typec) {
		READ_SAMPLE(typec, ONLINE, typec_online);
		READ_SAMPLE(typec, USB_TYPE, typec_type);
		READ_SAMPLE(typec, CURRENT_MAX, typec_ua);
		READ_SAMPLE(typec, VOLTAGE_NOW, typec_uv);
		if (!s->typec_online)
			s->plugged = 0;
	}
#undef READ_SAMPLE
	s->valid = true;
	return 0;
}

static bool r1_charge_fresh(struct r1_charge_policy *p, unsigned long start, int generation)
{
	return atomic_read(&p->generation) == generation &&
	       time_before(jiffies, start + msecs_to_jiffies(R1_SAMPLE_MAX_MS));
}

static int r1_charge_apply(struct r1_charge_policy *p, struct r1_charge_target *t,
			   unsigned long start, int generation)
{
	int ret;

	if (!t->input_ua) {
		t->voltage_uv = 0;
		return r1_charge_isolate(p);
	}
#define SET_LIMIT(prop, value) do { \
	ret = r1_set(p, POWER_SUPPLY_PROP_##prop, value); \
	if (ret) \
		return ret; \
} while (0)
	/* A changed target is programmed with charging inhibited, enable last. */
	if (t->input_ua != p->applied.input_ua || t->charge_ua != p->applied.charge_ua ||
	    t->voltage_uv != p->applied.voltage_uv || p->error) {
		SET_LIMIT(CHARGE_BEHAVIOUR, POWER_SUPPLY_CHARGE_BEHAVIOUR_INHIBIT_CHARGE);
		SET_LIMIT(CONSTANT_CHARGE_VOLTAGE, t->voltage_uv);
		SET_LIMIT(INPUT_VOLTAGE_LIMIT, 4200000);
		SET_LIMIT(PRECHARGE_CURRENT, 100000);
		SET_LIMIT(CHARGE_TERM_CURRENT, 100000);
		/* Set charge current before restoring the suspended input path. */
		if (t->charge_ua)
			SET_LIMIT(CONSTANT_CHARGE_CURRENT, t->charge_ua);
		if (!r1_charge_fresh(p, start, generation))
			return -EAGAIN;
		SET_LIMIT(INPUT_CURRENT_LIMIT, t->input_ua);
	}
	if (!r1_charge_fresh(p, start, generation))
		return -EAGAIN;
	if (t->charge_ua)
		SET_LIMIT(CHARGE_BEHAVIOUR, POWER_SUPPLY_CHARGE_BEHAVIOUR_AUTO);
#undef SET_LIMIT
	return r1_charge_fresh(p, start, generation) ? 0 : -EAGAIN;
}

static void r1_charge_work(struct work_struct *work)
{
	struct r1_charge_policy *p = container_of(to_delayed_work(work),
						struct r1_charge_policy, work);
	struct r1_charge_sample sample = {};
	struct r1_charge_target target;
	unsigned long start = jiffies;
	int ret, cleanup, generation = atomic_read(&p->generation);

	mutex_lock(&p->lock);
	if (p->stopping || p->suspended)
		goto out;
	ret = r1_charge_sample(p, &sample);
	p->measured_ua = sample.current_ua;
	target = r1_charge_decide(p, &sample);
	if (!ret && !r1_charge_fresh(p, start, generation)) {
		target = (struct r1_charge_target) { .reason = R1_STALE };
		ret = -EAGAIN;
	}
	if (!ret)
		ret = r1_charge_apply(p, &target, start, generation);
	if (ret) {
		cleanup = r1_charge_isolate(p);
		if (cleanup)
			dev_err_ratelimited(p->dev, "Charging isolation failed: %d\n", cleanup);
		target.input_ua = 0;
		target.charge_ua = 0;
		target.voltage_uv = 0;
		if (sample.valid)
			target.reason = ret == -EAGAIN ? R1_STALE : R1_IO_ERROR;
	}
	p->error = ret;
	p->applied = target;
	/* Preserve an immediate update queued by a concurrent budget change. */
	queue_delayed_work(system_highpri_wq, &p->work, 2 * HZ);
out:
	mutex_unlock(&p->lock);
}

static int r1_charge_notify(struct notifier_block *nb, unsigned long event, void *data)
{
	struct r1_charge_policy *p = container_of(nb, struct r1_charge_policy, notifier);

	/* Charger writes produce notifications too. Poll its live state every 2 s. */
	if (event == PSY_EVENT_PROP_CHANGED &&
	    (data == p->gadget || data == p->battery || data == p->typec)) {
		atomic_inc(&p->generation);
		if (!READ_ONCE(p->stopping) && !READ_ONCE(p->suspended))
			mod_delayed_work(system_highpri_wq, &p->work, 0);
	}
	return NOTIFY_OK;
}

static ssize_t charge_control_limit_show(struct device *dev,
					struct device_attribute *attr, char *buf)
{
	struct r1_charge_policy *p = dev_get_drvdata(dev);
	int value;

	mutex_lock(&p->lock);
	value = p->requested_ua;
	mutex_unlock(&p->lock);
	return sysfs_emit(buf, "%d\n", value);
}

static ssize_t charge_control_limit_store(struct device *dev,
					 struct device_attribute *attr,
					 const char *buf, size_t count)
{
	struct r1_charge_policy *p = dev_get_drvdata(dev);
	int ret, value;

	ret = kstrtoint(buf, 10, &value);
	if (ret)
		return ret;
	if (!r1_charge_limit_valid(value))
		return -ERANGE;
	mutex_lock(&p->lock);
	if (p->stopping) {
		mutex_unlock(&p->lock);
		return -ESHUTDOWN;
	}
	p->requested_ua = value;
	atomic_inc(&p->generation);
	if (!p->suspended)
		mod_delayed_work(system_highpri_wq, &p->work, 0);
	mutex_unlock(&p->lock);
	return count;
}
static DEVICE_ATTR_RW(charge_control_limit);

static ssize_t status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct r1_charge_policy *p = dev_get_drvdata(dev);
	ssize_t length;

	mutex_lock(&p->lock);
	length = sysfs_emit(buf, "requested_ua=%d\napplied_ua=%d\ninput_ua=%d\n"
			   "voltage_uv=%d\nmeasured_ua=%d\nreason=%s\nerror=%d\n",
			   p->requested_ua, p->applied.charge_ua, p->applied.input_ua,
			   p->applied.voltage_uv, p->measured_ua,
			   r1_reason_names[p->applied.reason], p->error);
	mutex_unlock(&p->lock);
	return length;
}
static DEVICE_ATTR_RO(status);

static struct attribute *r1_charge_attrs[] = {
	&dev_attr_charge_control_limit.attr,
	&dev_attr_status.attr,
	NULL,
};
static const struct attribute_group r1_charge_group = { .attrs = r1_charge_attrs };

static void r1_charge_stop(void *data)
{
	struct r1_charge_policy *p = data;

	mutex_lock(&p->lock);
	p->stopping = true;
	mutex_unlock(&p->lock);
	if (p->notifier_registered) {
		power_supply_unreg_notifier(&p->notifier);
		p->notifier_registered = false;
	}
	cancel_delayed_work_sync(&p->work);
	mutex_lock(&p->lock);
	p->error = r1_charge_isolate(p);
	p->applied.reason = R1_STOPPED;
	mutex_unlock(&p->lock);
}

static int r1_charge_supply(struct device *dev, const char *name, struct power_supply **psy)
{
	*psy = devm_power_supply_get_by_reference(dev, name);
	if (IS_ERR(*psy))
		return PTR_ERR(*psy);
	if (!*psy)
		return -EPROBE_DEFER;
	/* Unbind the consumer before a supplier frees its private driver data. */
	if (!device_link_add(dev, (*psy)->dev.parent, DL_FLAG_AUTOREMOVE_CONSUMER))
		return -ENOMEM;
	return 0;
}

static int r1_charge_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct r1_charge_policy *p;
	int ret;

	p = devm_kzalloc(dev, sizeof(*p), GFP_KERNEL);
	if (!p)
		return -ENOMEM;
	p->dev = dev;
	/* Android restores the persistent preference; diagnostics start at 500 mA. */
	p->requested_ua = R1_CHARGE_MIN_UA;
	p->applied.reason = R1_WAITING;
	platform_set_drvdata(pdev, p);
	mutex_init(&p->lock);
	atomic_set(&p->generation, 0);
	INIT_DELAYED_WORK(&p->work, r1_charge_work);
	ret = r1_charge_supply(dev, "charger-supply", &p->charger);
	if (ret)
		return dev_err_probe(dev, ret, "Missing charger\n");
	/* Probe failure after finding the charger must leave it isolated. */
	ret = r1_charge_isolate(p);
	if (ret)
		return dev_err_probe(dev, ret, "Cannot isolate charger\n");
	if (!device_property_read_bool(p->charger->dev.parent, "richtek,managed-charging"))
		return dev_err_probe(dev, -EINVAL, "Charger requires managed charging\n");
	ret = r1_charge_supply(dev, "battery-supply", &p->battery);
	if (ret)
		return dev_err_probe(dev, ret, "Missing battery measurements\n");
	ret = r1_charge_supply(dev, "usb-gadget-supply", &p->gadget);
	if (ret)
		return dev_err_probe(dev, ret, "Missing USB gadget budget\n");
	if (device_property_present(dev, "typec-supply")) {
		ret = r1_charge_supply(dev, "typec-supply", &p->typec);
		if (ret)
			return dev_err_probe(dev, ret, "Missing Type-C budget\n");
	}
	ret = devm_add_action_or_reset(dev, r1_charge_stop, p);
	if (ret)
		return ret;
	p->notifier.notifier_call = r1_charge_notify;
	ret = power_supply_reg_notifier(&p->notifier);
	if (ret)
		return ret;
	p->notifier_registered = true;
	ret = devm_device_add_group(dev, &r1_charge_group);
	if (ret)
		return ret;
	mod_delayed_work(system_highpri_wq, &p->work, 0);
	return 0;
}

static int r1_charge_suspend(struct device *dev)
{
	struct r1_charge_policy *p = dev_get_drvdata(dev);
	int ret;

	mutex_lock(&p->lock);
	p->suspended = true;
	mutex_unlock(&p->lock);
	cancel_delayed_work_sync(&p->work);
	mutex_lock(&p->lock);
	p->error = r1_charge_isolate(p);
	p->applied.reason = R1_STOPPED;
	/* On failure the PM core will not call our resume callback. */
	if (p->error) {
		p->suspended = false;
		mod_delayed_work(system_highpri_wq, &p->work, 0);
	}
	ret = p->error;
	mutex_unlock(&p->lock);
	return ret;
}

static int r1_charge_resume(struct device *dev)
{
	struct r1_charge_policy *p = dev_get_drvdata(dev);

	mutex_lock(&p->lock);
	p->suspended = false;
	if (!p->stopping)
		mod_delayed_work(system_highpri_wq, &p->work, 0);
	mutex_unlock(&p->lock);
	return 0;
}

static DEFINE_SIMPLE_DEV_PM_OPS(r1_charge_pm, r1_charge_suspend, r1_charge_resume);

static void r1_charge_shutdown(struct platform_device *pdev)
{
	r1_charge_stop(platform_get_drvdata(pdev));
}

static const struct of_device_id r1_charge_match[] = {
	{ .compatible = "rabbit,r1-charging-policy" },
	{}
};
MODULE_DEVICE_TABLE(of, r1_charge_match);

static struct platform_driver r1_charge_driver = {
	.probe = r1_charge_probe,
	.shutdown = r1_charge_shutdown,
	.driver = {
		.name = "rabbit-r1-charging",
		.of_match_table = r1_charge_match,
		.pm = pm_sleep_ptr(&r1_charge_pm),
	},
};
module_platform_driver(r1_charge_driver);

MODULE_DESCRIPTION("Rabbit r1 charging policy");
MODULE_LICENSE("GPL");
