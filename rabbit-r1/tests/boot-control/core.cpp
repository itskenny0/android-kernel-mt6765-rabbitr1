// SPDX-License-Identifier: Apache-2.0
#include "Control.h"

#include <algorithm>
#include <cassert>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

using namespace haretic::boot;

class FakeStorage : public Storage {
  public:
    Record record {};
    uint8_t config = 0x48;
    unsigned failures = 0;
    unsigned ignored_selector_writes = 0;
    unsigned wrong_config_reads = 0;
    bool apply_failed_write = false;
    unsigned calls = 0;
    std::string events;
    bool Call(char event) {
        events += event;
        assert(calls < 31);
        return !(failures & (1U << calls++));
    }
    bool ReadRecord(Record* out) override {
        const bool ok = Call('R');
        if (ok) *out = record;
        return ok;
    }
    bool WriteRecord(const Record& in) override {
        assert(Control::Valid(in));
        const bool ok = Call('W');
        if (ok) record = in;
        else if (apply_failed_write) std::copy_n(in.begin(), 16, record.begin());
        return ok;
    }
    bool ReadBootConfig(uint8_t* out) override {
        const bool ok = Call('E');
        if (ok) {
            *out = config;
            if (wrong_config_reads & (1U << (calls - 1))) *out ^= 0x18;
        }
        return ok;
    }
    bool WriteBootConfig(uint8_t in) override {
        assert((in & ~0x38) == (config & ~0x38));
        const bool ok = Call('S');
        if ((ok && !(ignored_selector_writes & (1U << (calls - 1)))) ||
            (!ok && apply_failed_write)) config = in;
        return ok;
    }
    std::string Error() const override { return "injected I/O failure"; }
};

Record Decode(const std::string& hex) {
    assert(hex.size() == 64);
    Record record;
    for (unsigned i = 0; i < record.size(); ++i) {
        record[i] = std::stoul(hex.substr(2 * i, 2), nullptr, 16);
    }
    return record;
}

