// SPDX-License-Identifier: Apache-2.0
#include "StartupOps.h"

#include <aidl/android/hardware/health/IHealth.h>
#include <android/binder_manager.h>
#include <cerrno>

using aidl::android::hardware::health::HealthInfo;
using aidl::android::hardware::health::IHealth;

namespace {
class BinderReadiness final : public r1::health::StartupOps {
  public:
    r1::health::Result Attempt() override {
        r1::health::Identity before{}, after{};
        if (auto r = r1::health::ReadIdentity("/sys", &before); r.error) return r;
        const std::string instance = std::string(IHealth::descriptor) + "/default";
        ndk::SpAIBinder binder(AServiceManager_checkService(instance.c_str()));
        if (!binder.get()) return {EAGAIN, "Health default instance is not registered"};
        auto health = IHealth::fromBinder(binder);
        if (!health) return {EPROTO, "registered Health Binder has the wrong interface"};
        int32_t capacity = -1;
        auto status = health->getCapacity(&capacity);
        if (!status.isOk()) return {EIO, "getCapacity: " + status.getDescription()};
        if (capacity < 0 || capacity > 100) return {ERANGE, "getCapacity is outside 0..100"};
        HealthInfo info;
        status = health->getHealthInfo(&info);
        if (!status.isOk()) return {EIO, "getHealthInfo: " + status.getDescription()};
        if (!info.batteryPresent) return {ENODEV, "HealthInfo reports no battery"};
        if (info.batteryLevel < 0 || info.batteryLevel > 100)
            return {ERANGE, "HealthInfo battery level is outside 0..100"};
        if (auto r = r1::health::ReadIdentity("/sys", &after); r.error) return r;
        if (!(before == after)) return {ESTALE, "battery rebound during Binder readiness check"};
        // No positive floor, exact-equality check between separate readings,
        // status/level substitution, callback suppression or property signal.
        return {0, "initial Health capacity available, including genuine zero"};
    }
};
}  // namespace

int main(int, char** argv) {
    ::android::base::InitLogging(argv);
    BinderReadiness readiness;
    const auto result = r1::health::WaitUntilReady(readiness, std::chrono::seconds(35));
    if (result.error) {
        LOG(ERROR) << result.detail;
        return 1;
    }
    LOG(INFO) << result.detail;
    return 0;
}
