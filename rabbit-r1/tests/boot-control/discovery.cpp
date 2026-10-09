// SPDX-License-Identifier: Apache-2.0
// Sysfs text is real, but every device fd and device syscall is simulated.
// No syscall wrapper forwards requests to a host block device.
#include "LinuxStorage.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <cstdarg>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <optional>
#include <string>
#include <vector>
#include <fcntl.h>
#include <linux/fs.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

using namespace haretic::boot;
namespace fs = std::filesystem;

static std::string case_name;
static void Require(bool condition, const char* message) {
    if (!condition) {
        std::cerr << "FAIL: " << case_name << ": " << message << '\n';
        std::abort();
    }
}

enum class Role { Writer, Reader, Disk };
struct Descriptor { Role role; int flags; };
struct Model;
static Model* model;

struct Model {
    explicit Model(fs::path path) : root(std::move(path)) {
        Require(!model, "overlapping device models");
        model = this;
        media.fill(0xa5);
        cache = media;
    }
    ~Model() {
        Require(fds.empty(), "device descriptors leaked");
        Require(!lock_owner, "exclusive lock leaked");
        model = nullptr;
    }

    fs::path root;
    std::string parent = "mmcblk7";
    std::optional<std::string> uevent = "DEVTYPE=partition\nPARTN=18\nPARTNAME=para\n";
    std::optional<std::string> type = "MMC\n", dev = "179:224\n";
    mode_t writer_mode = S_IFBLK, reader_mode = S_IFBLK, disk_mode = S_IFBLK;
    dev_t partition_dev = makedev(179, 242), reader_dev = partition_dev;
    dev_t disk_dev = makedev(179, 224);
    uint64_t size = 0x80000;
    bool resolve_error = false, sysfs_error = false, size_error = false, lock_error = false;
    std::optional<Role> open_error, stat_error;
    std::map<int, Descriptor> fds;
    std::vector<Role> opened, closed;
    int next_fd = 10000, lock_owner = 0;
    unsigned resolved = 0, locks = 0, reads = 0, writes = 0, syncs = 0;
    std::array<uint8_t, 0x80000> media, cache;

    std::string Partition() const { return (root / parent / "mmcblk7p18").string(); }
    std::string SysfsLink() const {
        return "/sys/dev/block/" + std::to_string(major(partition_dev)) + ":" +
               std::to_string(minor(partition_dev));
    }
    void Fixture() const {
        fs::create_directories(Partition());
        fs::create_directories(root / parent / "device");
        auto write = [](const fs::path& path, const std::optional<std::string>& text) {
            if (!text) return;
            std::ofstream stream(path);
            stream << *text;
            stream.close();
            Require(bool(stream), "cannot create sysfs fixture");
        };
        write(fs::path(Partition()) / "uevent", uevent);
        write(root / parent / "device/type", type);
        write(root / parent / "dev", dev);
    }
    const Descriptor& Fd(int fd) const {
        auto found = fds.find(fd);
        Require(found != fds.end(), "operation used an unknown or closed descriptor");
        return found->second;
    }
    void Clean() const {
        Require(fds.empty() && !lock_owner, "destruction did not release devices and lock");
        Require(opened.size() == closed.size(), "open/close counts differ");
    }
};

extern "C" char* __wrap_realpath(const char* path, char* resolved) {
    Require(model && resolved, "unexpected realpath call");
    ++model->resolved;
    std::string result;
    if (std::string(path) == "/dev/block/by-name/para") {
        if (model->resolve_error) { errno = ENOENT; return nullptr; }
        result = "/dev/block/mmcblk7p18";
    } else {
        Require(path == model->SysfsLink(), "wrong sysfs device number was resolved");
        if (model->sysfs_error) { errno = ENOENT; return nullptr; }
        result = model->Partition();
    }
    Require(result.size() < PATH_MAX, "fixture path exceeds realpath buffer");
    return std::strcpy(resolved, result.c_str());
}

extern "C" int __wrap_open(const char* path, int flags, ...) {
    Require(model, "unexpected device open");
    Require((flags & (O_CLOEXEC | O_NOFOLLOW)) == (O_CLOEXEC | O_NOFOLLOW),
            "device fd lacks close-on-exec or final-symlink protection");
    Require(!(flags & (O_CREAT | O_TRUNC | O_APPEND)), "unsafe device open flags");
    Role role;
    if (std::string(path) == "/dev/block/mmcblk7p18") {
        if ((flags & O_ACCMODE) == O_RDWR) {
            role = Role::Writer;
            Require((flags & O_SYNC) == O_SYNC && !(flags & O_DIRECT),
                    "metadata writer must be synchronous and buffered");
        } else {
            role = Role::Reader;
            Require((flags & O_ACCMODE) == O_RDONLY && (flags & O_DIRECT),
                    "metadata readback must use a read-only direct fd");
        }
    } else {
        Require(std::string(path) == "/dev/block/" + model->parent, "unexpected block path");
        role = Role::Disk;
        Require((flags & O_ACCMODE) == O_RDONLY, "whole disk permits ordinary writes");
    }
    if (model->open_error == role) { errno = EACCES; return -1; }
    const int fd = model->next_fd++;
    model->fds.emplace(fd, Descriptor{role, flags});
    model->opened.push_back(role);
    return fd;
}

