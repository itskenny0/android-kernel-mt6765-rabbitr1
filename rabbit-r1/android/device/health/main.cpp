// SPDX-License-Identifier: Apache-2.0
#include "StartupOps.h"

#include <android/binder_interface_utils.h>
#include <health-impl/ChargerUtils.h>
#include <health-impl/Health.h>
#include <health/utils.h>
#include <cerrno>
#include <cstring>

using aidl::android::hardware::health::HalHealthLoop;
using aidl::android::hardware::health::Health;
using aidl::android::hardware::health::charger::ChargerCallback;
using aidl::android::hardware::health::charger::ChargerModeMain;

namespace r1::health {
namespace {
std::unique_ptr<healthd_config> MakeConfig() {
    auto config = std::make_unique<healthd_config>();
    ::android::hardware::health::InitHealthdConfig(config.get());
    // Pin battery attributes to the identified pack. In particular, a second
    // supply must not fill an otherwise unsupported attribute for this pack.
    const std::pair<::android::String8 healthd_config::*, const char*> paths[] = {
        {&healthd_config::batteryStatusPath, "status"},
        {&healthd_config::batteryHealthPath, "health"},
        {&healthd_config::batteryPresentPath, "present"},
        {&healthd_config::batteryCapacityPath, "capacity"},
        {&healthd_config::batteryVoltagePath, "voltage_now"},
        {&healthd_config::batteryTemperaturePath, "temp"},
        {&healthd_config::batteryTechnologyPath, "technology"},
        {&healthd_config::batteryCurrentNowPath, "current_now"},
        {&healthd_config::batteryCurrentAvgPath, "current_avg"},
        {&healthd_config::batteryChargeCounterPath, "charge_counter"},
        {&healthd_config::batteryFullChargePath, "charge_full"},
        {&healthd_config::batteryCycleCountPath, "cycle_count"},
        {&healthd_config::batteryCapacityLevelPath, "capacity_level"},
        {&healthd_config::batteryChargeTimeToFullNowPath, "time_to_full_now"},
        {&healthd_config::batteryFullChargeDesignCapacityUahPath, "charge_full_design"},
        {&healthd_config::batteryStateOfHealthPath, "state_of_health"},
        {&healthd_config::batteryHealthStatusPath, "health_status"},
        {&healthd_config::batteryManufacturingDatePath, "manufacturing_date"},
        {&healthd_config::batteryFirstUsageDatePath, "first_usage_date"},
        {&healthd_config::chargingStatePath, "charging_state"},
        {&healthd_config::chargingPolicyPath, "charging_policy"},
        {&healthd_config::batterySerialPath, "serial_number"},
        {&healthd_config::batteryManufacturerPath, "manufacturer"},
        {&healthd_config::batteryModelNamePath, "model_name"},
        {&healthd_config::batteryVoltageMinDesignPath, "voltage_min_design"},
    };
    for (const auto& [member, name] : paths) {
        const std::string path = std::string("/sys/class/power_supply/mt6357-battery/") + name;
        config.get()->*member = ::android::String8(path.c_str());
    }
    return config;
}

class PrimeReadiness final : public StartupOps {
  public:
    std::shared_ptr<Health> health;
    Result Attempt() override {
        Identity before{}, after{};
        if (auto r = ReadIdentity("/sys", &before); r.error) return r;
        // init() can disable polling if discovery loses a registration race.
        // Every unsuccessful attempt discards both monitor and config.
        auto candidate = ndk::SharedRefBase::make<Health>("default", MakeConfig());
        const auto error = candidate->PrimeBatteryLevel();
        if (error != ::android::OK)
            return {-error, std::string("prime actual battery capacity: ") + strerror(-error)};
        if (auto r = ReadIdentity("/sys", &after); r.error) return r;
        if (!(before == after)) return {ESTALE, "battery rebound while priming Health"};
        health = std::move(candidate);
        return {0, "same Health monitor primed from an actual capacity reading"};
    }
};
}  // namespace
}  // namespace r1::health

int main(int argc, char** argv) {
    ::android::base::InitLogging(argv);
    if (argc == 2 && std::string_view(argv[1]) == "--charger") {
        // Preserve the stock charger-mode path. The initial readiness
        // ordering and failure policy apply only to normal boot.
        auto config = std::make_unique<healthd_config>();
        ::android::hardware::health::InitHealthdConfig(config.get());
        auto health = ndk::SharedRefBase::make<Health>("default", std::move(config));
        return ChargerModeMain(health, std::make_shared<ChargerCallback>(health));
    }
    if (argc != 1) {
        LOG(ERROR) << "Unsupported Health service arguments";
        return 1;
    }
    r1::health::PrimeReadiness readiness;
    // Engineering budget only; not a measured hardware initialization bound.
    auto result = r1::health::WaitUntilReady(readiness, std::chrono::seconds(30));
    if (result.error) {
        LOG(ERROR) << result.detail;
        return 1;
    }
    LOG(INFO) << result.detail;
    return std::make_shared<HalHealthLoop>(readiness.health, readiness.health)->StartLoop();
}
