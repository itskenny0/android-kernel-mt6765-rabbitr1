// SPDX-License-Identifier: Apache-2.0
#include "Expdb.h"
#include "ExpdbLayout.h"

#include <array>
#include <cerrno>
#include <climits>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <memory>
#include <sys/ioctl.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

namespace haretic::logging {
namespace {
constexpr const char* kBackend = "/sys/module/pstore/parameters/backend";
constexpr const char* kZone = "/vendor/lib/modules/pstore_zone.ko";
constexpr const char* kBlk = "/vendor/lib/modules/pstore_blk.ko";
constexpr unsigned kRejectedFlags = DM_BUFFER_FULL_FLAG | DM_SUSPEND_FLAG |
        DM_READONLY_FLAG | DM_INTERNAL_SUSPEND_FLAG | DM_DEFERRED_REMOVE |
        DM_INACTIVE_PRESENT_FLAG | DM_QUERY_INACTIVE_TABLE_FLAG;

struct Fd {
    explicit Fd(int value = -1) : value(value) {}
    ~Fd() { if (value >= 0) close(value); }
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
    int value;
};
struct Buffer {
    alignas(8) std::array<unsigned char, R1_EXPDB_DM_BUFFER_SIZE> bytes;
    dm_ioctl* Request(const std::string& name = {}, const std::string& uuid = {}) {
        bytes.fill(0);
        auto* dm = reinterpret_cast<dm_ioctl*>(bytes.data());
        dm->version[0] = DM_VERSION_MAJOR;
        dm->version[1] = DM_VERSION_MINOR;
        dm->version[2] = DM_VERSION_PATCHLEVEL;
        dm->data_size = bytes.size();
        dm->data_start = sizeof(*dm);
        // These strings only come from fixed names or the generated UUID.
        if (!name.empty()) std::memcpy(dm->name, name.c_str(), name.size() + 1);
        if (!uuid.empty()) std::memcpy(dm->uuid, uuid.c_str(), uuid.size() + 1);
        return dm;
    }
};

bool Reject(std::string* error, const std::string& message) {
    *error = message;
    return false;
}
bool Fail(std::string* error, const std::string& operation) {
    return Reject(error, operation + ": " + std::strerror(errno));
}
bool Equal(const char* value, size_t capacity, const std::string& expected) {
    const char* end = static_cast<const char*>(std::memchr(value, 0, capacity));
    return end && std::string(value, end) == expected;
}
bool Identity(const dm_ioctl& dm, size_t capacity, dev_t mapper, const std::string& uuid) {
    // CREATE/STATUS return only the ABI header, without its trailing padding.
    return dm.version[0] == DM_VERSION_MAJOR && dm.data_size >= offsetof(dm_ioctl, data) &&
           dm.data_size <= capacity && dm.dev == static_cast<uint64_t>(mapper) &&
           Equal(dm.name, sizeof(dm.name), R1_EXPDB_MAP_NAME) &&
           Equal(dm.uuid, sizeof(dm.uuid), uuid);
}

// Sysfs attributes are bounded and read to EOF; embedded NULs are never accepted.
bool ReadText(const std::string& path, std::string* value, std::string* error) {
    Fd fd(open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (fd.value < 0) return Fail(error, "open " + path);
    value->clear();
    std::array<char, 1024> chunk;
    while (true) {
        ssize_t count = read(fd.value, chunk.data(), chunk.size());
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) return Fail(error, "read " + path);
        if (!count) break;
        if (value->size() + count > 4096 || std::memchr(chunk.data(), 0, count))
            return Reject(error, "Invalid or oversized sysfs attribute: " + path);
        value->append(chunk.data(), count);
    }
    if (!value->empty() && value->back() == '\n') value->pop_back();
    return true;
}

bool Field(const std::string& text, const std::string& key, std::string* value) {
    bool found = false;
    size_t begin = 0;
    while (begin < text.size()) {
        size_t end = text.find('\n', begin);
        if (end == std::string::npos) end = text.size();
        if (text.compare(begin, key.size(), key) == 0 && begin + key.size() < end &&
            text[begin + key.size()] == '=') {
            if (found) return false;
            *value = text.substr(begin + key.size() + 1, end - begin - key.size() - 1);
            found = true;
        }
        begin = end + 1;
    }
    return found;
}
std::string DeviceNumber(dev_t device) {
    return std::to_string(major(device)) + ":" + std::to_string(minor(device));
}

class Runner {
  public:
    explicit Runner(std::string* error) : error_(error) {}
    ~Runner() {
        // After attempting attachment, module failure or a failed readback can
        // leave ownership uncertain. Retain the map instead of risking live I/O.
        if (created_ && !attach_attempted_) Cleanup();
    }
    bool Run();

