// SPDX-License-Identifier: GPL-2.0-only
/* Exercise the production parser and entry point with syscall boundaries mocked. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/dm-ioctl.h>
#include <linux/fs.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static int fake_open(const char *, int, ...);
static int fake_close(int);
static int fake_fstat(int, struct stat *);
static int fake_lstat(const char *, struct stat *);
static int fake_ioctl(int, unsigned long, ...);
static ssize_t fake_read(int, void *, size_t);
static ssize_t fake_pread(int, void *, size_t, off_t);
static int fake_fsync(int);

#define open fake_open
#define close fake_close
#define fstat fake_fstat
#define lstat fake_lstat
#define ioctl fake_ioctl
#define read fake_read
#define pread fake_pread
#define fsync fake_fsync
#define EXPDB_CHECKPOINT_TEST
#include <expdb-checkpoint.c>
#undef open
#undef close
#undef fstat
#undef lstat
#undef ioctl
#undef read
#undef pread
#undef fsync

static const char boot_id[] = "91b6eb3d-ddca-4cd5-81db-47149ee3d779";
static const char release_id[] = "7.1.0-rabbit-r1-bringup+";
static char marker[MARKER_BYTES];
static _Alignas(4096) unsigned char fixture[CONSOLE_BYTES];
static unsigned int checks;

enum fault {
	NONE, MODULE_LOADED, MODULE_STAT_ERROR, MODULE_RELOADED,
	MAP_OPEN_ERROR, MAP_SYMLINK, MAP_REGULAR, MAP_MINOR, MAP_SIZE,
	MAP_SECTOR_SMALL, MAP_SECTOR_BIG, MAP_SECTOR_NOT_POWER_TWO,
	MAP_UEVENT, CONTROL_OPEN_ERROR, CONTROL_REGULAR, CONTROL_NUMBER,
	TABLE_IOCTL_ERROR, TABLE_INACTIVE, TABLE_SUSPENDED, TABLE_EXTRA,
	TABLE_WRONG_NAME, TABLE_CHANGED, TABLE_BACKING_CHANGED,
	BACKING_NAME, BACKING_PARTNAME, BACKING_MAJOR, BACKING_MINOR,
	BACKING_DUPLICATE, BACKING_EMBEDDED_NUL, BACKING_NO_NEWLINE,
	BACKING_TOO_LONG, BACKING_SYMLINK, BACKING_REGULAR,
	BACKING_NODE_NUMBER, BACKING_SIZE, BACKING_READ_ERROR,
	FLUSH_ERROR, READ_ERROR, READ_SHORT, READ_ZERO, READ_EINTR,
	MARKER_MISSING, MARKER_DUPLICATE, WRONG_SIGNATURE, BAD_RING_BOUNDS,
	CLOSE_MAPPING_ERROR, CLOSE_CONTROL_ERROR, CLOSE_BACKING_ERROR,
	CLOSE_SYSFS_ERROR
};

static enum fault active_fault;
static int opens, closes, table_queries, module_queries, flushes, reads;
static int map_live, control_live, backing_live, sysfs_live;
static size_t attribute_offset, attribute_size;
static char attribute[5000];

static void put32(unsigned char *p, uint32_t n)
{
	p[0] = (unsigned char)n;
	p[1] = (unsigned char)(n >> 8);
	p[2] = (unsigned char)(n >> 16);
	p[3] = (unsigned char)(n >> 24);
}

static void partial_ring(const char *text)
{
	size_t length = strlen(text);

	assert(length <= sizeof(fixture) - 12);
	memset(fixture, 0, sizeof(fixture));
	put32(fixture, CONSOLE_SIGNATURE);
	put32(fixture + 4, (uint32_t)length);
	put32(fixture + 8, (uint32_t)length);
	memcpy(fixture + 12, text, length);
}

static void full_ring(size_t start, size_t position)
{
	size_t cap = sizeof(fixture) - 12;

	assert(position + strlen(marker) <= cap);
	memset(fixture, 'X', sizeof(fixture));
	put32(fixture, CONSOLE_SIGNATURE);
	put32(fixture + 4, (uint32_t)cap);
	put32(fixture + 8, (uint32_t)start);
	for (size_t i = 0; i < strlen(marker); ++i)
		fixture[12 + (start + position + i) % cap] = (unsigned char)marker[i];
}

static void table_fixture(unsigned char *buf)
{
	struct dm_ioctl *dm = (void *)buf;
	struct dm_target_spec *target;
	const char params[] = "179:7 0";

	memset(buf, 0, R1_EXPDB_DM_BUFFER_SIZE);
	dm->version[0] = DM_VERSION_MAJOR;
	dm->data_start = sizeof(*dm);
	dm->data_size = sizeof(*dm) + sizeof(*target) + sizeof(params);
	dm->target_count = 1;
	dm->dev = (253U << 8) | 3;
	dm->flags = DM_ACTIVE_PRESENT_FLAG | DM_STATUS_TABLE_FLAG;
	dm->event_nr = 7;
	strcpy(dm->name, "r1-expdb");
	target = (void *)(buf + sizeof(*dm));
	target->length = 36864;
	target->next = (sizeof(*target) + sizeof(params) + 7) & ~(size_t)7;
	strcpy(target->target_type, "linear");
	memcpy(target + 1, params, sizeof(params));
}

static int fake_open(const char *path, int flags, ...)
{
	++opens;
	assert((flags & O_ACCMODE) == O_RDONLY);
	assert(flags & O_CLOEXEC);
	assert(flags & O_NOFOLLOW);
	assert(!(flags & (O_CREAT | O_TRUNC | O_APPEND)));
	if (!strcmp(path, "/dev/dm-3")) {
		assert(flags & O_DIRECT);
		assert(!map_live);
		if (active_fault == MAP_OPEN_ERROR || active_fault == MAP_SYMLINK) {
			errno = active_fault == MAP_SYMLINK ? ELOOP : EACCES;
			return -1;
		}
		map_live = 1;
		return 10;
	}
	if (!strcmp(path, "/dev/mapper/control")) {
		assert(!(flags & O_DIRECT));
		assert(!control_live);
		if (active_fault == CONTROL_OPEN_ERROR) {
			errno = EACCES;
			return -1;
		}
		control_live = 1;
		return 11;
	}
	if (!strcmp(path, "/dev/mmcblk0p7")) {
		assert(!backing_live);
		if (active_fault == BACKING_SYMLINK) {
			errno = ELOOP;
			return -1;
		}
		backing_live = 1;
		return 12;
	}
	assert(!sysfs_live);
	attribute_offset = 0;
	if (!strcmp(path, "/sys/dev/block/253:3/uevent")) {
		strcpy(attribute, "MAJOR=253\nMINOR=3\nDEVNAME=dm-3\nDEVTYPE=disk\n");
		if (active_fault == MAP_UEVENT)
			strcpy(attribute, "MAJOR=253\nMINOR=3\nDEVNAME=dm-4\n");
	} else {
		assert(!strcmp(path, "/sys/dev/block/179:7/uevent"));
		strcpy(attribute, "MAJOR=179\nMINOR=7\nDEVNAME=mmcblk0p7\nDEVTYPE=partition\nPARTNAME=expdb\n");
		switch (active_fault) {
		case BACKING_NAME:
			strcpy(attribute, "MAJOR=179\nMINOR=7\nDEVNAME=sda7\nDEVTYPE=partition\nPARTNAME=expdb\n");
			break;
		case BACKING_PARTNAME:
			strcat(attribute, "PARTNAME=expdb_b\n");
			break;
		case BACKING_MAJOR:
			attribute[8] = '8';
			break;
		case BACKING_MINOR:
			attribute[16] = '8';
			break;
		case BACKING_DUPLICATE:
			strcat(attribute, "DEVNAME=mmcblk0p7\n");
			break;
		case BACKING_NO_NEWLINE:
			attribute[strlen(attribute) - 1] = 0;
			break;
		case BACKING_TOO_LONG:
			memset(attribute, 'X', sizeof(attribute) - 1);
			attribute[sizeof(attribute) - 1] = 0;
			break;
		default:
			break;
		}
	}
	attribute_size = strlen(attribute);
	if (active_fault == BACKING_EMBEDDED_NUL)
		attribute[3] = 0;
	sysfs_live = 1;
	return 13;
}

static int fake_close(int fd)
{
	++closes;
	switch (fd) {
	case 10:
		assert(map_live);
		map_live = 0;
		return active_fault == CLOSE_MAPPING_ERROR ? -1 : 0;
	case 11:
		assert(control_live);
		control_live = 0;
		return active_fault == CLOSE_CONTROL_ERROR ? -1 : 0;
	case 12:
		assert(backing_live);
		backing_live = 0;
		return active_fault == CLOSE_BACKING_ERROR ? -1 : 0;
	case 13:
		assert(sysfs_live);
		sysfs_live = 0;
		return active_fault == CLOSE_SYSFS_ERROR ? -1 : 0;
	default:
		assert(0);
	}
	return -1;
}

static int fake_fstat(int fd, struct stat *st)
{
	memset(st, 0, sizeof(*st));
	if (fd == 10) {
		assert(map_live);
		st->st_mode = active_fault == MAP_REGULAR ? S_IFREG : S_IFBLK;
		st->st_rdev = makedev(253, active_fault == MAP_MINOR ? 4 : 3);
	} else if (fd == 11) {
		assert(control_live);
		st->st_mode = active_fault == CONTROL_REGULAR ? S_IFREG : S_IFCHR;
		st->st_rdev = makedev(10, active_fault == CONTROL_NUMBER ? 235 : 236);
	} else {
		assert(fd == 12 && backing_live);
		st->st_mode = active_fault == BACKING_REGULAR ? S_IFREG : S_IFBLK;
		st->st_rdev = makedev(179, active_fault == BACKING_NODE_NUMBER ? 8 : 7);
	}
	return 0;
}

static int fake_lstat(const char *path, struct stat *st)
{
	(void)st;
	assert(!strcmp(path, "/sys/module/pstore_blk"));
	++module_queries;
	if (active_fault == MODULE_LOADED ||
	    (active_fault == MODULE_RELOADED && module_queries == 2))
		return 0;
	errno = active_fault == MODULE_STAT_ERROR ? EACCES : ENOENT;
	return -1;
}

static int fake_ioctl(int fd, unsigned long request, ...)
{
	va_list args;
	void *data;

	va_start(args, request);
	data = va_arg(args, void *);
	va_end(args);
	if (request == BLKGETSIZE64) {
		assert(fd == 10 || fd == 12);
		*(uint64_t *)data = fd == 10 ? R1_EXPDB_LOG_BYTES : R1_EXPDB_BYTES;
		if ((fd == 10 && active_fault == MAP_SIZE) ||
		    (fd == 12 && active_fault == BACKING_SIZE))
			*(uint64_t *)data += 512;
		return 0;
	}
	if (request == BLKSSZGET) {
		assert(fd == 10 && map_live);
		*(int *)data = active_fault == MAP_SECTOR_SMALL ? 256 :
			active_fault == MAP_SECTOR_BIG ? 8192 :
			active_fault == MAP_SECTOR_NOT_POWER_TWO ? 768 : 512;
		return 0;
	}
	/* Any modifying ioctl, including suspend/reload, fails this assertion. */
	assert(request == DM_TABLE_STATUS && fd == 11 && control_live);
	struct dm_ioctl *dm = data;
	assert(dm->flags == DM_STATUS_TABLE_FLAG && dm->dev == ((253U << 8) | 3));
	assert(dm->data_size == R1_EXPDB_DM_BUFFER_SIZE);
	assert(!dm->name[0] && !dm->uuid[0]);
	++table_queries;
	if (active_fault == TABLE_IOCTL_ERROR)
		return -1;
	table_fixture(data);
	if (active_fault == TABLE_INACTIVE)
		dm->flags |= DM_INACTIVE_PRESENT_FLAG;
	if (active_fault == TABLE_SUSPENDED)
		dm->flags |= DM_SUSPEND_FLAG;
	if (active_fault == TABLE_EXTRA)
		dm->target_count = 2;
	if (active_fault == TABLE_WRONG_NAME)
		strcpy(dm->name, "other-expdb");
	if (active_fault == TABLE_CHANGED && table_queries == 2)
		++dm->event_nr;
	if (active_fault == TABLE_BACKING_CHANGED && table_queries == 2) {
		struct dm_target_spec *target = (void *)((unsigned char *)data + dm->data_start);
		strcpy((char *)(target + 1), "179:8 0");
	}
	return 0;
}

