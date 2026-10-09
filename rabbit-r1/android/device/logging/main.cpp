// SPDX-License-Identifier: Apache-2.0
#include "Expdb.h"
#include <android-base/logging.h>
#include <android-base/properties.h>
#include <cstdlib>

int main(int argc, char* argv[]) {
    android::base::InitLogging(argv, android::base::KernelLogger);
    if (argc != 1 || android::base::GetProperty("ro.vendor.r1.expdb.enabled", "") != "1") {
        LOG(ERROR) << "haretic expdb: fixed build-profile opt-in is required";
        return EXIT_FAILURE;
    }
    std::string error;
    if (!haretic::logging::StartExpdb(&error)) {
        LOG(ERROR) << "haretic expdb unavailable: " << error;
        return EXIT_FAILURE;
    }
    LOG(INFO) << "haretic expdb ready: verified 18 MiB pstore_blk map; 2 MiB tail excluded";
    return EXIT_SUCCESS;
}