extern "C" int __wrap_fstat(int fd, struct stat* st) {
    const Role role = model->Fd(fd).role;
    if (model->stat_error == role) { errno = EIO; return -1; }
    *st = {};
    if (role == Role::Writer) { st->st_mode = model->writer_mode; st->st_rdev = model->partition_dev; }
    if (role == Role::Reader) { st->st_mode = model->reader_mode; st->st_rdev = model->reader_dev; }
    if (role == Role::Disk) { st->st_mode = model->disk_mode; st->st_rdev = model->disk_dev; }
    return 0;
}

extern "C" int __wrap_ioctl(int fd, unsigned long request, ...) {
    Require(model->Fd(fd).role == Role::Writer && request == BLKGETSIZE64,
            "discovery issued an unexpected ioctl");
    if (model->size_error) { errno = EIO; return -1; }
    va_list args;
    va_start(args, request);
    *va_arg(args, uint64_t*) = model->size;
    va_end(args);
    return 0;
}

extern "C" int __wrap_flock(int fd, int operation) {
    Require(model->Fd(fd).role == Role::Writer && operation == (LOCK_EX | LOCK_NB),
            "metadata lock must be exclusive and nonblocking");
    ++model->locks;
    if (model->lock_error || (model->lock_owner && model->lock_owner != fd)) {
        errno = EWOULDBLOCK;
        return -1;
    }
    model->lock_owner = fd;
    return 0;
}

extern "C" int __wrap_close(int fd) {
    model->closed.push_back(model->Fd(fd).role);
    if (model->lock_owner == fd) model->lock_owner = 0;
    model->fds.erase(fd);
    return 0;
}

extern "C" ssize_t __wrap_pread(int fd, void* data, size_t size, off_t offset) {
    Require(model->Fd(fd).role == Role::Reader, "readback bypassed the direct-read fd");
    Require(offset == 0 && size == 4096 && reinterpret_cast<uintptr_t>(data) % 4096 == 0,
            "direct read is not sector aligned");
    ++model->reads;
    std::memcpy(data, model->media.data(), size);
    return size;
}

extern "C" ssize_t __wrap_pwrite(int fd, const void* data, size_t size, off_t offset) {
    Require(model->Fd(fd).role == Role::Writer, "ordinary write escaped the metadata fd");
    Require(offset >= 2048 && offset + size <= 2080, "write escaped the boot-control record");
    ++model->writes;
    std::memcpy(model->cache.data() + offset, data, size);
    return size;
}

extern "C" int __wrap_fsync(int fd) {
    Require(model->Fd(fd).role == Role::Writer, "sync used the wrong descriptor");
    ++model->syncs;
    model->media = model->cache;
    return 0;
}

