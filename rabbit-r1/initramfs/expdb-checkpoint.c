// SPDX-License-Identifier: GPL-2.0-only
/* Verify a stopped pstore console on the r1's bounded expdb mapping. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/dm-ioctl.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#include "../android/device/logging/ExpdbLayout.h"

#define CONSOLE_OFFSET (64U * 1024)
#define CONSOLE_BYTES (1024U * 1024)
#define CONSOLE_SIGNATURE (0x43474244U ^ 2U)
#define DIRECT_ALIGNMENT 4096U
#define MARKER_BYTES 224U

struct table_identity {
	dev_t backing;
	uint32_t event;
};

static int decimal(const char **cursor, unsigned int *value)
{
	const char *p = *cursor;
	unsigned int n = 0;

	if (*p < '0' || *p > '9')
		return 0;
	if (p[0] == '0' && p[1] >= '0' && p[1] <= '9')
		return 0;
	while (*p >= '0' && *p <= '9') {
		unsigned int digit = (unsigned int)(*p++ - '0');

		if (n > (UINT_MAX - digit) / 10)
			return 0;
		n = n * 10 + digit;
	}
	*cursor = p;
	*value = n;
	return 1;
}

static int map_path(const char *path, unsigned int *number)
{
	if (strncmp(path, "/dev/dm-", 8))
		return 0;
	path += 8;
	return decimal(&path, number) && !*path;
}

static int make_marker(char marker[MARKER_BYTES], const char *uuid,
		       const char *release)
{
	size_t length = strnlen(release, 129);
	int count;

	if (strnlen(uuid, 37) != 36 || !length || length > 128)
		return 0;
	for (size_t i = 0; i < 36; ++i) {
		int hyphen = i == 8 || i == 13 || i == 18 || i == 23;

		if (hyphen ? uuid[i] != '-' :
		    !((uuid[i] >= '0' && uuid[i] <= '9') ||
		      (uuid[i] >= 'a' && uuid[i] <= 'f')))
			return 0;
	}
	for (size_t i = 0; i < length; ++i)
		if (!((release[i] >= '0' && release[i] <= '9') ||
		      (release[i] >= 'a' && release[i] <= 'z') ||
		      (release[i] >= 'A' && release[i] <= 'Z') ||
		      strchr("._+-", release[i])))
			return 0;
	count = snprintf(marker, MARKER_BYTES,
			 "r1: expdb shutdown checkpoint %s %s\n", uuid, release);
	return count > 0 && count < (int)MARKER_BYTES;
}

/* Linux's DM ABI uses new_encode_dev, independent of libc's dev_t size. */
static uint64_t dm_device(dev_t device)
{
	unsigned int maj = major(device), min = minor(device);

	if (maj > 4095 || min > 1048575)
		return UINT64_MAX;
	return (min & 0xffU) | (maj << 8) | ((uint64_t)(min & ~0xffU) << 12);
}

static int parse_table(const unsigned char *buffer, size_t capacity,
		       dev_t mapping, struct table_identity *identity)
{
	const struct dm_ioctl *dm = (const void *)buffer;
	const struct dm_target_spec *target;
	const char *params, *end;
	unsigned int maj, min;
	size_t param_bytes, used, aligned;
	uint32_t permitted = DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG;

	if (capacity < sizeof(*dm) || dm->version[0] != DM_VERSION_MAJOR ||
	    dm->data_size > capacity || dm->data_start < sizeof(*dm) ||
	    dm->data_start % 8 || dm->data_start > dm->data_size ||
	    dm->data_size - dm->data_start < sizeof(*target) + 1 ||
	    dm->target_count != 1 || dm->dev != dm_device(mapping) ||
	    dm->flags != permitted ||
	    !memchr(dm->name, 0, sizeof(dm->name)) ||
	    strcmp(dm->name, R1_EXPDB_MAP_NAME) || dm->uuid[0])
		return 0;
	target = (const void *)(buffer + dm->data_start);
	if (target->sector_start || target->length != R1_EXPDB_LOG_SECTORS ||
	    target->status ||
	    !memchr(target->target_type, 0, sizeof(target->target_type)) ||
	    strcmp(target->target_type, "linear"))
		return 0;
	params = (const char *)(target + 1);
	param_bytes = dm->data_size - dm->data_start - sizeof(*target);
	end = memchr(params, 0, param_bytes);
	if (!end)
		return 0;
	/* The kernel excludes alignment padding from data_size for the last target. */
	used = sizeof(*target) + (size_t)(end - params) + 1;
	aligned = (used + 7) & ~(size_t)7;
	if (used != dm->data_size - dm->data_start ||
	    aligned != target->next || aligned > capacity - dm->data_start ||
	    !decimal(&params, &maj) || *params++ != ':' ||
	    !decimal(&params, &min) || strcmp(params, " 0") ||
	    maj > 4095 || min > 1048575)
		return 0;
	identity->backing = makedev(maj, min);
	identity->event = dm->event_nr;
	return 1;
}