  private:
    bool Clock(uint64_t* now) {
        timespec ts {};
        if (clock_gettime(CLOCK_MONOTONIC, &ts)) return Fail(error_, "monotonic clock");
        *now = static_cast<uint64_t>(ts.tv_sec) * 1000 + ts.tv_nsec / 1000000;
        return true;
    }
    bool Pause() {
        uint64_t now;
        if (!Clock(&now)) return false;
        if (now >= deadline_) return Reject(error_, "expdb discovery/readiness timed out");
        const uint64_t wait_ms = deadline_ - now < 100 ? deadline_ - now : 100;
        timespec delay {0, static_cast<long>(wait_ms * 1000000)};
        while (nanosleep(&delay, &delay)) {
            if (errno != EINTR) return Fail(error_, "wait for expdb");
            if (!Clock(&now)) return false;
            if (now >= deadline_) return Reject(error_, "expdb discovery/readiness timed out");
        }
        return true;
    }
    bool Absent(const char* path) {
        struct stat st {};
        if (!lstat(path, &st)) return Reject(error_, std::string("Already loaded: ") + path);
        if (errno != ENOENT) return Fail(error_, std::string("inspect ") + path);
        return true;
    }
    bool EmptyBackend() {
        std::string value;
        if (!ReadText(kBackend, &value, error_)) return false;
        // Linux's %s formatter renders the initial NULL charp as '(null)'.
        return value.empty() || value == "(null)" || Reject(error_, "A pstore backend is already selected");
    }
    bool Discover();
    bool CheckBlock(const std::string& path, dev_t expected, uint64_t size);
    bool OwnStatus();
    bool VerifyMap();
    bool WaitLinks();
    bool LoadModule(const char* path, const std::string& parameters, bool attaching);
    bool VerifyBackend();
    void Cleanup();

