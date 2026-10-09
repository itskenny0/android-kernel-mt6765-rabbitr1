// SPDX-License-Identifier: Apache-2.0
#include "Readiness.h"
#include <cassert>
#include <cerrno>
#include <chrono>
#include <iostream>
#include <vector>
using namespace r1::health;
using namespace std::chrono;
struct Model : ReadinessOps {
    Clock::time_point now{};
    Clock::duration attempt_time{}, report_time{}, oversleep{};
    int calls = 0, sleeps = 0, reports = 0, errors_before_success = 0;
    Clock::time_point Now() override { return now; }
    void Sleep(Clock::duration d) override {
        assert(d > Clock::duration::zero() && d <= milliseconds(100));
        ++sleeps; now += d + oversleep;
    }
    Result Attempt() override {
        ++calls; now += attempt_time;
        return calls <= errors_before_success ? Result{ENODATA, "no genuine capacity"} : Result{0, "actual zero"};
    }
    void Report(const Result& r) override { assert(r.error); ++reports; now += report_time; }
};
int main() {
    int cases = 0;
    for (int budget : {30, 35}) {
        for (int offset : {-1, 0, 1}) {
            Model m; m.attempt_time = seconds(budget) + nanoseconds(offset);
            auto r = WaitUntilReady(m, seconds(budget));
            assert(r.error == (offset < 0 ? 0 : ETIMEDOUT));
            assert(m.calls == 1 && m.sleeps == 0); ++cases;
        }
        Model transient; transient.errors_before_success = 3;
        assert(!WaitUntilReady(transient, seconds(budget)).error);
        assert(transient.calls == 4 && transient.sleeps == 3 && transient.reports == 1); ++cases;
        Model never; never.errors_before_success = 10000;
        assert(WaitUntilReady(never, seconds(budget)).error == ETIMEDOUT);
        assert(never.calls == budget * 10 && never.now == Clock::time_point{} + seconds(budget)); ++cases;
        Model blocked_report; blocked_report.errors_before_success = 1; blocked_report.report_time = seconds(budget);
        assert(WaitUntilReady(blocked_report, seconds(budget)).error == ETIMEDOUT);
        assert(blocked_report.calls == 1 && blocked_report.sleeps == 0); ++cases;
        Model late_wakeup; late_wakeup.errors_before_success = 1; late_wakeup.oversleep = seconds(budget);
        assert(WaitUntilReady(late_wakeup, seconds(budget)).error == ETIMEDOUT);
        assert(late_wakeup.calls == 1 && late_wakeup.sleeps == 1); ++cases;
    }
    Model none; assert(WaitUntilReady(none, seconds(0)).error == ETIMEDOUT && !none.calls); ++cases;
    std::cout << cases << " exact-deadline/retry/report/wakeup cases passed; actual WaitUntilReady body, modeled monotonic clock and calls\n";
}