static int live_table(int control, dev_t mapping, struct table_identity *identity)
{
	_Alignas(8) unsigned char buffer[R1_EXPDB_DM_BUFFER_SIZE] = {0};
	struct dm_ioctl *dm = (void *)buffer;

	dm->version[0] = DM_VERSION_MAJOR;
	dm->version[1] = DM_VERSION_MINOR;
	dm->version[2] = DM_VERSION_PATCHLEVEL;
	dm->data_size = sizeof(buffer);
	dm->data_start = sizeof(*dm);
	dm->dev = dm_device(mapping);
	dm->flags = DM_STATUS_TABLE_FLAG;
	return !ioctl(control, DM_TABLE_STATUS, dm) &&
		parse_table(buffer, sizeof(buffer), mapping, identity);
}

/* Read only a bounded sysfs text attribute; reject truncation and embedded NUL. */
static int read_text(const char *path, char *text, size_t capacity)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	size_t used = 0;
	int valid = 0;

	if (fd < 0)
		return 0;
	while (used < capacity - 1) {
		ssize_t count = read(fd, text + used, capacity - 1 - used);

		if (count < 0 && errno == EINTR)
			continue;
		if (count < 0)
			goto done;
		if (!count) {
			text[used] = 0;
			valid = used && !memchr(text, 0, used) && text[used - 1] == '\n';
			goto done;
		}
		used += (size_t)count;
	}
done:
	if (close(fd))
		valid = 0;
	return valid;
}

static int line_value(const char *text, const char *key, char *value,
		      size_t capacity)
{
	size_t key_bytes = strlen(key);
	int found = 0;

	while (*text) {
		const char *end = strchr(text, '\n');

		if (!end)
			return 0;
		if ((size_t)(end - text) >= key_bytes && !strncmp(text, key, key_bytes)) {
			size_t length = (size_t)(end - text) - key_bytes;

			if (found++ || !length || length >= capacity)
				return 0;
			memcpy(value, text + key_bytes, length);
			value[length] = 0;
		}
		text = end + 1;
	}
	return found == 1;
}

static int device_uevent(const char *text, dev_t device, const char *name,
			int partition)
{
	char expected[32], actual[128];

	snprintf(expected, sizeof(expected), "%u", major(device));
	if (!line_value(text, "MAJOR=", actual, sizeof(actual)) || strcmp(actual, expected))
		return 0;
	snprintf(expected, sizeof(expected), "%u", minor(device));
	if (!line_value(text, "MINOR=", actual, sizeof(actual)) || strcmp(actual, expected) ||
	    !line_value(text, "DEVNAME=", actual, sizeof(actual)) || strcmp(actual, name))
		return 0;
	return !partition ||
		(line_value(text, "PARTNAME=", actual, sizeof(actual)) &&
		 !strcmp(actual, "expdb") &&
		 line_value(text, "DEVTYPE=", actual, sizeof(actual)) &&
		 !strcmp(actual, "partition"));
}

static int check_uevent(dev_t device, const char *name, int partition)
{
	char path[128], text[4096];

	snprintf(path, sizeof(path), "/sys/dev/block/%u:%u/uevent",
		 major(device), minor(device));
	return read_text(path, text, sizeof(text)) &&
		device_uevent(text, device, name, partition);
}

static int backing_partition(dev_t device)
{
	char path[128], text[4096], name[128];
	struct stat st;
	uint64_t bytes;
	int fd, valid;

	snprintf(path, sizeof(path), "/sys/dev/block/%u:%u/uevent",
		 major(device), minor(device));
	if (!read_text(path, text, sizeof(text)) ||
	    !line_value(text, "DEVNAME=", name, sizeof(name)) ||
	    !r1_expdb_partition_name(name) ||
	    !device_uevent(text, device, name, 1))
		return 0;
	if (snprintf(path, sizeof(path), "/dev/%s", name) >= (int)sizeof(path))
		return 0;
	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return 0;
	valid = !fstat(fd, &st) && S_ISBLK(st.st_mode) && st.st_rdev == device &&
		!ioctl(fd, BLKGETSIZE64, &bytes) && bytes == R1_EXPDB_BYTES;
	if (close(fd))
		valid = 0;
	return valid;
}

static int pstore_stopped(void)
{
	struct stat st;

	return lstat("/sys/module/pstore_blk", &st) == -1 && errno == ENOENT;
}

