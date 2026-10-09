// SPDX-License-Identifier: GPL-2.0-only
/* Target-only helper: expose the first 18 MiB of the r1's 20 MiB expdb. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
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

#define EXPDB_BYTES R1_EXPDB_BYTES
#define LOG_BYTES R1_EXPDB_LOG_BYTES
#define MAP_NAME R1_EXPDB_MAP_NAME

static int partition_name(const char *name)
{
	return r1_expdb_partition_name(name);
}

static int has_line(const char *file, const char *expected)
{
	char line[256];
	FILE *fp = fopen(file, "r");
	int found = 0;

	if (!fp)
		return 0;
	while (fgets(line, sizeof(line), fp))
		if (!strcmp(line, expected))
			found = 1;
	fclose(fp);
	return found;
}

static struct dm_ioctl *request(unsigned char *buf)
{
	struct dm_ioctl *dm = (void *)buf;

	memset(buf, 0, 4096);
	dm->version[0] = DM_VERSION_MAJOR;
	dm->version[1] = DM_VERSION_MINOR;
	dm->version[2] = DM_VERSION_PATCHLEVEL;
	dm->data_size = 4096;
	dm->data_start = sizeof(*dm);
	strcpy(dm->name, MAP_NAME);
	return dm;
}

static int linear_table(unsigned char *buf, dev_t dev)
{
	struct dm_ioctl *dm = request(buf);

	return r1_expdb_linear_table(dm, 4096, dev);
}

#ifndef EXPDB_TEST
int main(int argc, char **argv)
{
	_Alignas(8) unsigned char buf[4096];
	char path[256], expected[128];
	struct stat st, mapped;
	struct dm_ioctl *dm;
	uint64_t bytes;
	int fd, ctl, saved;
	unsigned int dm_minor;

	if (argc != 2 || !partition_name(argv[1])) {
		fprintf(stderr, "Usage: expdb-map mmcblkNpN (target only)\n");
		return 1;
	}
	/* Check the GPT label and the node's device number, not its index. */
	snprintf(path, sizeof(path), "/sys/class/block/%s/uevent", argv[1]);
	if (!has_line(path, "PARTNAME=expdb\n")) {
		fprintf(stderr, "Refusing a partition not labelled expdb\n");
		return 1;
	}
	snprintf(expected, sizeof(expected), "DEVNAME=%s\n", argv[1]);
	if (!has_line(path, expected))
		return 1;
	snprintf(path, sizeof(path), "/dev/%s", argv[1]);
	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_EXCL);
	if (fd < 0) {
		perror("open expdb");
		return 1;
	}
	if (fstat(fd, &st) || !S_ISBLK(st.st_mode) ||
	    ioctl(fd, BLKGETSIZE64, &bytes) || bytes != EXPDB_BYTES) {
		fprintf(stderr, "expdb must be an unused 20 MiB block partition\n");
		close(fd);
		return 1;
	}
	snprintf(path, sizeof(path), "/sys/class/block/%s/dev", argv[1]);
	snprintf(expected, sizeof(expected), "%u:%u\n", major(st.st_rdev), minor(st.st_rdev));
	if (!has_line(path, expected)) {
		close(fd);
		return 1;
	}
	close(fd);
	ctl = open("/dev/mapper/control", O_RDWR | O_CLOEXEC);
	if (ctl < 0) {
		perror("open device mapper");
		return 1;
	}
	dm = request(buf);
	if (ioctl(ctl, DM_DEV_CREATE, dm)) {
		perror("create r1-expdb mapping");
		close(ctl);
		return 1;
	}
	dm_minor = minor(dm->dev);
	if (!linear_table(buf, st.st_rdev)) {
		errno = EINVAL;
		goto fail;
	}
	if (ioctl(ctl, DM_TABLE_LOAD, buf))
		goto fail;
	dm = request(buf); /* Clear DM_SUSPEND_FLAG to activate the table. */
	if (ioctl(ctl, DM_DEV_SUSPEND, dm))
		goto fail;
	snprintf(path, sizeof(path), "/dev/dm-%u", dm_minor);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		goto fail;
	if (fstat(fd, &mapped) || !S_ISBLK(mapped.st_mode) ||
	    mapped.st_rdev != dm->dev || ioctl(fd, BLKGETSIZE64, &bytes) || bytes != LOG_BYTES) {
		close(fd);
		errno = EINVAL;
		goto fail;
	}
	close(fd);
	close(ctl);
	puts(path);
	return 0;
fail:
	saved = errno;
	dm = request(buf);
	ioctl(ctl, DM_DEV_REMOVE, dm);
	close(ctl);
	errno = saved;
	perror("configure bounded expdb mapping");
	return 1;
}
#endif
