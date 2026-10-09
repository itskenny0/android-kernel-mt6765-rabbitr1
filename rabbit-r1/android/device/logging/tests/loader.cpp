// SPDX-License-Identifier: Apache-2.0
// Execute the real loader without forwarding any syscall to a host device.
#include "Expdb.h"
#include "ExpdbLayout.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdarg>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <functional>
#include <iostream>
#include <linux/fs.h>
#include <map>
#include <string>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include <vector>

using haretic::logging::StartExpdb;
using haretic::logging::ValidateExpdbTable;
static std::string test_name;
static void Check(bool value, const char* message) {
    if (!value) {
        std::cerr << "FAIL: " << test_name << ": " << message << '\n';
        std::abort();
    }
}
const dev_t raw_dev = makedev(179, 5), map_dev = makedev(253, 9);
constexpr const char* raw_path = "/dev/block/mmcblk0p5";
constexpr const char* map_path = "/dev/block/dm-9";
constexpr const char* backend_path = "/sys/module/pstore/parameters/backend";
constexpr const char* parameters = "blkdev=/dev/block/dm-9 best_effort=1 console_size=1024 pmsg_size=64 kmsg_size=64 ftrace_size=0 max_reason=2";

enum class Role { Text, Raw, Control, Map, Zone, Blk };
struct File { Role role; std::string text; size_t offset = 0; };
struct Model {
    std::map<int, File> files;
    int next_fd = 20000;
    std::map<std::string, std::string> attributes = {
        {"/sys/class/block/mmcblk0p5/uevent", "DEVNAME=mmcblk0p5\nDEVTYPE=partition\nPARTNAME=expdb\n"},
        {"/sys/class/block/mmcblk0p5/dev", "179:5\n"},
        {"/sys/class/block/mmcblk0/device/type", "MMC\n"}};
    std::vector<std::string> entries = {".", "..", "mmcblk0", "mmcblk0p5", "dm-0"};
    std::vector<std::string> directory_entries;
    size_t directory_index = 0;
    bool directory_open = false;
    std::string failure, table_fault, uuid, backend = "(null)";
    std::string changed_parameter, wrong_parameter = "wrong", after_backend;
    unsigned table_fault_at = 1, tables = 0, creates = 0, removes = 0, sleeps = 0;
    unsigned loads = 0, zone_loads = 0, blk_attempts = 0;
    unsigned nodes_delay = 0, links_delay = 0;
    bool map_exists = false, active = false, zone = false, blk = false;
    bool preexisting = false, replace_owner = false, bad_links = false, missing_links = false;
    bool raw_regular = false, map_regular = false, module_regular = true;
    bool raw_wrong_dev = false, map_wrong_dev = false, random_partial = false;
    uint64_t raw_size = 20971520, map_size = 18874368;
    uint64_t elapsed_ms = 0;
    std::vector<unsigned char> backing;

    bool Fault(const char* at, int error = EIO) {
        if (failure != at) return false;
        errno = error;
        return true;
    }
    File& Fd(int fd) {
        auto it = files.find(fd);
        Check(it != files.end(), "unknown or closed fd used");
        return it->second;
    }
    std::string Parameter(const std::string& key) const {
        if (key == changed_parameter) return wrong_parameter;
        if (key == "blkdev") return map_path;
        if (key == "best_effort") return "Y";
        if (key == "console_size") return "1024";
        if (key == "pmsg_size" || key == "kmsg_size") return "64";
        if (key == "ftrace_size") return "0";
        if (key == "max_reason") return "2";
        Check(false, "unexpected module parameter read");
        return {};
    }
};
static Model* current;

