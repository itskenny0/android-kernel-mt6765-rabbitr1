// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <chrono>
#include <string>
#include <sys/types.h>

namespace r1::health {

using Clock = std::chrono::steady_clock;
struct Result {
    int error = 0;  // Positive errno, or zero for readiness.
    std::string detail;
};

struct Identity {
    dev_t device;
    ino_t inode;
    std::string path;
    bool operator==(const Identity& rhs) const {
        return device == rhs.device && inode == rhs.inode && path == rhs.path;
    }
};

// sysfs_root is /sys in the two executables. Tests supply a private fixture.
// Checks real device association, Battery type and genuine present=1. This
// does not make multiple sysfs reads an atomic measurement snapshot.
Result ReadIdentity(const std::string& sysfs_root, Identity* identity);

class ReadinessOps {
  public:
    virtual ~ReadinessOps() = default;
    virtual Clock::time_point Now() = 0;
    virtual void Sleep(Clock::duration duration) = 0;
    virtual Result Attempt() = 0;
    virtual void Report(const Result& result) = 0;
};

// Deadlines bound decisions between calls; init's service timeout separately
// terminates a hung helper/Binder operation. No successful late result is used.
Result WaitUntilReady(ReadinessOps& ops, std::chrono::seconds budget);

}  // namespace r1::health
