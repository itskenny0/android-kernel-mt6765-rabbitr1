// SPDX-License-Identifier: Apache-2.0
// Exercise the real LinuxStorage syscall boundary without opening host devices.
// Open()/sysfs discovery and SELinux still require Android/device validation.
#include "LinuxStorage.h"

#include <algorithm>
#include <array>
#include <cassert>
#include <cerrno>
#include <cstdarg>
#include <cstring>
#include <iostream>
#include <linux/mmc/ioctl.h>
#include <sys/ioctl.h>
#include <sys/types.h>

using namespace haretic::boot;
static std::array<uint8_t, 0x80000> media;
static std::array<uint8_t, 0x80000> cache;
static unsigned reads, writes, syncs, ext_reads, switches;
static bool short_io, interrupt_once, zero_io, sync_error, corrupt_readback;
static bool short_read, drop_write, corrupt_media, read_error, sync_interrupt;
static bool ext_error, switch_error, r1_error;
static uint8_t boot_config;

extern "C" ssize_t __wrap_pread(int fd, void* data, size_t size, off_t offset) {
    assert(fd == -1 && offset == 0 && size == 4096);
    assert(reinterpret_cast<uintptr_t>(data) % 4096 == 0);
    ++reads;
    if (interrupt_once) { interrupt_once = false; errno = EINTR; return -1; }
    if (zero_io) return 0;
    if (read_error) { errno = EIO; return -1; }
    const size_t count = short_read ? 2049 : size;
    memcpy(data, media.data() + offset, count);
    if (corrupt_readback) static_cast<uint8_t*>(data)[2048] ^= 1;
    return count;
}

extern "C" ssize_t __wrap_pwrite(int fd, const void* data, size_t size, off_t offset) {
    assert(fd == -1 && offset >= 2048 && offset + size <= 2080);
    ++writes;
    if (interrupt_once) { interrupt_once = false; errno = EINTR; return -1; }
    if (zero_io) return 0;
    const size_t count = short_io ? std::min(size, size_t{3}) : size;
    memcpy(cache.data() + offset, data, count);
    return count;
}

extern "C" int __wrap_fsync(int fd) {
    assert(fd == -1);
    ++syncs;
    if (sync_interrupt) { sync_interrupt = false; errno = EINTR; return -1; }
    if (sync_error) { errno = EIO; return -1; }
    if (!drop_write) media = cache;
    if (corrupt_media) media[2048] ^= 1;
    return 0;
}

extern "C" int __wrap_ioctl(int fd, unsigned long request, ...) {
    assert(fd == -1 && request == MMC_IOC_CMD);
    va_list args;
    va_start(args, request);
    auto* cmd = va_arg(args, mmc_ioc_cmd*);
    va_end(args);
    assert(!cmd->write_flag && !cmd->is_acmd);
    if (cmd->opcode == 8) {
        ++ext_reads;
        assert(cmd->arg == 0 && cmd->flags == 0x35);
        assert(cmd->blksz == 512 && cmd->blocks == 1);
        if (ext_error) { errno = EIO; return -1; }
        auto* data = reinterpret_cast<uint8_t*>(cmd->data_ptr);
        memset(data, 0, 512);
        data[179] = boot_config;
    } else {
        ++switches;
        assert(cmd->opcode == 6 && cmd->flags == 0x1d);
        assert(cmd->arg >> 16 == 0x3b3 && (cmd->arg & 255) == 1);
        assert(!cmd->blksz && !cmd->blocks && !cmd->data_ptr);
        assert(cmd->cmd_timeout_ms == 1000);
        if (switch_error) { errno = ETIMEDOUT; return -1; }
        if (r1_error) { cmd->response[0] = 128; return 0; }
        boot_config = (cmd->arg >> 8) & 255;
    }
    return 0;
}

static void Reset() {
    reads = writes = syncs = ext_reads = switches = 0;
    short_io = interrupt_once = zero_io = sync_error = corrupt_readback = false;
    short_read = drop_write = corrupt_media = read_error = sync_interrupt = false;
    ext_error = switch_error = r1_error = false;
    boot_config = 0x48;
    media.fill(0xa5);
    cache = media;
}

