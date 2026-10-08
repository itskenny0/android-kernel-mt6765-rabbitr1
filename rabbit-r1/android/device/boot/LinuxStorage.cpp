// SPDX-License-Identifier: Apache-2.0
#include "LinuxStorage.h"

#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <linux/fs.h>
#include <linux/mmc/ioctl.h>
#include <sstream>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

namespace haretic::boot {
namespace {
constexpr off_t kRecordOffset = 2048;
// Covers the record in one transfer, aligned for both 512- and 4096-byte
// logical sectors. Only reads use this range; writes stay at 32 bytes.
constexpr size_t kReadSize = 4096;
constexpr uint64_t kParaSize = 0x80000;
constexpr uint8_t kBootMask = 0x38;
constexpr uint32_t kR1SwitchError = 1U << 7;

bool ReadText(const std::string& path, std::string* output) {
    std::ifstream stream(path);
    if (!stream) return false;
    std::getline(stream, *output);
    return !stream.bad() && !output->empty();
}

bool Write(int fd, const void* data, size_t size) {
    const auto* bytes = static_cast<const uint8_t*>(data);
    size_t done = 0;
    while (done < size) {
        const ssize_t count = pwrite(fd, bytes + done, size - done, kRecordOffset + done);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) {
            if (!count) errno = EIO;
            return false;
        }
        done += count;
    }
    return true;
}
}  // namespace

LinuxStorage::~LinuxStorage() {
    if (emmc_ >= 0) close(emmc_);
    if (misc_read_ >= 0) close(misc_read_);
    if (misc_ >= 0) close(misc_);
}

bool LinuxStorage::Fail(const char* operation) {
    error_ = std::string(operation) + ": " + strerror(errno);
    return false;
}

bool LinuxStorage::Reject(const char* message) {
    error_ = message;
    return false;
}