static ssize_t fake_read(int fd, void *buf, size_t count)
{
	assert(fd == 13 && sysfs_live);
	if (active_fault == BACKING_READ_ERROR) {
		errno = EIO;
		return -1;
	}
	if (count > attribute_size - attribute_offset)
		count = attribute_size - attribute_offset;
	/* Exercise short sysfs reads in every case. */
	if (count > 17)
		count = 17;
	memcpy(buf, attribute + attribute_offset, count);
	attribute_offset += count;
	return (ssize_t)count;
}

static int fake_fsync(int fd)
{
	assert(fd == 10 && map_live);
	assert(!backing_live && !sysfs_live);
	assert(table_queries == 1 && !reads);
	++flushes;
	if (active_fault == FLUSH_ERROR) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static ssize_t fake_pread(int fd, void *buf, size_t count, off_t offset)
{
	assert(fd == 10 && map_live);
	assert(count == CONSOLE_BYTES && offset == CONSOLE_OFFSET);
	assert((uintptr_t)buf % DIRECT_ALIGNMENT == 0 && flushes == 1);
	++reads;
	if (active_fault == READ_ERROR || (active_fault == READ_EINTR && reads == 1)) {
		errno = active_fault == READ_ERROR ? EIO : EINTR;
		return -1;
	}
	if (active_fault == READ_ZERO)
		return 0;
	memcpy(buf, fixture, count);
	return active_fault == READ_SHORT ? (ssize_t)count - 512 : (ssize_t)count;
}

static void expect(int condition)
{
	assert(condition);
	++checks;
}

static void reset(enum fault fault)
{
	assert(!map_live && !control_live && !backing_live && !sysfs_live);
	active_fault = fault;
	opens = closes = table_queries = module_queries = flushes = reads = 0;
	partial_ring(marker);
	if (fault == MARKER_MISSING)
		partial_ring("Linux booted but no checkpoint\n");
	if (fault == MARKER_DUPLICATE) {
		char duplicate[MARKER_BYTES * 2];
		snprintf(duplicate, sizeof(duplicate), "%s%s", marker, marker);
		partial_ring(duplicate);
	}
	if (fault == WRONG_SIGNATURE)
		put32(fixture, 0);
	if (fault == BAD_RING_BOUNDS)
		put32(fixture + 4, UINT32_MAX);
}

static void test_entry(void)
{
	char *argv[] = {"expdb-checkpoint", "/dev/dm-3", (char *)boot_id, (char *)release_id};

	reset(NONE);
	expect(checkpoint_main(4, argv) == 0);
	expect(table_queries == 2 && module_queries == 2 && flushes == 1 && reads == 1);
	expect(opens == closes);
	for (enum fault f = MODULE_LOADED; f <= CLOSE_SYSFS_ERROR; ++f) {
		reset(f);
		int result = checkpoint_main(4, argv);

		if (f == READ_EINTR) {
			expect(result == 0);
			expect(reads == 2);
		} else {
			if (result == 0)
				fprintf(stderr, "Fault accepted: %d\n", f);
			expect(result != 0);
		}
		expect(!map_live && !control_live && !backing_live && !sysfs_live);
		if (f == FLUSH_ERROR)
			expect(!reads);
		if (f == READ_SHORT)
			expect(reads == 1);
	}
	reset(NONE);
	expect(checkpoint_main(3, argv) != 0 && !opens && !module_queries);
	argv[1] = "/dev/mapper/r1-expdb";
	expect(checkpoint_main(4, argv) != 0 && !opens && !module_queries);
	argv[1] = "/dev/dm-3";
	argv[2] = "91b6eb3d-ddca-4cd5-81db-47149ee3d779\n";
	expect(checkpoint_main(4, argv) != 0 && !opens && !module_queries);
	argv[2] = (char *)boot_id;
	argv[3] = "7.1.0;poweroff";
	expect(checkpoint_main(4, argv) != 0 && !opens && !module_queries);
}

static void test_arguments(void)
{
	unsigned int number;
	char out[MARKER_BYTES], long_release[130];
	const char *bad_paths[] = {"", "/dev/dm-", "/dev/dm-03", "/dev/dm--3",
		"/dev/dm-3/../dm-4", "/dev/dm-3\n", "/dev/dm-4294967296", "dm-3"};
	const char *bad_uuids[] = {"", "91B6eb3d-ddca-4cd5-81db-47149ee3d779",
		"91b6eb3d_ddca-4cd5-81db-47149ee3d779", "91b6eb3d-ddca-4cd5-81db-47149ee3d77",
		"91b6eb3d-ddca-4cd5-81db-47149ee3d77g", "91b6eb3d-ddca-4cd5-81db-47149ee3d779x"};
	const char *bad_release[] = {"", "kernel release", "7.1\n", "$(poweroff)", "kernel/../other", "7.1\t"};

	expect(map_path("/dev/dm-0", &number) && number == 0);
	expect(map_path("/dev/dm-1234", &number) && number == 1234);
	for (size_t i = 0; i < sizeof(bad_paths) / sizeof(*bad_paths); ++i)
		expect(!map_path(bad_paths[i], &number));
	for (size_t i = 0; i < sizeof(bad_uuids) / sizeof(*bad_uuids); ++i)
		expect(!make_marker(out, bad_uuids[i], release_id));
	for (size_t i = 0; i < sizeof(bad_release) / sizeof(*bad_release); ++i)
		expect(!make_marker(out, boot_id, bad_release[i]));
	memset(long_release, 'a', sizeof(long_release));
	long_release[128] = 0;
	expect(make_marker(out, boot_id, long_release));
	long_release[128] = 'a';
	long_release[129] = 0;
	expect(!make_marker(out, boot_id, long_release));
	expect(make_marker(out, boot_id, release_id) && !strcmp(out, marker));
}

static void test_ring(void)
{
	const size_t cap = CONSOLE_BYTES - 12;
	char longer[MARKER_BYTES * 2];

	partial_ring(marker);
	expect(console_has_marker(fixture, sizeof(fixture), marker));
	expect(!console_has_marker(fixture, sizeof(fixture) - 1, marker));
	expect(!console_has_marker(fixture, sizeof(fixture), ""));
	expect(!console_has_marker(fixture, sizeof(fixture), "r1: expdb shutdown checkpoint wrong-boot\n"));
	snprintf(longer, sizeof(longer), "[ 0.1] Linux\n[ 31.5] %stail\n", marker);
	partial_ring(longer);
	expect(console_has_marker(fixture, sizeof(fixture), marker));
	partial_ring("");
	expect(!console_has_marker(fixture, sizeof(fixture), marker));
	full_ring(0, 0);
	expect(console_has_marker(fixture, sizeof(fixture), marker));
	full_ring(cap, cap - strlen(marker));
	expect(console_has_marker(fixture, sizeof(fixture), marker));
	full_ring(cap - 10, 0);
	expect(console_has_marker(fixture, sizeof(fixture), marker));
	/* Every possible physical split within the complete marker. */
	for (size_t split = 1; split < strlen(marker); ++split) {
		full_ring(cap - split, 0);
		expect(console_has_marker(fixture, sizeof(fixture), marker));
	}
	/* Joining newest and oldest bytes is not a chronological marker. */
	full_ring(0, 0);
	memset(fixture + 12, 'X', cap);
	memcpy(fixture + 12 + cap - 10, marker, 10);
	memcpy(fixture + 12, marker + 10, strlen(marker) - 10);
	expect(!console_has_marker(fixture, sizeof(fixture), marker));
	uint32_t bad[][2] = {{UINT32_MAX, 0}, {0, UINT32_MAX}, {3, 4}, {3, 2},
		{(uint32_t)cap + 1, 0}, {(uint32_t)cap, (uint32_t)cap + 1}};
	for (size_t i = 0; i < sizeof(bad) / sizeof(*bad); ++i) {
		partial_ring(marker);
		put32(fixture + 4, bad[i][0]);
		put32(fixture + 8, bad[i][1]);
		expect(!console_has_marker(fixture, sizeof(fixture), marker));
	}
	partial_ring(marker);
	put32(fixture, CONSOLE_SIGNATURE ^ 1);
	expect(!console_has_marker(fixture, sizeof(fixture), marker));
	snprintf(longer, sizeof(longer), "%s%s", marker, marker);
	partial_ring(longer);
	expect(!console_has_marker(fixture, sizeof(fixture), marker));
	partial_ring(marker);
	fixture[12 + strlen(marker) - 1] = ' ';
	expect(!console_has_marker(fixture, sizeof(fixture), marker));
}

static void test_table(void)
{
	_Alignas(8) unsigned char buf[R1_EXPDB_DM_BUFFER_SIZE];
	struct dm_ioctl *dm = (void *)buf;
	struct dm_target_spec *target = (void *)(buf + sizeof(*dm));
	struct table_identity identity;
	dev_t mapping = makedev(253, 3);
	uint32_t bad_flags[] = {0, DM_STATUS_TABLE_FLAG,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_INACTIVE_PRESENT_FLAG,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_SUSPEND_FLAG,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_INTERNAL_SUSPEND_FLAG,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_BUFFER_FULL_FLAG,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_READONLY_FLAG,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_DEFERRED_REMOVE,
		DM_STATUS_TABLE_FLAG | DM_ACTIVE_PRESENT_FLAG | DM_QUERY_INACTIVE_TABLE_FLAG};

	table_fixture(buf);
	expect(parse_table(buf, sizeof(buf), mapping, &identity));
	expect(identity.backing == makedev(179, 7) && identity.event == 7);
	expect(!parse_table(buf, sizeof(*dm) - 1, mapping, &identity));
	for (size_t i = 0; i < sizeof(bad_flags) / sizeof(*bad_flags); ++i) {
		table_fixture(buf);
		dm->flags = bad_flags[i];
		expect(!parse_table(buf, sizeof(buf), mapping, &identity));
	}
#define BAD_TABLE(change) do { table_fixture(buf); change; expect(!parse_table(buf, sizeof(buf), mapping, &identity)); } while (0)
	BAD_TABLE(dm->version[0]++);
	BAD_TABLE(dm->data_start = UINT32_MAX);
	BAD_TABLE(dm->data_start++);
	BAD_TABLE(dm->data_start = sizeof(*dm) - 8);
	BAD_TABLE(dm->data_size = sizeof(buf) + 1);
	BAD_TABLE(dm->data_size = dm->data_start + sizeof(*target));
	BAD_TABLE(dm->data_size++);
	BAD_TABLE(dm->target_count = 0);
	BAD_TABLE(dm->target_count = 2);
	BAD_TABLE(dm->dev++);
	BAD_TABLE(strcpy(dm->name, "other"));
	BAD_TABLE(memset(dm->name, 'X', sizeof(dm->name)));
	BAD_TABLE(strcpy(dm->uuid, "unapproved-map"));
	BAD_TABLE(target->sector_start = 1);
	BAD_TABLE(target->length--);
	BAD_TABLE(target->length = 40960);
	BAD_TABLE(target->status = 1);
	BAD_TABLE(strcpy(target->target_type, "striped"));
	BAD_TABLE(memset(target->target_type, 'X', sizeof(target->target_type)));
	BAD_TABLE(target->next = 0);
	BAD_TABLE(target->next += 8);
	BAD_TABLE(memset(target + 1, 'X', 8));
	BAD_TABLE(strcpy((char *)(target + 1), "179:7 1"));
	BAD_TABLE(strcpy((char *)(target + 1), "179:7\t0"));
	BAD_TABLE(strcpy((char *)(target + 1), "x79:7 0"));
#undef BAD_TABLE
}

int main(void)
{
	assert(make_marker(marker, boot_id, release_id));
	test_arguments();
	test_ring();
	test_table();
	test_entry();
	printf("PASS: %u checks; production parser and full helper entry point\n", checks);
	return 0;
}