int main() {
    LinuxStorage storage;  // Fds stay -1; every used syscall is wrapped above.
    Record record {};
    record[4] = 'B'; record[5] = 'C'; record[6] = 'A'; record[7] = 'B';
    record[8] = 1; record[9] = 2; record[12] = 0x7f; record[14] = 0x7e;
    Control::Seal(&record);
    unsigned cases = 0;
    for (bool partial : {false, true}) for (bool interrupted : {false, true}) {
        Reset(); short_io = partial; interrupt_once = interrupted;
        assert(storage.WriteRecord(record));
        assert(syncs == 1 && reads && writes);
        assert(std::all_of(media.begin(), media.begin() + 2048, [](auto b) { return b == 0xa5; }));
        assert(std::all_of(media.begin() + 2080, media.end(), [](auto b) { return b == 0xa5; }));
        assert(std::equal(record.begin(), record.end(), media.begin() + 2048));
        Record readback;
        interrupt_once = interrupted;
        assert(storage.ReadRecord(&readback) && record == readback);
        ++cases;
    }
    Reset(); zero_io = true;
    assert(!storage.WriteRecord(record) && syncs == 0 && reads == 0);
    assert(!storage.ReadRecord(&record));
    ++cases;
    Reset(); sync_error = true;
    assert(!storage.WriteRecord(record) && syncs == 1 && reads == 0);
    ++cases;
    Reset(); corrupt_readback = true;
    assert(!storage.WriteRecord(record) && syncs == 1 && reads == 1);
    ++cases;
    Reset(); drop_write = true;
    assert(!storage.WriteRecord(record));
    assert(std::equal(record.begin(), record.end(), cache.begin() + 2048));
    assert(media[2048] == 0xa5);  // Cached readback would have falsely passed.
    ++cases;
    Reset(); corrupt_media = true;
    assert(!storage.WriteRecord(record));
    assert(std::equal(record.begin(), record.end(), cache.begin() + 2048));
    ++cases;
    Reset(); short_read = true;
    assert(!storage.ReadRecord(&record) && reads == 1);
    assert(!storage.WriteRecord(record) && reads == 2);
    ++cases;
    Reset(); read_error = true;
    assert(!storage.ReadRecord(&record));
    assert(!storage.WriteRecord(record));
    ++cases;
    Reset(); sync_interrupt = true;
    assert(storage.WriteRecord(record) && syncs == 2);
    ++cases;
    Reset(); record[0] ^= 1;
    assert(!storage.WriteRecord(record) && !writes && !syncs && !reads);
    record[0] ^= 1;
    ++cases;

    for (unsigned before = 0; before < 256; ++before) for (unsigned region : {1U, 2U}) {
        Reset(); boot_config = before;
        const uint8_t target = (before & ~0x38) | (region << 3);
        assert(storage.WriteBootConfig(target));
        assert(boot_config == target && ext_reads == 1 && switches == 1);
        uint8_t readback = 0;
        assert(storage.ReadBootConfig(&readback) && readback == target);
        assert(!writes && !syncs);  // No raw disk or metadata writes.
        ++cases;
    }
    Reset();
    assert(!storage.WriteBootConfig(0x08));  // Would clear BOOT_ACK.
    assert(ext_reads == 1 && !switches && boot_config == 0x48);
    ++cases;
    for (unsigned region : {0U, 3U, 4U, 5U, 6U, 7U}) {
        Reset();
        assert(!storage.WriteBootConfig(0x40 | (region << 3)) && !ext_reads && !switches);
        ++cases;
    }
    Reset(); ext_error = true;
    assert(!storage.WriteBootConfig(0x50) && !switches);
    ++cases;
    Reset(); switch_error = true;
    assert(!storage.WriteBootConfig(0x50) && boot_config == 0x48);
    ++cases;
    Reset(); r1_error = true;
    assert(!storage.WriteBootConfig(0x50) && boot_config == 0x48);
    ++cases;
    std::cout << "PASS: " << cases << " Linux storage boundary cases\n";
}