bool LinuxStorage::Open(const std::string& misc_path) {
    if (misc_ >= 0) return Reject("Storage is already open");
    char resolved[PATH_MAX];
    if (!realpath(misc_path.c_str(), resolved)) return Fail("resolve misc");
    misc_ = open(resolved, O_RDWR | O_CLOEXEC | O_SYNC | O_NOFOLLOW);
    if (misc_ < 0) return Fail("open misc");
    struct stat misc_stat {};
    if (fstat(misc_, &misc_stat)) return Fail("stat misc");
    if (!S_ISBLK(misc_stat.st_mode)) return Reject("misc is not a block device");
    uint64_t size;
    if (ioctl(misc_, BLKGETSIZE64, &size)) return Fail("get misc size");
    if (size != kParaSize) return Reject("misc does not match the r1 para size");
    // An advisory lock prevents another instance of this adapter from writing
    // concurrently. It does not lock out kernel code or other raw writers.
    if (flock(misc_, LOCK_EX | LOCK_NB)) return Fail("lock misc");

    // Buffered readback could merely return our own dirty/clean page cache.
    // A separate direct-read fd checks what the device returns after fsync.
    misc_read_ = open(resolved, O_RDONLY | O_CLOEXEC | O_DIRECT | O_NOFOLLOW);
    if (misc_read_ < 0) return Fail("open misc for direct reads");
    struct stat read_stat {};
    if (fstat(misc_read_, &read_stat)) return Fail("stat misc direct-read fd");
    if (!S_ISBLK(read_stat.st_mode) || read_stat.st_rdev != misc_stat.st_rdev) {
        return Reject("Wrong misc direct-read device node");
    }

    const std::string device = "/sys/dev/block/" + std::to_string(major(misc_stat.st_rdev)) +
                               ":" + std::to_string(minor(misc_stat.st_rdev));
    if (!realpath(device.c_str(), resolved)) return Fail("resolve misc sysfs device");
    const std::string partition = resolved;
    std::ifstream uevent(partition + "/uevent");
    bool para = false;
    for (std::string line; std::getline(uevent, line);) {
        if (line == "PARTNAME=para") para = true;
    }
    if (!para || uevent.bad()) return Reject("misc is not the para GPT partition");
    const std::string parent = partition.substr(0, partition.rfind('/'));
    const std::string name = parent.substr(parent.rfind('/') + 1);
    if (name.size() <= 6 || name.compare(0, 6, "mmcblk") ||
        name.find_first_not_of("0123456789", 6) != std::string::npos) {
        return Reject("para is not on an MMC disk");
    }
    std::string type, dev;
    if (!ReadText(parent + "/device/type", &type) || type != "MMC" ||
        !ReadText(parent + "/dev", &dev)) return Reject("Cannot identify the eMMC parent");
    unsigned disk_major, disk_minor;
    char colon, extra;
    std::istringstream numbers(dev);
    if (!(numbers >> disk_major >> colon >> disk_minor) || colon != ':' || (numbers >> extra)) {
        return Reject("Invalid eMMC device number");
    }
    // The mainline MMC ioctl checks CAP_SYS_RAWIO and whole-disk ownership,
    // not FMODE_WRITE. Keep ordinary write()/pwrite() unavailable on this fd.
    emmc_ = open(("/dev/block/" + name).c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (emmc_ < 0) return Fail("open eMMC");
    struct stat disk_stat {};
    if (fstat(emmc_, &disk_stat)) return Fail("stat eMMC");
    if (!S_ISBLK(disk_stat.st_mode) || major(disk_stat.st_rdev) != disk_major ||
        minor(disk_stat.st_rdev) != disk_minor) return Reject("Wrong eMMC device node");
    error_.clear();
    return true;
}

bool LinuxStorage::ReadRecord(Record* record) {
    alignas(kReadSize) std::array<uint8_t, kReadSize> data {};
    ssize_t count;
    do { count = pread(misc_read_, data.data(), data.size(), 0); }
    while (count < 0 && errno == EINTR);
    if (count < 0) return Fail("direct read para record");
    if (count != static_cast<ssize_t>(data.size())) {
        // Continuing a short direct read could violate sector alignment.
        errno = EIO;
        return Fail("short direct read of para");
    }
    std::copy_n(data.begin() + kRecordOffset, record->size(), record->begin());
    error_.clear();
    return true;
}

bool LinuxStorage::WriteRecord(const Record& record) {
    if (!Control::Valid(record)) return Reject("Refusing an invalid boot record write");
    if (!Write(misc_, record.data(), record.size())) return Fail("write para record");
    int result;
    do { result = fsync(misc_); } while (result && errno == EINTR);
    if (result) return Fail("sync para record");
    Record readback;
    if (!ReadRecord(&readback)) return false;
    if (readback != record) return Reject("para record readback differs");
    error_.clear();
    return true;
}

bool LinuxStorage::ReadBootConfig(uint8_t* config) {
    std::array<uint8_t, 512> data {};
    mmc_ioc_cmd cmd {};
    cmd.opcode = 8;  // SEND_EXT_CSD
    cmd.flags = 0x35;  // R1 | ADTC, matching the stock request
    cmd.blksz = data.size();
    cmd.blocks = 1;
    mmc_ioc_cmd_set_data(cmd, data.data());
    if (ioctl(emmc_, MMC_IOC_CMD, &cmd)) return Fail("read eMMC EXT_CSD");
    *config = data[179];
    error_.clear();
    return true;
}

bool LinuxStorage::WriteBootConfig(uint8_t config) {
    const unsigned region = (config & kBootMask) >> 3;
    if (region != 1 && region != 2) return Reject("Invalid eMMC boot region");
    uint8_t before;
    if (!ReadBootConfig(&before)) return false;
    if ((before & ~kBootMask) != (config & ~kBootMask)) {
        return Reject("Refusing a change to unrelated eMMC configuration bits");
    }
    mmc_ioc_cmd cmd {};
    cmd.opcode = 6;  // SWITCH, write byte 179, command set 1
    cmd.arg = 0x03b30001 | (uint32_t{config} << 8);
    cmd.flags = 0x1d;  // R1B | AC
    cmd.cmd_timeout_ms = 1000;
    // Do not skip an unchanged byte here: a rollback after an uncertain ioctl
    // must also refresh the kernel's cached PARTITION_CONFIG.
    if (ioctl(emmc_, MMC_IOC_CMD, &cmd)) return Fail("select eMMC boot region");
    if (cmd.response[0] & kR1SwitchError) return Reject("eMMC reported SWITCH_ERROR");
    error_.clear();
    return true;
}

}  // namespace haretic::boot