static uint32_t little32(const unsigned char *p)
{
	return p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
		((uint32_t)p[3] << 24);
}

/* Parse the actual zone.c ring, including a marker split at the wrap point. */
static int console_has_marker(const unsigned char *zone, size_t bytes,
			      const char *marker)
{
	const size_t header = 12;
	size_t capacity, length, start, marker_bytes = strlen(marker), matches = 0;

	if (bytes != CONSOLE_BYTES || little32(zone) != CONSOLE_SIGNATURE)
		return 0;
	capacity = bytes - header;
	length = little32(zone + 4);
	start = little32(zone + 8);
	/* Negative little-endian s32 values also exceed this fixed capacity. */
	if (start > length || length > capacity ||
	    (length < capacity && start != length) || !marker_bytes || marker_bytes > length)
		return 0;
	if (length < capacity)
		start = 0;
	else if (start == capacity)
		start = 0;
	for (size_t i = 0; i <= length - marker_bytes; ++i) {
		size_t j;

		for (j = 0; j < marker_bytes; ++j)
			if (zone[header + (start + i + j) % capacity] != (unsigned char)marker[j])
				break;
		if (j == marker_bytes && ++matches > 1)
			return 0;
	}
	return matches == 1;
}

/* A short direct read is a failure: never stitch together a moving snapshot. */
static int checkpoint_readback(int fd, unsigned char *zone, const char *marker)
{
	ssize_t count;

	if (fsync(fd))
		return 0;
	do {
		count = pread(fd, zone, CONSOLE_BYTES, CONSOLE_OFFSET);
	} while (count < 0 && errno == EINTR);
	return count == CONSOLE_BYTES && console_has_marker(zone, CONSOLE_BYTES, marker);
}

static int checkpoint_main(int argc, char **argv)
{
	char marker[MARKER_BYTES];
	struct stat mapping, control_stat;
	struct table_identity before, after;
	uint64_t bytes;
	unsigned int dm_minor;
	unsigned char *zone = NULL;
	int fd = -1, control = -1, sector_size;
	int result = 1;
	const char *stage = "arguments";

	if (argc != 4 || !map_path(argv[1], &dm_minor) ||
	    !make_marker(marker, argv[2], argv[3]))
		goto done;
	stage = "pstore_blk must be unloaded";
	if (!pstore_stopped())
		goto done;
	stage = "direct mapping open";
	fd = open(argv[1], O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECT);
	if (fd < 0)
		goto done;
	stage = "mapping identity and size";
	if (fstat(fd, &mapping) || !S_ISBLK(mapping.st_mode) ||
	    minor(mapping.st_rdev) != dm_minor ||
	    ioctl(fd, BLKGETSIZE64, &bytes) || bytes != R1_EXPDB_LOG_BYTES ||
	    ioctl(fd, BLKSSZGET, &sector_size) || sector_size < 512 ||
	    sector_size > (int)DIRECT_ALIGNMENT || (sector_size & (sector_size - 1)) ||
	    !check_uevent(mapping.st_rdev, argv[1] + 5, 0))
		goto done;
	stage = "device mapper control open";
	control = open("/dev/mapper/control", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (control < 0 || fstat(control, &control_stat) ||
	    !S_ISCHR(control_stat.st_mode) || major(control_stat.st_rdev) != 10 ||
	    minor(control_stat.st_rdev) != 236)
		goto done;
	stage = "bounded live table and expdb backing";
	if (!live_table(control, mapping.st_rdev, &before) ||
	    !backing_partition(before.backing))
		goto done;
	stage = "aligned read buffer";
	if (posix_memalign((void **)&zone, DIRECT_ALIGNMENT, CONSOLE_BYTES))
		goto done;
	stage = "flush and direct console readback";
	if (!checkpoint_readback(fd, zone, marker))
		goto done;
	stage = "stable table and stopped logger after readback";
	if (!live_table(control, mapping.st_rdev, &after) ||
	    before.backing != after.backing || before.event != after.event ||
	    !backing_partition(after.backing) || !pstore_stopped())
		goto done;
	result = 0;
done:
	free(zone);
	if (control >= 0 && close(control)) {
		stage = "close device mapper control";
		result = 1;
	}
	if (fd >= 0 && close(fd)) {
		stage = "close mapping";
		result = 1;
	}
	if (result)
		fprintf(stderr, "expdb-checkpoint: failed: %s\n", stage);
	else
		puts("expdb-checkpoint: flush and direct marker readback verified");
	return result;
}

#ifndef EXPDB_CHECKPOINT_TEST
int main(int argc, char **argv)
{
	return checkpoint_main(argc, argv);
}
#endif