extern "C" int __wrap_open(const char* input, int flags, ...) {
    auto& m = *current;
    const std::string path = input;
    Check((flags & (O_CLOEXEC | O_NOFOLLOW)) == (O_CLOEXEC | O_NOFOLLOW), "missing safe open flags");
    Check(!(flags & (O_CREAT | O_TRUNC | O_APPEND)), "destructive open flags");
    Role role;
    std::string value;
    if (path == raw_path) {
        role = Role::Raw;
        Check(flags & O_EXCL, "raw expdb was not exclusively probed");
        if (m.Fault("raw.open")) return -1;
    } else if (path == "/dev/device-mapper") {
        role = Role::Control;
        if (m.Fault("control.open")) return -1;
    } else if (path == map_path) {
        role = Role::Map;
        Check(m.active, "map node opened before activation");
        if (m.Fault("map.open")) return -1;
    } else if (path == "/vendor/lib/modules/pstore_zone.ko") {
        role = Role::Zone;
        if (m.Fault("zone.open")) return -1;
    } else if (path == "/vendor/lib/modules/pstore_blk.ko") {
        role = Role::Blk;
        if (m.Fault("blk.open")) return -1;
    } else {
        role = Role::Text;
        if (m.Fault("text.open")) return -1;
        if (path == backend_path) {
            if (m.blk && m.Fault("backend.open")) return -1;
            value = (m.blk && !m.after_backend.empty() ? m.after_backend : m.backend) + "\n";
        } else if (path.find("/sys/module/pstore_blk/parameters/") == 0) {
            Check(m.blk, "module parameters read before module loading");
            value = m.Parameter(path.substr(path.rfind('/') + 1)) + "\n";
        } else {
            auto found = m.attributes.find(path);
            if (found == m.attributes.end()) { errno = ENOENT; return -1; }
            value = found->second;
        }
    }
    Check((flags & O_ACCMODE) == (role == Role::Control ? O_RDWR : O_RDONLY),
          "ordinary write access requested outside mapper control");
    const int fd = m.next_fd++;
    m.files.emplace(fd, File{role, value, 0});
    return fd;
}
extern "C" int __wrap_close(int fd) {
    current->Fd(fd);
    current->files.erase(fd);
    return 0;
}
extern "C" ssize_t __wrap_read(int fd, void* buffer, size_t capacity) {
    auto& file = current->Fd(fd);
    Check(file.role == Role::Text, "attempted a raw-device read");
    if (current->Fault("text.read")) return -1;
    // Small reads exercise the production bounded accumulation loop.
    const size_t size = std::min({capacity, file.text.size() - file.offset, size_t{17}});
    std::memcpy(buffer, file.text.data() + file.offset, size);
    file.offset += size;
    return size;
}
extern "C" int __wrap_fstat(int fd, struct stat* result) {
    auto& m = *current;
    const Role role = m.Fd(fd).role;
    *result = {};
    result->st_size = 4096;
    if (role == Role::Raw) {
        if (m.Fault("raw.stat")) return -1;
        result->st_mode = m.raw_regular ? S_IFREG : S_IFBLK;
        result->st_rdev = m.raw_wrong_dev ? makedev(179, 6) : raw_dev;
    } else if (role == Role::Map) {
        if (m.Fault("map.stat")) return -1;
        result->st_mode = m.map_regular ? S_IFREG : S_IFBLK;
        result->st_rdev = m.map_wrong_dev ? makedev(253, 10) : map_dev;
    } else if (role == Role::Control) {
        if (m.Fault("control.stat")) return -1;
        result->st_mode = S_IFCHR;
    } else {
        Check(role == Role::Zone || role == Role::Blk, "unexpected fstat");
        if (m.Fault(role == Role::Zone ? "zone.stat" : "blk.stat")) return -1;
        result->st_mode = m.module_regular ? S_IFREG : S_IFLNK;
    }
    return 0;
}
extern "C" int __wrap_lstat(const char* path, struct stat* result) {
    auto& m = *current;
    bool present;
    if (std::string(path) == "/sys/module/pstore_zone") present = m.zone;
    else { Check(std::string(path) == "/sys/module/pstore_blk", "unexpected lstat"); present = m.blk; }
    if (m.Fault("module.inspect", EACCES)) return -1;
    if (!present) { errno = ENOENT; return -1; }
    *result = {};
    result->st_mode = S_IFDIR;
    return 0;
}
extern "C" DIR* __wrap_opendir(const char* path) {
    auto& m = *current;
    Check(std::string(path) == "/sys/class/block" && !m.directory_open, "unexpected directory open");
    if (m.Fault("scan")) return nullptr;
    m.directory_open = true;
    m.directory_entries = m.sleeps < m.nodes_delay ? std::vector<std::string>{} : m.entries;
    m.directory_index = 0;
    return reinterpret_cast<DIR*>(&m);
}
extern "C" dirent* __wrap_readdir(DIR* directory) {
    auto& m = *current;
    Check(directory == reinterpret_cast<DIR*>(&m) && m.directory_open, "wrong directory handle");
    if (m.Fault("scan.read")) return nullptr;
    if (m.directory_index == m.directory_entries.size()) return nullptr;
    static dirent entry;
    std::memset(&entry, 0, sizeof(entry));
    const auto& name = m.directory_entries[m.directory_index++];
    Check(name.size() < sizeof(entry.d_name), "fixture name too long");
    std::strcpy(entry.d_name, name.c_str());
    return &entry;
}
extern "C" int __wrap_closedir(DIR* directory) {
    Check(directory == reinterpret_cast<DIR*>(current) && current->directory_open, "wrong directory close");
    current->directory_open = false;
    return 0;
}
extern "C" int __wrap_clock_gettime(clockid_t clock, timespec* time) {
    Check(clock == CLOCK_MONOTONIC, "deadline is not monotonic");
    if (current->Fault("clock")) return -1;
    time->tv_sec = current->elapsed_ms / 1000;
    time->tv_nsec = (current->elapsed_ms % 1000) * 1000000;
    return 0;
}
extern "C" int __wrap_nanosleep(const timespec* time, timespec*) {
    Check(time->tv_sec == 0 && time->tv_nsec > 0 && time->tv_nsec <= 100000000,
          "unbounded readiness sleep");
    if (current->Fault("sleep")) return -1;
    ++current->sleeps;
    current->elapsed_ms += time->tv_nsec / 1000000;
    return 0;
}
extern "C" char* __wrap_realpath(const char* path, char* result) {
    auto& m = *current;
    Check(std::string(path) == "/dev/block/mapper/r1-expdb" ||
          std::string(path) == "/dev/block/mapper/by-uuid/" + m.uuid, "untrusted mapper link");
    Check(result && m.active, "mapper link resolved before activation");
    if (m.Fault("links")) return nullptr;
    if (m.missing_links || m.sleeps < m.links_delay) { errno = ENOENT; return nullptr; }
    return std::strcpy(result, m.bad_links ? "/dev/block/dm-8" : map_path);
}
extern "C" ssize_t __wrap_getrandom(void* data, size_t size, unsigned flags) {
    Check(flags == 0 && size && size <= 16, "wrong random request");
    if (current->Fault("random")) return -1;
    const size_t count = current->random_partial ? std::min(size, size_t{3}) : size;
    std::memset(data, 0x42, count);
    return count;
}

