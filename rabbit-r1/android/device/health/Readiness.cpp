// SPDX-License-Identifier: Apache-2.0
#include "Readiness.h"

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <memory>
#include <sys/stat.h>
#include <unistd.h>

namespace r1::health {
namespace {
class Fd {
  public:
    explicit Fd(int fd) : fd_(fd) {}
    ~Fd() { if (fd_ >= 0) close(fd_); }
    int get() const { return fd_; }
  private:
    int fd_;
};

Result Error(int error, const std::string& operation) {
    return {error, operation + ": " + strerror(error)};
}

Result Resolve(const std::string& path, std::string* resolved) {
    std::unique_ptr<char, decltype(&free)> real(realpath(path.c_str(), nullptr), free);
    if (!real) return Error(errno, "realpath " + path);
    *resolved = real.get();
    return {};
}

Result ReadAttribute(int directory, const char* name, std::string* out) {
    const Fd fd(openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (fd.get() < 0) return Error(errno, std::string("open ") + name);
    out->clear();
    char bytes[128];
    for (;;) {
        ssize_t size = read(fd.get(), bytes, sizeof(bytes));
        if (size < 0 && errno == EINTR) continue;
        if (size < 0) return Error(errno, std::string("read ") + name);
        if (!size) break;
        if (out->size() + size > sizeof(bytes)) return Error(EOVERFLOW, name);
        out->append(bytes, size);
    }
    if (!out->empty() && out->back() == '\n') out->pop_back();
    return {};
}
}  // namespace

Result ReadIdentity(const std::string& sysfs_root, Identity* identity) {
    const std::string path = sysfs_root + "/class/power_supply/mt6357-battery";
    std::string resolved, devices;
    if (auto r = Resolve(path, &resolved); r.error) return r;
    if (auto r = Resolve(sysfs_root + "/devices", &devices); r.error) return r;
    if (resolved.compare(0, devices.size() + 1, devices + "/") != 0)
        return Error(ENODEV, "battery is outside sysfs devices");
    const Fd supply(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (supply.get() < 0) return Error(errno, "open battery directory");
    struct stat before{}, after{}, driver{}, expected{};
    if (fstat(supply.get(), &before)) return Error(errno, "fstat battery directory");
    const Fd device(openat(supply.get(), "device", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
    if (device.get() < 0) return Error(errno, "open battery device");
    if (fstatat(device.get(), "driver", &driver, 0)) return Error(errno, "stat battery driver");
    const std::string expected_driver = sysfs_root + "/bus/platform/drivers/mt6357-gauge";
    if (stat(expected_driver.c_str(), &expected)) return Error(errno, "stat mt6357-gauge driver");
    if (driver.st_dev != expected.st_dev || driver.st_ino != expected.st_ino)
        return Error(ENODEV, "battery driver is not mt6357-gauge");
    std::string value;
    if (auto r = ReadAttribute(supply.get(), "type", &value); r.error) return r;
    if (value != "Battery") return Error(ENODEV, "power supply is not Battery");
    if (auto r = ReadAttribute(supply.get(), "present", &value); r.error) return r;
    if (value != "1") return Error(ENODEV, "battery is not present");
    std::string resolved_after;
    if (auto r = Resolve(path, &resolved_after); r.error) return r;
    if (stat(path.c_str(), &after)) return Error(errno, "restat battery directory");
    if (resolved != resolved_after || before.st_dev != after.st_dev ||
        before.st_ino != after.st_ino)
        return Error(ESTALE, "battery identity changed during check");
    *identity = {before.st_dev, before.st_ino, resolved};
    return {};
}

Result WaitUntilReady(ReadinessOps& ops, std::chrono::seconds budget) {
    const auto deadline = ops.Now() + budget;
    Result last{ENODATA, "no initial measurement"};
    Result reported{};
    for (;;) {
        if (ops.Now() >= deadline) break;
        last = ops.Attempt();
        if (ops.Now() >= deadline) break;
        if (!last.error) return last;
        if (last.error != reported.error || last.detail != reported.detail) {
            ops.Report(last);
            reported = last;
        }
        const auto remaining = deadline - ops.Now();
        if (remaining <= Clock::duration::zero()) break;
        ops.Sleep(std::min(remaining, Clock::duration(std::chrono::milliseconds(100))));
    }
    return {ETIMEDOUT, "initial battery deadline expired; last attempt: " + last.detail +
                        " (errno " + std::to_string(last.error) + ")"};
}
}  // namespace r1::health
