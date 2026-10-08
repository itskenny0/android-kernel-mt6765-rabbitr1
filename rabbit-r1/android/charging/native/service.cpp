// SPDX-License-Identifier: Apache-2.0
#include "choice.h"
#include <android-base/file.h>
#include <android-base/logging.h>
#include <android-base/properties.h>
#include <chrono>
#include <string>
#include <thread>

static std::string Reconcile() {
    const std::string choice = android::base::GetProperty(r1::kProperty, "auto");
    const int parsed = r1::CurrentLimit(choice);
    // A corrupt stored choice must not increase the limit.
    const std::string wanted = std::to_string(parsed < 0 ? 500000 : parsed) + "\n";
    std::string actual;
    std::string error;
    if (!android::base::ReadFileToString(r1::kLimit, &actual)) {
        error = "Charging policy unavailable; saved choice will be retried";
    } else if (actual != wanted && !android::base::WriteStringToFile(wanted, r1::kLimit)) {
        error = "Could not apply charging limit; retrying";
    } else if (parsed < 0) {
        error = "Invalid saved charging choice; using 500 mA";
    }
    return error;
}

int main(int argc, char** argv) {
    (void)argc;
    android::base::InitLogging(argv, android::base::LogdLogger(android::base::SYSTEM));
    std::string previous_error;
    for (;;) {
        std::string error = Reconcile();
        if (!error.empty() && error != previous_error) LOG(WARNING) << error;
        previous_error = error;
        // Read back on every pass: handles probe deferral, rebind and service restart.
        std::this_thread::sleep_for(std::chrono::seconds(2));
    }
}