static void FillHeader(dm_ioctl* dm, const Model& m) {
    dm->version[0] = DM_VERSION_MAJOR;
    dm->dev = map_dev;
    std::strcpy(dm->name, R1_EXPDB_MAP_NAME);
    std::strcpy(dm->uuid, (m.replace_owner && m.active) ? "foreign-owner" : m.uuid.c_str());
    dm->data_size = offsetof(dm_ioctl, data);
    dm->data_start = sizeof(*dm);
    dm->flags = m.active ? DM_ACTIVE_PRESENT_FLAG : 0;
    dm->target_count = m.active ? 1 : 0;
}
static dm_target_spec* FillTable(dm_ioctl* dm, const Model& m) {
    FillHeader(dm, m);
    dm->flags |= DM_STATUS_TABLE_FLAG;
    dm->data_size = sizeof(*dm);
    auto* target = reinterpret_cast<dm_target_spec*>(reinterpret_cast<char*>(dm) + dm->data_start);
    *target = {};
    target->length = 36864;
    std::strcpy(target->target_type, "linear");
    char* args = reinterpret_cast<char*>(target + 1);
    std::strcpy(args, "179:5 0");
    dm->data_size += sizeof(*target) + std::strlen(args) + 1;
    target->next = (sizeof(*target) + std::strlen(args) + 1 + 7) & ~size_t{7};
    return target;
}
static void Corrupt(dm_ioctl* dm, dm_target_spec* target, const std::string& kind) {
    char* args = reinterpret_cast<char*>(target + 1);
    if (kind == "size") dm->data_size = 4097;
    else if (kind == "short") dm->data_size = sizeof(*dm) - 1;
    else if (kind == "overlap") dm->data_start = sizeof(*dm) - 8;
    else if (kind == "unaligned") ++dm->data_start;
    else if (kind == "missing") dm->target_count = 0;
    else if (kind == "multiple") dm->target_count = 2;
    else if (kind == "full") dm->flags |= DM_BUFFER_FULL_FLAG;
    else if (kind == "suspended") dm->flags |= DM_SUSPEND_FLAG;
    else if (kind == "readonly") dm->flags |= DM_READONLY_FLAG;
    else if (kind == "inactive") dm->flags &= ~DM_ACTIVE_PRESENT_FLAG;
    else if (kind == "pending") dm->flags |= DM_INACTIVE_PRESENT_FLAG;
    else if (kind == "wrongdev") ++dm->dev;
    else if (kind == "wronguuid") dm->uuid[0] ^= 1;
    else if (kind == "uuidnul") std::memset(dm->uuid, 'x', sizeof(dm->uuid));
    else if (kind == "namenul") std::memset(dm->name, 'x', sizeof(dm->name));
    else if (kind == "start") target->sector_start = 1;
    else if (kind == "length") target->length = 40960;
    else if (kind == "type") std::strcpy(target->target_type, "error");
    else if (kind == "typenul") std::memset(target->target_type, 'x', sizeof(target->target_type));
    else if (kind == "backing") std::strcpy(args, "179:6 0");
    else if (kind == "offset") std::strcpy(args, "179:5 1");
    else if (kind == "paramsnul") std::memset(args, 'x', dm->data_size - dm->data_start - sizeof(*target));
    else if (kind == "next") target->next = 4096;
    else if (kind == "status") target->status = -EIO;
    else Check(false, "unknown table fault");
}
extern "C" int __wrap_ioctl(int fd, unsigned long request, ...) {
    auto& m = *current;
    const Role role = m.Fd(fd).role;
    va_list args;
    va_start(args, request);
    void* argument = va_arg(args, void*);
    va_end(args);
    if (request == BLKGETSIZE64) {
        Check(role == Role::Raw || role == Role::Map, "size ioctl on wrong fd");
        if (m.Fault(role == Role::Raw ? "raw.size" : "map.size")) return -1;
        *static_cast<uint64_t*>(argument) = role == Role::Raw ? m.raw_size : m.map_size;
        return 0;
    }
    Check(role == Role::Control, "DM ioctl sent to a block device");
    auto* dm = static_cast<dm_ioctl*>(argument);
    Check(dm->data_size == 4096 && dm->data_start == sizeof(*dm), "bad DM input buffer");
    if (request == DM_DEV_STATUS && dm->name[0]) {
        Check(!dm->uuid[0] && std::string(dm->name) == "r1-expdb", "wrong existing-map lookup");
        if (m.Fault("lookup")) return -1;
        if (m.preexisting) { m.map_exists = true; return 0; }
        errno = ENXIO;
        return -1;
    }
    if (request == DM_DEV_CREATE) {
        Check(std::string(dm->name) == "r1-expdb" && std::string(dm->uuid).size() == 46,
              "map name or random UUID missing");
        Check(m.files.size() == 1, "exclusive raw fd not released before DM ownership");
        if (m.Fault("create", EEXIST)) return -1;
        ++m.creates;
        m.map_exists = true;
        m.uuid = dm->uuid;
        FillHeader(dm, m);
        return 0;
    }
    Check(!dm->name[0] && std::string(dm->uuid) == m.uuid, "mutation did not use this invocation's UUID");
    Check(m.map_exists, "operation on an absent map");
    if (request == DM_DEV_STATUS) {
        if (m.Fault("status")) return -1;
        FillHeader(dm, m);
    } else if (request == DM_TABLE_LOAD) {
        if (m.Fault("load")) return -1;
        Check(dm->target_count == 1 && !dm->flags, "unbounded target count or table flags");
        auto* target = reinterpret_cast<dm_target_spec*>(reinterpret_cast<char*>(dm) + dm->data_start);
        Check(target->sector_start == 0 && target->length == 36864 &&
              std::string(target->target_type) == "linear" &&
              std::string(reinterpret_cast<char*>(target + 1)) == "179:5 0",
              "submitted table can reach outside the first 18 MiB of expdb");
    } else if (request == DM_DEV_SUSPEND) {
        if (m.Fault("resume")) return -1;
        Check(!dm->flags, "loader suspended instead of activating");
        m.active = true;
    } else if (request == DM_TABLE_STATUS) {
        if (m.Fault("table")) return -1;
        Check(dm->flags == DM_STATUS_TABLE_FLAG, "query did not request the active table");
        ++m.tables;
        auto* target = FillTable(dm, m);
        if (!m.table_fault.empty() && m.tables == m.table_fault_at) Corrupt(dm, target, m.table_fault);
    } else if (request == DM_DEV_REMOVE) {
        Check(!m.blk_attempts && !m.blk && !m.preexisting && !m.replace_owner,
              "removed a preexisting, foreign or possibly attached map");
        if (m.Fault("remove")) return -1;
        Check(!dm->flags, "deferred/forced deletion requested");
        ++m.removes;
        m.map_exists = false;
    } else Check(false, "unexpected DM ioctl");
    return 0;
}
extern "C" long __wrap_syscall(long number, ...) {
    auto& m = *current;
    Check(number == SYS_finit_module, "unexpected syscall");
    va_list args;
    va_start(args, number);
    const int fd = va_arg(args, int);
    const char* options = va_arg(args, const char*);
    const int flags = va_arg(args, int);
    va_end(args);
    Check(flags == 0 && m.active && m.tables, "module loaded before verified mapping or with force flags");
    ++m.loads;
    if (m.Fd(fd).role == Role::Zone) {
        Check(!*options && !m.zone && !m.blk, "wrong zone-module options or order");
        ++m.zone_loads;
        if (m.Fault("zone.load")) return -1;
        m.zone = true;
    } else {
        Check(m.Fd(fd).role == Role::Blk && m.zone && !m.blk && m.tables == 2,
              "blk module load did not follow zone load and fresh mapping verification");
        Check(std::string(options) == parameters, "unsafe or unexpected pstore module options");
        ++m.blk_attempts;
        if (m.Fault("blk.load")) return -1;
        m.blk = true;
        m.backend = "pstore_blk";
        m.backing.assign(20971520, 0xa5);
        std::fill(m.backing.begin(), m.backing.begin() + 18874368, 0x5a);
    }
    return 0;
}

