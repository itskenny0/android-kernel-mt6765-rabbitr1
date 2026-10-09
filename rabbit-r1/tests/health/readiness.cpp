// SPDX-License-Identifier: Apache-2.0
#include "Readiness.h"
#include <cassert>
#include <cerrno>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sys/stat.h>
#include <vector>

namespace fs = std::filesystem;
using namespace r1::health;
static std::string replace_on_stat, replacement_target;
extern "C" int __real_stat(const char*, struct stat*);
extern "C" int __wrap_stat(const char* path, struct stat* st) {
    if (path == replace_on_stat) {
        fs::remove(path);
        fs::create_directory_symlink(replacement_target, path);
        replace_on_stat.clear();
    }
    return __real_stat(path, st);
}

static void Write(const fs::path& path, const std::string& value) {
    std::ofstream file(path);
    file << value;
    assert(file.good());
}

struct Fixture {
    fs::path root, supply, link, device, driver;
    explicit Fixture(fs::path path) : root(std::move(path)) {
        device = root / "devices/platform/gauge";
        supply = device / "power_supply/mt6357-battery";
        link = root / "class/power_supply/mt6357-battery";
        driver = root / "bus/platform/drivers/mt6357-gauge";
        fs::create_directories(supply);
        fs::create_directories(link.parent_path());
        fs::create_directories(driver);
        fs::create_directory_symlink(supply, link);
        fs::create_directory_symlink(device, supply / "device");
        fs::create_directory_symlink(driver, device / "driver");
        Write(supply / "type", "Battery\n");
        Write(supply / "present", "1\n");
    }
};

class Model final : public ReadinessOps {
  public:
    Clock::time_point now{};
    std::vector<Result> attempts;
    int calls = 0, sleeps = 0, reports = 0;
    Clock::duration in_call{};
    Clock::time_point Now() override { return now; }
    void Sleep(Clock::duration duration) override {
        assert(duration > Clock::duration::zero());
        assert(duration <= std::chrono::milliseconds(100));
        now += duration;
        ++sleeps;
    }
    Result Attempt() override {
        now += in_call;
        return attempts.at(std::min<size_t>(calls++, attempts.size() - 1));
    }
    void Report(const Result&) override { ++reports; }
};

int main(int argc, char** argv) {
    assert(argc == 2);
    const fs::path root = fs::absolute(argv[1]);
    assert(root.string().starts_with("/rabbitr1/out/health-readiness-tests/"));
    assert(!fs::exists(root));
    Fixture fixture(root);
    Identity original{}, again{};
    assert(!ReadIdentity(root, &original).error);
    assert(!ReadIdentity(root, &again).error && original == again);
    Write(fixture.supply / "present", "0\n");
    assert(ReadIdentity(root, &again).error == ENODEV);
    Write(fixture.supply / "present", "1\n");
    Write(fixture.supply / "type", "USB\n");
    assert(ReadIdentity(root, &again).error == ENODEV);
    Write(fixture.supply / "type", "Battery\n");
    fs::remove(fixture.device / "driver");
    fs::create_directory_symlink(fixture.device, fixture.device / "driver");
    assert(ReadIdentity(root, &again).error == ENODEV);
    fs::remove(fixture.device / "driver");
    fs::create_directory_symlink(fixture.driver, fixture.device / "driver");
    fs::remove(fixture.supply / "present");
    assert(ReadIdentity(root, &again).error == ENOENT);
    fs::create_directory(fixture.supply / "present");
    assert(ReadIdentity(root, &again).error == EISDIR);
    fs::remove(fixture.supply / "present");
    Write(fixture.supply / "present", std::string(129, '1'));
    assert(ReadIdentity(root, &again).error == EOVERFLOW);
    Write(fixture.supply / "present", "1\n");
    auto replacement = fixture.root / "devices/replacement";
    fs::create_directories(replacement);
    replace_on_stat = fixture.link;
    replacement_target = replacement;
    assert(ReadIdentity(root, &again).error == ESTALE);
    fs::remove(fixture.link);
    assert(ReadIdentity(root, &again).error == ENOENT);

    Model late;
    late.attempts = {{ENOENT, "provider absent"}, {ENODATA, "no capacity"}, {0, "genuine 0"}};
    assert(!WaitUntilReady(late, std::chrono::seconds(30)).error);
    assert(late.calls == 3 && late.sleeps == 2 && late.reports == 2);
    Model unavailable;
    unavailable.attempts = {{EACCES, "capacity permission denied"}};
    auto timeout = WaitUntilReady(unavailable, std::chrono::seconds(2));
    assert(timeout.error == ETIMEDOUT && timeout.detail.find("permission denied") != std::string::npos);
    assert(unavailable.calls == 20 && unavailable.sleeps == 20 && unavailable.reports == 1);
    Model slow;
    slow.attempts = {{0, "a late successful read"}};
    slow.in_call = std::chrono::seconds(30);
    assert(WaitUntilReady(slow, std::chrono::seconds(30)).error == ETIMEDOUT);
    assert(slow.calls == 1 && slow.sleeps == 0);
    fs::remove_all(root);
    std::cout << "identity and bounded readiness cases passed\n";
}
