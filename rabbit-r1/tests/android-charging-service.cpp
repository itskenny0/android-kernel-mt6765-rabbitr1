// SPDX-License-Identifier: Apache-2.0
#include <cassert>
#include <iostream>
#include <string>
#include <android-base/file.h>
#include <android-base/properties.h>
#include "choice.h"
static std::string saved = "auto", node = "500000\n";
static bool available = true, writable = true;
static int writes;
namespace android::base {
std::string GetProperty(const std::string& key, const std::string& fallback) {
    assert(key == r1::kProperty && fallback == "auto"); return saved;
}
bool ReadFileToString(const std::string& path, std::string* out, bool) {
    assert(path == r1::kLimit); *out = node; return available;
}
bool WriteStringToFile(const std::string& value, const std::string& path, bool) {
    assert(path == r1::kLimit); ++writes; if (writable) node = value; return writable;
}
}
#include "reconcile-under-test.h"
int main() {
    const std::string choices[] = {"auto", "500", "600", "700", "800", "900", "1000"};
    for (const auto& choice : choices) {
        saved=choice; node="500000\n"; writes=0;
        assert(Reconcile().empty());
        assert(node == std::to_string(choice=="auto" ? 1000000 : std::stoi(choice)*1000)+"\n");
        int previous=writes; assert(Reconcile().empty() && writes==previous);
        node="0\n"; assert(Reconcile().empty() && writes==previous+1); // Rebind/readback recovery.
    }
    available=false; saved="600"; writes=0;
    assert(!Reconcile().empty() && !writes); // Save survives deferred probe.
    available=true; assert(Reconcile().empty() && node=="600000\n");
    writable=false; saved="500";
    assert(!Reconcile().empty() && node=="600000\n");
    writable=true; assert(Reconcile().empty() && node=="500000\n");
    const std::string invalid[]={"", "0", "-1", "1001", "500000", "500\n", " 500", "5e2", "0500", "Auto"};
    for (const auto& value : invalid) {
        assert(r1::CurrentLimit(value)==-1); saved=value; node="1000000\n";
        assert(!Reconcile().empty() && node=="500000\n");
    }
    std::cout << "PASS: Android choices, persistence restoration, deferred probe, rebind, failed writes and corrupt preferences\n";
}
