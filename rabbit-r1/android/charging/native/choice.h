// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <string_view>

namespace r1 {
inline constexpr char kProperty[] = "persist.sys.r1.charge_limit";
inline constexpr char kLimit[] = "/sys/devices/platform/charging-policy/charge_control_limit";

// Return -1 for malformed input. Automatic means the board maximum, never unlimited.
inline int CurrentLimit(std::string_view choice) {
    if (choice == "auto") return 1000000;
    if (choice == "500") return 500000;
    if (choice == "600") return 600000;
    if (choice == "700") return 700000;
    if (choice == "800") return 800000;
    if (choice == "900") return 900000;
    if (choice == "1000") return 1000000;
    return -1;
}
}  // namespace r1
