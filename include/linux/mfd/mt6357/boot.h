/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef __MFD_MT6357_BOOT_H__
#define __MFD_MT6357_BOOT_H__

#include <linux/bits.h>
#include <linux/errno.h>
#include <linux/types.h>

struct device;

enum mt6357_boot_field {
	MT6357_BOOT_POWER_ON,
	MT6357_BOOT_PLUGIN,
	MT6357_BOOT_STARTUP,
	MT6357_BOOT_SYSTEM_INFO,
	MT6357_BOOT_BATTERY_STATUS,
	MT6357_BOOT_FIELD_COUNT,
};

struct mt6357_boot_word {
	u16 raw;
	int error;
};

/* First parent observation, not a timestamp of the PMIC's capture event. */
struct mt6357_boot_snapshot {
	u64 started_ns;
	u64 finished_ns;
	u16 swcid;
	bool observed;
	struct mt6357_boot_word words[MT6357_BOOT_FIELD_COUNT];
};

struct mt6357_boot_ocv {
	u16 code;
	u32 microvolts;
};

struct mt6357_boot_metadata {
	bool startup_select;
	bool reboot_2sec;
	bool preloader_charging;
	bool monitor_preloader_charging;
	bool battery_plug;
	bool nvram_fail;
	bool monitor_shutdown_valid_time;
	u8 stored_soc;
	bool battery_present;
};

/* Successful decoding does not establish freshness or suitability as a seed. */
static inline int mt6357_boot_decode_ocv(const struct mt6357_boot_word *word,
				       struct mt6357_boot_ocv *out)
{
	struct mt6357_boot_ocv value = {};

	if (word->error)
		return word->error;
	if (!(word->raw & BIT(15)))
		return -ENODATA;
	value.code = word->raw & GENMASK(14, 0);
	/* Preserve stock 0.1 mV truncation before conversion to microvolts. */
	value.microvolts = (u32)value.code * 54000 / 32768 * 100;
	*out = value;
	return 0;
}

static inline int mt6357_boot_decode_metadata(const struct mt6357_boot_snapshot *s,
					    struct mt6357_boot_metadata *out)
{
	struct mt6357_boot_metadata value = {};
	u16 info;
	unsigned int i;

	if (!s->observed)
		return -ENODATA;
	for (i = MT6357_BOOT_STARTUP; i < MT6357_BOOT_FIELD_COUNT; i++)
		if (s->words[i].error)
			return s->words[i].error;
	info = s->words[MT6357_BOOT_SYSTEM_INFO].raw;
	value.startup_select = !!(s->words[MT6357_BOOT_STARTUP].raw & BIT(2));
	value.reboot_2sec = !!(info & BIT(0));
	value.preloader_charging = !!(info & BIT(1));
	value.monitor_preloader_charging = !!(info & BIT(2));
	value.battery_plug = !!(info & BIT(3));
	value.nvram_fail = !!(info & BIT(4));
	value.monitor_shutdown_valid_time = !!(info & BIT(5));
	value.stored_soc = (info >> 9) & 0x7f;
	value.battery_present = !(s->words[MT6357_BOOT_BATTERY_STATUS].raw & BIT(1));
	*out = value;
	return 0;
}

/*
 * LK emits bounded signed decimal bytes without a terminating NUL. Accept one
 * optional final NUL too, but no plus sign, whitespace, embedded NUL or suffix.
 * Parsing says nothing about measurement validity, units, age or source.
 */
static inline int mt6357_boot_parse_lk_decimal(const void *data, size_t len, s32 *out)
{
	const u8 *text = data;
	u32 value = 0, limit;
	bool negative;
	size_t i = 0;

	if (!data || !out || !len || len > 12)
		return -EINVAL;
	if (!text[len - 1])
		len--;
	if (!len || len > 11)
		return -EINVAL;
	negative = text[0] == '-';
	if (negative)
		i++;
	if (i == len)
		return -EINVAL;
	limit = negative ? 2147483648U : 2147483647U;
	for (; i < len; i++) {
		unsigned int digit = text[i] - '0';

		if (digit > 9)
			return -EINVAL;
		if (value > (limit - digit) / 10)
			return -ERANGE;
		value = value * 10 + digit;
	}
	*out = negative ? -(s64)value : value;
	return 0;
}

/*
 * Caller holds a live parent device reference. This copies immutable data; no
 * pointer to parent devres escapes. -EAGAIN means probe/removal/another caller
 * holds the device lock. In particular a synchronous child probe must defer.
 * Parent rebind yields a new observation, not proof of a fresh silicon capture.
 */
int mt6357_boot_snapshot_get(struct device *parent, struct mt6357_boot_snapshot *out);

#endif /* __MFD_MT6357_BOOT_H__ */
