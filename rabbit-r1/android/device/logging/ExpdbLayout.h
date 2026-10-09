/* SPDX-License-Identifier: GPL-2.0-only OR Apache-2.0 */
#ifndef R1_EXPDB_LAYOUT_H
#define R1_EXPDB_LAYOUT_H

#include <linux/dm-ioctl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/sysmacros.h>
#include <sys/types.h>

#define R1_EXPDB_BYTES (20ULL * 1024 * 1024)
#define R1_EXPDB_LOG_BYTES (18ULL * 1024 * 1024)
#define R1_EXPDB_LOG_SECTORS (R1_EXPDB_LOG_BYTES / 512)
#define R1_EXPDB_MAP_NAME "r1-expdb"
#define R1_EXPDB_DM_BUFFER_SIZE 4096

static inline int r1_expdb_partition_name(const char *name)
{
    if (strncmp(name, "mmcblk", 6)) return 0;
    name += 6;
    if (*name < '0' || *name > '9') return 0;
    while (*name >= '0' && *name <= '9') ++name;
    if (*name++ != 'p' || *name < '0' || *name > '9') return 0;
    while (*name >= '0' && *name <= '9') ++name;
    return !*name;
}

/* The caller supplies an aligned, zeroed ioctl buffer with data_start set. */
static inline int r1_expdb_linear_table(struct dm_ioctl *dm, size_t capacity, dev_t backing)
{
    struct dm_target_spec *target;
    char *params;
    size_t available;
    int count;
    if (dm->data_start < sizeof(*dm) || dm->data_start % 8 ||
        dm->data_start > capacity || capacity - dm->data_start < sizeof(*target) + 32)
        return 0;
    target = (struct dm_target_spec *)((unsigned char *)dm + dm->data_start);
    available = capacity - dm->data_start - sizeof(*target);
    params = (char *)(target + 1);
    memset(target, 0, sizeof(*target));
    target->length = R1_EXPDB_LOG_SECTORS;
    strcpy(target->target_type, "linear");
    count = snprintf(params, available, "%u:%u 0", major(backing), minor(backing));
    if (count < 0 || (size_t)count >= available) return 0;
    target->next = (sizeof(*target) + (size_t)count + 1 + 7) & ~(size_t)7;
    if (target->next > capacity - dm->data_start) return 0;
    dm->target_count = 1;
    return 1;
}
#endif