int main(int argc, char* argv[]) {
    assert(argc == 2);
    std::ifstream vectors(argv[1]);
    assert(vectors);
    size_t cases = 0;
    Record base {};
    for (std::string line; std::getline(vectors, line);) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream fields(line);
        std::string action, before, after;
        unsigned current, slot;
        assert(fields >> action >> current >> slot >> before >> after);
        FakeStorage store;
        base = store.record = Decode(before);
        assert(Control::Valid(base));
        Control control(store, current);
        Result result = action == "active" ? control.SetActive(slot) :
                        action == "mark" ? control.MarkSuccessful() : control.SetUnbootable(slot);
        assert(result);
        assert(store.record == Decode(after));  // Independent stock-instruction oracle.
        assert(store.config == (action == "active" ? (slot == 0 ? 0x48 : 0x50) : 0x48));
        ++cases;
    }
    assert(cases == 80);

    for (unsigned current = 0; current < 2; ++current) {
        for (unsigned a = 0; a < 16; ++a) for (unsigned b = 0; b < 16; ++b) {
            FakeStorage store;
            store.record = base;
            store.record[12] = a;
            store.record[14] = b;
            Control::Seal(&store.record);
            Control control(store, current);
            unsigned slot = 999;
            assert(control.Active(&slot));
            assert(slot == (a == b ? current : a > b ? 0 : 1));
            assert(store.events == "R");
            ++cases;
        }
        for (unsigned value = 0; value < 256; ++value) for (unsigned slot = 0; slot < 2; ++slot) {
            FakeStorage store;
            store.record = base;
            store.record[12 + 2 * slot] = value;
            Control::Seal(&store.record);
            Control control(store, current);
            bool bootable = false, successful = false;
            assert(control.Bootable(slot, &bootable));
            assert(control.Successful(slot, &successful));
            assert(bootable == bool(value & 0x70));
            assert(successful == bool((value & 0x70) && (value & 0x80)));
            assert(store.events == "RR");
            ++cases;
        }
    }

    // Every single-bit corruption is rejected before any write or MMC request.
    for (unsigned i = 0; i < 32; ++i) for (unsigned bit = 0; bit < 8; ++bit) {
        FakeStorage store;
        store.record = base;
        store.record[i] ^= 1U << bit;
        const Record before = store.record;
        Control control(store, 0);
        assert(!control.SetActive(1));
        assert(store.events == "R" && store.record == before);
        ++cases;
    }
    for (unsigned index : {4U, 5U, 6U, 7U, 8U, 9U}) {
        FakeStorage store;
        store.record = base;
        store.record[index] ^= 1;
        Control::Seal(&store.record);  // Valid CRC does not excuse wrong format.
        Control control(store, 0);
        assert(!control.MarkSuccessful() && store.events == "R");
        ++cases;
    }
    for (unsigned slot : {2U, 3U, 0xffffffffU}) {
        FakeStorage store;
        store.record = base;
        Control control(store, 0);
        bool unused;
        assert(!control.SetActive(slot) && !control.SetUnbootable(slot));
        assert(!control.Bootable(slot, &unused) && !control.Successful(slot, &unused));
        assert(store.events.empty());
        ++cases;
    }

    // Fail each operation through the selector readback. Writes that report an
    // error can still partly affect media; the restoration must cope with both.
    for (unsigned failure = 0; failure < 5; ++failure) for (bool applied : {false, true}) {
        FakeStorage store;
        store.record = base;
        store.failures = 1U << failure;
        store.apply_failed_write = applied;
        Control control(store, 0);
        const Result result = control.SetActive(1);
        assert(!result && !result.error.empty());
        assert(store.record == base && store.config == 0x48);
        if (failure < 3) assert(store.events.find('S') == std::string::npos);
        ++cases;
    }
    // Selector change and its restoration both fail: never claim success or
    // restore old metadata while the hardware selection remains uncertain.
    for (bool applied : {false, true}) {
        FakeStorage store;
        store.record = base;
        store.failures = (1U << 3) | (1U << 4);
        store.apply_failed_write = applied;
        Control control(store, 0);
        const Result result = control.SetActive(1);
        assert(!result && result.error.find("recovery required") != std::string::npos);
        assert(store.events == "REWSS" && store.record != base);
        assert(Control::Valid(store.record));
        ++cases;
    }
    // Partial record write followed by failed restoration is reported; eMMC
    // remains untouched. Also exercise restoration failure after a switch.
    for (unsigned failures : {(1U << 2) | (1U << 3), (1U << 3) | (1U << 6)}) {
        FakeStorage store;
        store.record = base;
        store.failures = failures;
        store.apply_failed_write = true;
        Control control(store, 0);
        const Result result = control.SetActive(1);
        assert(!result && result.error.find("restoration") != std::string::npos);
        assert(store.config == 0x48);
        ++cases;
    }
    // A successful ioctl can leave the selector unchanged, and a successful
    // read can still return the wrong value. Both require verified rollback.
    for (bool ignored_switch : {false, true}) {
        FakeStorage store;
        store.record = base;
        if (ignored_switch) store.ignored_selector_writes = 1U << 3;
        else store.wrong_config_reads = 1U << 4;
        Control control(store, 0);
        const Result result = control.SetActive(1);
        assert(!result && result.error.find("original region and record restored") != std::string::npos);
        assert(store.events == "REWSESEW" && store.record == base && store.config == 0x48);
        ++cases;
    }
    // If rollback is ignored or cannot be read back, keep the new metadata.
    // Even an apparently restored selector is insufficient without readback.
    for (bool ignored_restore : {false, true}) {
        FakeStorage store;
        store.record = base;
        store.failures = 1U << 4;
        if (ignored_restore) store.ignored_selector_writes = 1U << 5;
        else store.failures |= 1U << 6;
        Control control(store, 0);
        const Result result = control.SetActive(1);
        assert(!result && result.error.find("recovery required") != std::string::npos);
        assert(store.events == "REWSESE" && store.record != base);
        assert(Control::Valid(store.record) && store.record[14] == 0xef);
        assert(store.config == (ignored_restore ? 0x50 : 0x48));
        ++cases;
    }
    // Mark-success shares the record restoration path and must propagate its
    // failure even if a partial restoring write happens to recover the bytes.
    for (bool failed_restore : {false, true}) {
        FakeStorage store;
        store.record = base;
        store.failures = (1U << 1) | (failed_restore ? 1U << 2 : 0);
        store.apply_failed_write = true;
        Control control(store, 0);
        const Result result = control.MarkSuccessful();
        assert(!result && store.events == "RWW" && store.record == base);
        assert(result.error.find(failed_restore ? "record restoration also failed" :
                                                "original record restored") != std::string::npos);
        ++cases;
    }
    {
        FakeStorage store;
        store.record = base;
        Control control(store, 2);
        assert(!control.MarkSuccessful() && store.events.empty());
        ++cases;
    }
    for (unsigned region : {0U, 3U, 4U, 5U, 6U, 7U}) {
        FakeStorage store;
        store.record = base;
        store.config = 0x40 | (region << 3);
        Control control(store, 0);
        assert(!control.SetActive(1));
        assert(store.events == "RE" && store.record == base);
        ++cases;
    }
    std::cout << "PASS: " << cases << " boot-control cases, including 80 stock HAL vectors\n";
}