int main(int argc, char* argv[]) {
    Require(argc == 2, "expected a fixture parent directory");
    const fs::path output = fs::absolute(argv[1]).lexically_normal();
    Require(output == "/rabbitr1/out/boot-control", "fixture directory must stay in the workspace");
    std::string temporary = (output / "discovery-XXXXXX").string();
    Require(mkdtemp(temporary.data()), "cannot create fixture directory");
    const fs::path root = temporary;
    unsigned cases = 0;
    auto reject = [&](const char* name, const std::function<void(Model&)>& configure,
                      const char* error) {
        case_name = name;
        Model state(root / std::to_string(cases));
        configure(state);
        state.Fixture();
        {
            LinuxStorage storage;
            Require(!storage.Open("/dev/block/by-name/para"), "unsafe or unavailable device accepted");
            Require(storage.Error().find(error) != std::string::npos, "failure reason was lost");
            Require(!state.reads && !state.writes && !state.syncs, "discovery modified device contents");
        }
        state.Clean();
        ++cases;
    };

    reject("missing misc alias", [](auto& m) { m.resolve_error = true; }, "resolve misc");
    reject("missing sysfs link", [](auto& m) { m.sysfs_error = true; }, "resolve misc sysfs");
    for (Role role : {Role::Writer, Role::Reader, Role::Disk}) {
        reject("open failure", [=](auto& m) { m.open_error = role; }, "open ");
        reject("stat failure", [=](auto& m) { m.stat_error = role; }, "stat ");
    }
    reject("misc is a regular file", [](auto& m) { m.writer_mode = S_IFREG; }, "not a block device");
    reject("size ioctl failure", [](auto& m) { m.size_error = true; }, "get misc size");
    reject("undersized para", [](auto& m) { --m.size; }, "para size");
    reject("oversized para", [](auto& m) { ++m.size; }, "para size");
    reject("exclusive lock unavailable", [](auto& m) { m.lock_error = true; }, "lock misc");
    reject("direct-read node replaced", [](auto& m) { m.reader_dev = makedev(179, 243); },
           "Wrong misc direct-read device");
    reject("direct-read node is regular", [](auto& m) { m.reader_mode = S_IFREG; },
           "Wrong misc direct-read device");
    reject("uevent missing", [](auto& m) { m.uevent.reset(); }, "para GPT partition");
    reject("wrong GPT partition", [](auto& m) { m.uevent = "PARTNAME=metadata\n"; }, "para GPT partition");
    reject("GPT prefix collision", [](auto& m) { m.uevent = "PARTNAME=para_backup\n"; }, "para GPT partition");
    for (const char* name : {"sda", "mmcblk", "mmcblk7p1", "mmcblk7extra"}) {
        reject("non-disk sysfs parent", [=](auto& m) { m.parent = name; }, "not on an MMC disk");
    }
    reject("MMC type missing", [](auto& m) { m.type.reset(); }, "identify the eMMC parent");
    reject("SD instead of eMMC", [](auto& m) { m.type = "SD\n"; }, "identify the eMMC parent");
    reject("empty MMC type", [](auto& m) { m.type = ""; }, "identify the eMMC parent");
    reject("parent device number missing", [](auto& m) { m.dev.reset(); }, "identify the eMMC parent");
    for (const char* dev : {"179,224\n", "179:224junk\n", "179:\n", "4294967296:224\n"}) {
        reject("invalid parent device number", [=](auto& m) { m.dev = dev; }, "Invalid eMMC device number");
    }
    reject("wrong whole-disk major", [](auto& m) { m.disk_dev = makedev(180, 224); }, "Wrong eMMC device");
    reject("wrong whole-disk minor", [](auto& m) { m.disk_dev = makedev(179, 225); }, "Wrong eMMC device");
    reject("whole-disk node is regular", [](auto& m) { m.disk_mode = S_IFREG; }, "Wrong eMMC device");

    case_name = "valid discovery, descriptor routing and lock lifetime";
    {
        Model state(root / "valid");
        state.parent = "mmcblk12";  // Do not assume a single-digit disk number.
        state.Fixture();
        {
            LinuxStorage storage;
            Require(storage.Open("/dev/block/by-name/para"), "valid eMMC discovery failed");
            Require(storage.Error().empty() && state.fds.size() == 3 && state.lock_owner,
                    "successful open lacks descriptors or retains an error");
            const size_t opened = state.opened.size();
            Require(!storage.Open("/dev/block/by-name/para"), "duplicate Open was accepted");
            Require(state.opened.size() == opened, "duplicate Open leaked descriptors");
            {
                LinuxStorage competitor;
                Require(!competitor.Open("/dev/block/by-name/para"), "competing writer acquired the lock");
                Require(competitor.Error().find("lock misc") != std::string::npos,
                        "competing writer failed for the wrong reason");
            }
            Require(state.fds.size() == 3 && state.lock_owner, "competitor damaged the owner's descriptors");

            Record record {};
            record[4] = 'B'; record[5] = 'C'; record[6] = 'A'; record[7] = 'B';
            record[8] = 1; record[9] = 2; record[12] = 0x7f; record[14] = 0x7e;
            Control::Seal(&record);
            Require(storage.WriteRecord(record), "write with discovered descriptors failed");
            Record readback;
            Require(storage.ReadRecord(&readback) && readback == record, "direct readback differs");
            Require(storage.Error().empty() && state.reads == 2 && state.writes == 1 && state.syncs == 1,
                    "wrong data path or stale error after successful operations");
            Require(std::all_of(state.media.begin(), state.media.begin() + 2048,
                                [](auto b) { return b == 0xa5; }) &&
                    std::all_of(state.media.begin() + 2080, state.media.end(),
                                [](auto b) { return b == 0xa5; }), "unrelated para bytes changed");
        }
        state.Clean();
        {
            LinuxStorage next;
            Require(next.Open("/dev/block/by-name/para"), "destruction did not release exclusive access");
        }
        state.Clean();
        ++cases;
    }
    fs::remove_all(root);
    std::cout << "PASS: " << cases << " Linux storage discovery scenarios\n";
}