int main() {
    unsigned scenarios = 0;
    auto run = [&](const char* name, const std::function<void(Model&)>& setup, bool success,
                   unsigned removals, unsigned loads) {
        test_name = name;
        Model model;
        current = &model;
        setup(model);
        std::string error;
        Check(StartExpdb(&error) == success, "unexpected loader result");
        Check(success ? error.empty() : !error.empty(), "result/error disagreement");
        Check(model.removes == removals && model.loads == loads, "wrong cleanup or module-load count");
        Check(model.files.empty() && !model.directory_open, "descriptor/directory leak");
        if (!model.backing.empty())
            Check(std::all_of(model.backing.begin() + 18874368, model.backing.end(),
                              [](auto byte) { return byte == 0xa5; }), "protected 2 MiB tail changed");
        if (success) Check(model.tables == 3 && model.blk && model.map_exists, "ready without verified attachment");
        current = nullptr;
        ++scenarios;
    };
    run("complete setup", [](auto&) {}, true, 0, 2);
    run("delayed enumeration and ueventd", [](auto& m) { m.nodes_delay = 2; m.links_delay = 5; }, true, 0, 2);
    run("partial random reads", [](auto& m) { m.random_partial = true; }, true, 0, 2);
    for (const char* failure : {"clock", "module.inspect", "scan", "scan.read", "text.open", "text.read",
                                 "raw.open", "raw.stat", "raw.size", "control.open", "control.stat",
                                 "lookup", "random", "create"}) {
        run(failure, [=](auto& m) { m.failure = failure; }, false, 0, 0);
    }
    for (const char* failure : {"load", "resume", "links", "table", "map.open", "map.stat", "map.size",
                                 "zone.open", "zone.stat"}) {
        run(failure, [=](auto& m) { m.failure = failure; }, false, 1, 0);
    }
    run("zone finit failure", [](auto& m) { m.failure = "zone.load"; }, false, 1, 1);
    run("blk open failure", [](auto& m) { m.failure = "blk.open"; }, false, 1, 1);
    run("blk stat failure", [](auto& m) { m.failure = "blk.stat"; }, false, 1, 1);
    run("blk finit failure retains map", [](auto& m) { m.failure = "blk.load"; }, false, 0, 2);
    run("backend read failure retains map", [](auto& m) { m.failure = "backend.open"; }, false, 0, 2);
    run("another backend exists", [](auto& m) { m.backend = "ramoops"; }, false, 0, 0);
    run("zone already loaded", [](auto& m) { m.zone = true; }, false, 0, 0);
    run("blk already loaded", [](auto& m) { m.blk = true; }, false, 0, 0);
    run("existing map untouched", [](auto& m) { m.preexisting = true; }, false, 0, 0);
    run("no expdb before deadline", [](auto& m) { m.entries.clear(); }, false, 0, 0);
    run("sleep failure", [](auto& m) { m.entries.clear(); m.failure = "sleep"; }, false, 0, 0);
    run("wrong GPT name", [](auto& m) { m.attributes["/sys/class/block/mmcblk0p5/uevent"] = "PARTNAME=expdb_backup\n"; }, false, 0, 0);
    run("duplicate expdb", [](auto& m) {
        m.entries.push_back("mmcblk0p6");
        m.attributes["/sys/class/block/mmcblk0p6/uevent"] = "DEVNAME=mmcblk0p6\nDEVTYPE=partition\nPARTNAME=expdb\n";
    }, false, 0, 0);
    run("duplicate label field", [](auto& m) { m.attributes["/sys/class/block/mmcblk0p5/uevent"] += "PARTNAME=expdb\n"; }, false, 0, 0);
    run("wrong sysfs devname", [](auto& m) { m.attributes["/sys/class/block/mmcblk0p5/uevent"] = "PARTNAME=expdb\nDEVNAME=mmcblk0p6\nDEVTYPE=partition\n"; }, false, 0, 0);
    run("SD parent", [](auto& m) { m.attributes["/sys/class/block/mmcblk0/device/type"] = "SD\n"; }, false, 0, 0);
    run("missing parent type", [](auto& m) { m.attributes.erase("/sys/class/block/mmcblk0/device/type"); }, false, 0, 0);
    run("embedded NUL attribute", [](auto& m) { m.attributes["/sys/class/block/mmcblk0p5/dev"] = std::string("179:5\0bad", 9); }, false, 0, 0);
    run("oversized attribute", [](auto& m) { m.attributes["/sys/class/block/mmcblk0p5/dev"].assign(4097, '1'); }, false, 0, 0);
    run("regular raw node", [](auto& m) { m.raw_regular = true; }, false, 0, 0);
    run("raw identity mismatch", [](auto& m) { m.raw_wrong_dev = true; }, false, 0, 0);
    run("raw size mismatch", [](auto& m) { ++m.raw_size; }, false, 0, 0);
    run("regular mapper node", [](auto& m) { m.map_regular = true; }, false, 1, 0);
    run("mapper identity mismatch", [](auto& m) { m.map_wrong_dev = true; }, false, 1, 0);
    run("mapper size mismatch", [](auto& m) { m.map_size = m.raw_size; }, false, 1, 0);
    run("module is symlink", [](auto& m) { m.module_regular = false; }, false, 1, 0);
    run("stale mapper links", [](auto& m) { m.bad_links = true; }, false, 1, 0);
    run("missing mapper links", [](auto& m) { m.missing_links = true; }, false, 1, 0);
    run("uncertain ownership retained", [](auto& m) { m.missing_links = true; m.failure = "status"; }, false, 0, 0);
    run("replaced map retained", [](auto& m) { m.replace_owner = true; }, false, 0, 0);
    run("failed cleanup retained", [](auto& m) { m.bad_links = true; m.failure = "remove"; }, false, 0, 0);
    for (const char* kind : {"size", "short", "overlap", "unaligned", "missing", "multiple", "full",
                             "suspended", "readonly", "inactive", "pending", "wrongdev", "wronguuid",
                             "uuidnul", "namenul", "start", "length", "type", "typenul", "backing",
                             "offset", "paramsnul", "next", "status"}) {
        run(kind, [=](auto& m) { m.table_fault = kind; }, false, 1, 0);
    }
    run("table changes before attachment", [](auto& m) { m.table_fault = "length"; m.table_fault_at = 2; }, false, 1, 1);
    run("table changes after attachment", [](auto& m) { m.table_fault = "length"; m.table_fault_at = 3; }, false, 0, 2);
    run("wrong registered backend", [](auto& m) { m.after_backend = "ramoops"; }, false, 0, 2);
    for (const char* name : {"blkdev", "best_effort", "console_size", "pmsg_size", "kmsg_size", "ftrace_size", "max_reason"})
        run(name, [=](auto& m) { m.changed_parameter = name; }, false, 0, 2);

    test_name = "bounded table parser truncations";
    alignas(8) std::array<unsigned char, 4096> bytes {};
    Model model;
    model.active = true;
    model.uuid = "haretic-expdb-test";
    FillTable(reinterpret_cast<dm_ioctl*>(bytes.data()), model);
    std::string error;
    Check(ValidateExpdbTable(bytes.data(), bytes.size(), raw_dev, map_dev, model.uuid, &error), "valid kernel table rejected");
    const auto used = reinterpret_cast<dm_ioctl*>(bytes.data())->data_size;
    for (size_t size = 0; size < used; ++size)
        Check(!ValidateExpdbTable(bytes.data(), size, raw_dev, map_dev, model.uuid, &error), "truncated response accepted");
    ++scenarios;
    std::cout << "PASS: " << scenarios << " expdb loader scenarios and all table truncation boundaries\n";
}