    std::string* error_;
    Fd control_;
    std::string uuid_, mapper_path_;
    dev_t backing_ = 0, mapper_ = 0;
    uint64_t deadline_ = 0;
    bool created_ = false, attach_attempted_ = false;
};

bool Runner::Discover() {
    while (true) {
        DIR* raw = opendir("/sys/class/block");
        if (!raw) return Fail(error_, "scan block devices");
        std::unique_ptr<DIR, int (*)(DIR*)> directory(raw, closedir);
        std::string candidate;
        while (true) {
            errno = 0;
            dirent* entry = readdir(directory.get());
            if (!entry) {
                if (errno) return Fail(error_, "enumerate block devices");
                break;
            }
            if (!r1_expdb_partition_name(entry->d_name)) continue;
            const std::string name = entry->d_name;
            std::string text, part;
            if (!ReadText("/sys/class/block/" + name + "/uevent", &text, error_)) return false;
            if (!Field(text, "PARTNAME", &part))
                return Reject(error_, "Missing or duplicate GPT name for " + name);
            if (part != "expdb") continue;
            if (!candidate.empty()) return Reject(error_, "More than one expdb partition");
            std::string devname, kind;
            if (!Field(text, "DEVNAME", &devname) || devname != name ||
                !Field(text, "DEVTYPE", &kind) || kind != "partition")
                return Reject(error_, "Invalid expdb sysfs identity");
            candidate = name;
        }
        directory.reset();
        if (candidate.empty()) { if (!Pause()) return false; continue; }
        const std::string parent = candidate.substr(0, candidate.rfind('p'));
        std::string type, number;
        if (!ReadText("/sys/class/block/" + parent + "/device/type", &type, error_) ||
            !ReadText("/sys/class/block/" + candidate + "/dev", &number, error_)) return false;
        if (type != "MMC") return Reject(error_, "expdb is not on an eMMC disk");
        Fd partition(open(("/dev/block/" + candidate).c_str(),
                          O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_EXCL));
        if (partition.value < 0) {
            if (errno == ENOENT) { if (!Pause()) return false; continue; }
            return Fail(error_, "open exclusive expdb");
        }
        struct stat st {};
        uint64_t size = 0;
        if (fstat(partition.value, &st)) return Fail(error_, "stat expdb");
        if (!S_ISBLK(st.st_mode) || number != DeviceNumber(st.st_rdev))
            return Reject(error_, "Wrong expdb device node");
        if (ioctl(partition.value, BLKGETSIZE64, &size)) return Fail(error_, "size expdb");
        if (size != R1_EXPDB_BYTES) return Reject(error_, "expdb must be exactly 20 MiB");
        backing_ = st.st_rdev;
        // Release O_EXCL before DM acquires its holder claim. This cannot
        // exclude an unrelated privileged raw writer across the transition.
        return true;
    }
}

bool Runner::CheckBlock(const std::string& path, dev_t expected, uint64_t size) {
    Fd node(open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (node.value < 0) return Fail(error_, "open bounded mapper");
    struct stat st {};
    uint64_t actual = 0;
    if (fstat(node.value, &st)) return Fail(error_, "stat bounded mapper");
    if (!S_ISBLK(st.st_mode) || st.st_rdev != expected)
        return Reject(error_, "Wrong bounded mapper device node");
    if (ioctl(node.value, BLKGETSIZE64, &actual)) return Fail(error_, "size bounded mapper");
    return actual == size || Reject(error_, "Wrong bounded mapper size");
}
bool Runner::OwnStatus() {
    Buffer buffer;
    auto* dm = buffer.Request({}, uuid_);
    if (ioctl(control_.value, DM_DEV_STATUS, dm)) return Fail(error_, "query expdb map ownership");
    return Identity(*dm, buffer.bytes.size(), mapper_, uuid_) || Reject(error_, "expdb map ownership changed");
}
bool Runner::VerifyMap() {
    Buffer buffer;
    auto* dm = buffer.Request({}, uuid_);
    dm->flags = DM_STATUS_TABLE_FLAG;
    if (ioctl(control_.value, DM_TABLE_STATUS, dm)) return Fail(error_, "read active expdb table");
    return ValidateExpdbTable(buffer.bytes.data(), buffer.bytes.size(), backing_, mapper_, uuid_, error_) &&
           CheckBlock(mapper_path_, mapper_, R1_EXPDB_LOG_BYTES);
}
bool Runner::WaitLinks() {
    const std::array<std::string, 2> paths = {
        "/dev/block/mapper/by-uuid/" + uuid_, "/dev/block/mapper/" R1_EXPDB_MAP_NAME};
    while (true) {
        bool ready = true;
        for (const auto& path : paths) {
            char resolved[PATH_MAX];
            if (!realpath(path.c_str(), resolved)) {
                if (errno != ENOENT) return Fail(error_, "resolve mapper link");
                ready = false;
            } else if (resolved != mapper_path_) {
                return Reject(error_, "Stale or incorrect mapper link");
            }
        }
        if (ready) return true;
        if (!OwnStatus() || !Pause()) return false;
    }
}
bool Runner::LoadModule(const char* path, const std::string& parameters, bool attaching) {
    Fd file(open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (file.value < 0) return Fail(error_, std::string("open module ") + path);
    struct stat st {};
    if (fstat(file.value, &st)) return Fail(error_, "stat module");
    if (!S_ISREG(st.st_mode) || st.st_size <= 0) return Reject(error_, "Invalid module file");
    if (attaching && !VerifyMap()) return false;
    if (attaching) attach_attempted_ = true;
    if (syscall(SYS_finit_module, file.value, parameters.c_str(), 0))
        return Fail(error_, std::string("load module ") + path);
    return true;
}
bool Runner::VerifyBackend() {
    std::string value;
    if (!ReadText(kBackend, &value, error_)) return false;
    if (value != "pstore_blk") return Reject(error_, "pstore_blk did not register its backend");
    const std::array<std::pair<const char*, std::string>, 7> parameters = {{
        {"blkdev", mapper_path_}, {"best_effort", "Y"}, {"console_size", "1024"},
        {"pmsg_size", "64"}, {"kmsg_size", "64"}, {"ftrace_size", "0"}, {"max_reason", "2"}}};
    for (const auto& [name, expected] : parameters) {
        if (!ReadText(std::string("/sys/module/pstore_blk/parameters/") + name, &value, error_)) return false;
        if (value != expected) return Reject(error_, std::string("Wrong pstore_blk parameter: ") + name);
    }
    return VerifyMap();
}
void Runner::Cleanup() {
    const std::string original = *error_;
    if (!mapper_ || !Absent("/sys/module/pstore_blk") || !EmptyBackend() || !OwnStatus()) {
        *error_ = original + "; map retained because safe cleanup could not be established";
        return;
    }
    Buffer buffer;
    // UUID lookup makes a concurrently replaced name insufficient for deletion.
    auto* dm = buffer.Request({}, uuid_);
    if (ioctl(control_.value, DM_DEV_REMOVE, dm)) {
        *error_ = original + "; owned map cleanup failed: " + std::strerror(errno);
        return;
    }
    created_ = false;
    *error_ = original;
}

bool Runner::Run() {
    error_->clear();
    if (!Clock(&deadline_)) return false;
    deadline_ += 30000;
    if (!Absent("/sys/module/pstore_blk") || !Absent("/sys/module/pstore_zone") || !EmptyBackend() ||
        !Discover()) return false;
    control_.value = open("/dev/device-mapper", O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (control_.value < 0) return Fail(error_, "open device-mapper control");
    struct stat st {};
    if (fstat(control_.value, &st)) return Fail(error_, "stat device-mapper control");
    if (!S_ISCHR(st.st_mode)) return Reject(error_, "device-mapper control is not a character device");
    Buffer buffer;
    auto* dm = buffer.Request(R1_EXPDB_MAP_NAME);
    if (!ioctl(control_.value, DM_DEV_STATUS, dm)) return Reject(error_, "r1-expdb mapping already exists");
    if (errno != ENXIO) return Fail(error_, "check existing expdb mapping");

    std::array<unsigned char, 16> random;
    size_t received = 0;
    while (received < random.size()) {
        ssize_t count = getrandom(random.data() + received, random.size() - received, 0);
        if (count < 0 && errno == EINTR) continue;
        if (!count) errno = EIO;
        if (count <= 0) return Fail(error_, "generate expdb UUID");
        received += count;
    }
    uuid_ = "haretic-expdb-";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char byte : random) { uuid_ += hex[byte >> 4]; uuid_ += hex[byte & 15]; }
    dm = buffer.Request(R1_EXPDB_MAP_NAME, uuid_);
    if (ioctl(control_.value, DM_DEV_CREATE, dm)) return Fail(error_, "create expdb mapping");
    created_ = true;
    mapper_ = static_cast<dev_t>(dm->dev);
    if (!mapper_ || !Identity(*dm, buffer.bytes.size(), mapper_, uuid_))
        return Reject(error_, "Invalid created mapper identity");
    mapper_path_ = "/dev/block/dm-" + std::to_string(minor(mapper_));
    dm = buffer.Request({}, uuid_);
    if (!r1_expdb_linear_table(dm, buffer.bytes.size(), backing_))
        return Reject(error_, "Cannot construct bounded expdb table");
    if (ioctl(control_.value, DM_TABLE_LOAD, dm)) return Fail(error_, "load expdb table");
    dm = buffer.Request({}, uuid_);
    if (ioctl(control_.value, DM_DEV_SUSPEND, dm)) return Fail(error_, "activate expdb table");
    if (!WaitLinks() || !VerifyMap()) return false;
    if (!LoadModule(kZone, "", false)) return false;
    if (!Absent("/sys/module/pstore_blk") || !EmptyBackend()) return false;
    const std::string parameters = "blkdev=" + mapper_path_ +
        " best_effort=1 console_size=1024 pmsg_size=64 kmsg_size=64 ftrace_size=0 max_reason=2";
    if (!LoadModule(kBlk, parameters, true) || !VerifyBackend()) return false;
    error_->clear();
    return true;
}
}  // namespace

bool ValidateExpdbTable(const void* buffer, size_t capacity, dev_t backing,
                        dev_t mapper, const std::string& uuid, std::string* error) {
    if (capacity < sizeof(dm_ioctl)) return Reject(error, "Short mapper response");
    dm_ioctl dm;
    std::memcpy(&dm, buffer, sizeof(dm));
    if (!Identity(dm, capacity, mapper, uuid) || !(dm.flags & DM_ACTIVE_PRESENT_FLAG) ||
        (dm.flags & kRejectedFlags) || dm.target_count != 1 ||
        dm.data_start < sizeof(dm) || dm.data_start % 8 || dm.data_start > dm.data_size ||
        dm.data_size - dm.data_start <= sizeof(dm_target_spec))
        return Reject(error, "Invalid active expdb table header");
    const auto* first = static_cast<const unsigned char*>(buffer) + dm.data_start;
    dm_target_spec target;
    std::memcpy(&target, first, sizeof(target));
    const char* params = reinterpret_cast<const char*>(first + sizeof(target));
    const size_t available = dm.data_size - dm.data_start - sizeof(target);
    const char* end = static_cast<const char*>(std::memchr(params, 0, available));
    if (!end || target.status || target.sector_start || target.length != R1_EXPDB_LOG_SECTORS ||
        !Equal(target.target_type, sizeof(target.target_type), "linear") ||
        std::string(params, end) != DeviceNumber(backing) + " 0")
        return Reject(error, "expdb table exceeds or changes the permitted extent");
    // Kernel data_size ends at NUL; next includes up to seven padding bytes.
    const size_t next = (sizeof(target) + size_t(end - params) + 1 + 7) & ~size_t{7};
    if (target.next != next || next > capacity - dm.data_start)
        return Reject(error, "Invalid expdb table target boundary");
    return true;
}

bool StartExpdb(std::string* error) {
    Runner runner(error);
    return runner.Run();
}
}  // namespace haretic::logging
