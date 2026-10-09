// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "Readiness.h"
#include <android-base/logging.h>
#include <thread>

namespace r1::health {
class StartupOps : public ReadinessOps {
  public:
    Clock::time_point Now() override { return Clock::now(); }
    void Sleep(Clock::duration delay) override { std::this_thread::sleep_for(delay); }
    void Report(const Result& result) override {
        LOG(WARNING) << result.detail << " (errno " << result.error << ')';
    }
};
}  // namespace r1::health
